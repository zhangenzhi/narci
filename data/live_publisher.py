"""TCP/JSON-lines live event broadcaster — narci-sg → echo-air channel.

Spec: echo INTERFACE_ECHO_NARCI.md §14 Delivery 9 (2026-05-19 pivot) +
§18 Delivery 13 (2026-05-21 wire v2 multi-symbol fix).

The recorder tees each WS event off `self.buffers[sym].extend(...)` and
fans it out to subscribed TCP clients as JSON lines:

**Wire protocol v2** (2026-05-21):

    {"kind":"hello","protocol_version":2}                                          ← sent once on connect
    {"venue":"um","symbol":"BTCUSDT","ts_ms":1779164453356,"side":2,"price":68234.5,"qty":0.123}
    {"venue":"um","symbol":"BTCUSDT","ts_ms":1779164453402,"side":0,"price":68234.5,"qty":0.5}
    {"kind":"heartbeat","ts_ms":1779164458356}

One JSON object per line, UTF-8, terminated `\\n`. Fields:
  - `venue`: echo-side venue tag (`um` / `bs` etc) — broadcaster-assigned
  - `symbol`: native exchange symbol (`BTCUSDT` etc) — passed through
    from `ExchangeAdapter.parse_message`. **Added in v2** to fix the
    multi-symbol contamination bug (echo D13 §18) — v1 was implicit
    one-symbol-per-venue, multi-symbol fan-out collapsed all symbols
    into one L2Reconstructor on subscriber side.
  - `ts_ms`: event timestamp (ms since UTC epoch)
  - `side`: 0/1=bid/ask update, 2=aggTrade, 3/4=bid/ask snapshot
  - `qty`: positive for book updates; preserves sign for trade events
    (negative = seller maker / buyer-aggressed)

The `hello` frame announces protocol_version=2 to subscribers; legacy
v1 subscribers that don't parse `hello` should ignore unknown
`kind` values and simply ignore the extra `symbol` field on data lines
(standard JSON forward-compat).

Heartbeat every `heartbeat_sec` seconds when no real events flowed.

Backpressure: subscribers whose write buffer exceeds
`max_subscriber_buffer_bytes` (default 1 MB) get dropped + force
reconnect — better than mixing fresh with stale. Cold-start gives only
live events, no history backfill (echo-air's FeatureBuilder warms over
~30 s of live events; long training windows use lustre1 cold tier).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Iterable

log = logging.getLogger(__name__)


PROTOCOL_VERSION = 2


class LivePublisher:
    """TCP/JSON-lines broadcaster — multi-venue + multi-symbol capable.

    A single publisher process handles many venues (e.g. `um` + `bs` on
    narci-sg) and many symbols on the same port; (venue_tag, symbol) is
    supplied per `fanout()` call so subscribers receive a single mixed
    stream and disambiguate by the `venue` and `symbol` fields in each
    line.

    Wire protocol v2 (2026-05-21): every data line carries both `venue`
    and `symbol`. Sends a one-shot `{"kind":"hello","protocol_version":2}`
    frame to each subscriber immediately on connection.
    """

    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        heartbeat_sec: float = 5.0,
        max_subscriber_buffer_bytes: int = 1_048_576,
    ):
        self.port = port
        self.host = host
        self.heartbeat_sec = heartbeat_sec
        self.max_buffer = max_subscriber_buffer_bytes
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._last_send_ts_ms: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_subscriber, host=self.host, port=self.port,
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        log.info(f"[live_publisher] listening on {self.host}:{self.port}")

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._subscribers):
            self._drop_subscriber(w)

    async def _handle_subscriber(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        self._subscribers.add(writer)
        log.info(f"[live_publisher] subscriber connected {peer} "
                  f"(total {len(self._subscribers)})")
        # v2: send hello frame so subscribers can negotiate protocol version.
        # v1 subscribers will see an unknown `kind` and (per JSON forward-compat
        # convention) ignore it — non-fatal.
        hello = (json.dumps(
            {"kind": "hello", "protocol_version": PROTOCOL_VERSION},
            separators=(",", ":")) + "\n").encode("utf-8")
        self._try_write(writer, hello)
        try:
            # Subscriber never sends us anything; just hold the socket
            # until they disconnect.
            await reader.read()
        finally:
            self._drop_subscriber(writer)
            log.info(f"[live_publisher] subscriber disconnected {peer} "
                      f"(remaining {len(self._subscribers)})")

    def _drop_subscriber(self, writer: asyncio.StreamWriter) -> None:
        self._subscribers.discard(writer)
        try:
            if not writer.transport.is_closing():
                writer.close()
        except Exception:
            pass

    def fanout(self, venue_tag: str, symbol: str,
                records: Iterable[Iterable]) -> None:
        """Push raw 4-tuple records `(ts_ms, side, price, qty)` tagged
        with `(venue_tag, symbol)` to every subscriber. Non-blocking —
        never awaits.

        `symbol` is the native exchange symbol (e.g. ``BTCUSDT``) and is
        emitted as the `symbol` field on every JSON line. Added 2026-05-21
        per echo D13 §18 to fix multi-symbol contamination — pre-v2 wire
        omitted symbol, so a venue_tag with N symbols collapsed N
        L2Reconstructor states on the subscriber side."""
        if not self._subscribers:
            return
        payload = bytearray()
        for r in records:
            ts_ms, side, price, qty = r[0], r[1], r[2], r[3]
            line = json.dumps({
                "venue": venue_tag,
                "symbol": symbol,
                "ts_ms": int(ts_ms),
                "side": int(side),
                "price": float(price),
                "qty": float(qty),
            }, separators=(",", ":")) + "\n"
            payload.extend(line.encode("utf-8"))
        if not payload:
            return
        self._last_send_ts_ms = int(time.time() * 1000)
        for w in list(self._subscribers):
            self._try_write(w, bytes(payload))

    def _try_write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            if writer.transport.is_closing():
                self._drop_subscriber(writer)
                return
            buf_size = writer.transport.get_write_buffer_size()
            if buf_size > self.max_buffer:
                log.warning(f"[live_publisher] slow subscriber "
                             f"(buf={buf_size}>{self.max_buffer}); dropping")
                self._drop_subscriber(writer)
                return
            writer.write(data)
        except Exception as e:
            log.warning(f"[live_publisher] write failed ({type(e).__name__}); "
                         f"dropping subscriber: {e}")
            self._drop_subscriber(writer)

    async def _heartbeat_loop(self) -> None:
        """Send a heartbeat frame every `heartbeat_sec` if no real events
        flowed during that interval. Lets subscribers reset their staleness
        timer on any frame."""
        while True:
            await asyncio.sleep(self.heartbeat_sec)
            now_ms = int(time.time() * 1000)
            if (now_ms - self._last_send_ts_ms) < self.heartbeat_sec * 1000:
                continue   # real events kept the stream warm
            if not self._subscribers:
                continue
            hb = (json.dumps({"kind": "heartbeat", "ts_ms": now_ms},
                              separators=(",", ":")) + "\n").encode("utf-8")
            self._last_send_ts_ms = now_ms
            for w in list(self._subscribers):
                self._try_write(w, hb)
