"""Standalone live-event publisher — narci-sg → echo-air channel.

Spec: echo INTERFACE_ECHO_NARCI.md §14 Delivery 9 (2026-05-19 pivot).

This process is INDEPENDENT from the recorder. It opens its own WS
connections to Binance (UM + spot), translates messages via the same
ExchangeAdapter layer, and broadcasts JSON-lines events over TCP to
subscribed clients. No interaction with cold tier, parquet, recorder
state, or shared on-disk files — pure event forwarding.

Why a separate process rather than a recorder hook:
- Decoupled failure modes: publisher crash doesn't risk cold-tier
  data integrity, and vice versa.
- No coupling to recorder's alignment / snapshot-injection / book
  state machine. The wire contract is raw event records; alignment
  is a backtest concern, not a live-subscriber concern.
- Independently restartable / scalable / config-versioned.

Usage:
    python -m data.event_publisher --config configs/event_publisher.yaml

Config schema (yaml):
    publisher:
      port: 9100
      host: 0.0.0.0          # optional, defaults 0.0.0.0
      heartbeat_sec: 5        # optional
      max_subscriber_buffer_bytes: 1048576   # optional, 1 MB
    venues:
      - venue_tag: um         # echo-side venue name (`um` / `bs` etc)
        exchange: binance     # narci ExchangeAdapter family
        market_type: um_futures
        symbols: [BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT, DOGEUSDT]
        interval_ms: 100      # optional, depth refresh frequency hint
      - venue_tag: bs
        exchange: binance
        market_type: spot
        symbols: [BTCUSDT, ETHUSDT]   # only basis-relevant pairs
        interval_ms: 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time

import yaml

from data.exchange import get_adapter
from data.live_publisher import LivePublisher

log = logging.getLogger(__name__)


async def _stream_venue(
    venue_tag: str,
    exchange: str,
    market_type: str,
    symbols: list[str],
    interval_ms: int,
    publisher: LivePublisher,
    retry_wait: float = 5.0,
) -> None:
    """One async task per WS URL. Reconnects on failure with backoff.

    Mirrors `L2Recorder.record_stream` minus the recorder buffer and
    alignment state machine — we just translate WS msg → records →
    publisher.fanout."""
    import websockets  # lazy import
    adapter = get_adapter(exchange, market_type=market_type)
    urls = adapter.ws_urls(symbols, interval_ms)
    log.info(f"[publisher:{venue_tag}] {exchange}/{market_type} "
              f"symbols={symbols} → {len(urls)} WS URL(s)")

    async def _one_url(url: str) -> None:
        while True:
            try:
                log.info(f"[publisher:{venue_tag}] connect {url[:120]}...")
                async with websockets.connect(url) as ws:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        sym, event_type, data = adapter.parse_message(msg)
                        if event_type not in ("depth", "trade"):
                            continue
                        records = adapter.standardize_event(event_type, data)
                        # 2026-05-21 (echo D13 §18 F2): pass `sym` from
                        # parse_message to fanout — wire v2 carries per-record
                        # symbol so multi-symbol fan-out no longer collapses
                        # on subscriber side.
                        if records:
                            publisher.fanout(venue_tag, sym, records)
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception as e:
                log.warning(f"[publisher:{venue_tag}] WS error "
                             f"{type(e).__name__}: {e}; retry in {retry_wait}s")
                await asyncio.sleep(retry_wait)

    await asyncio.gather(*(_one_url(u) for u in urls))


def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def run(config: dict) -> None:
    pub_cfg = config.get("publisher", {})
    publisher = LivePublisher(
        port=int(pub_cfg.get("port", 9100)),
        host=str(pub_cfg.get("host", "0.0.0.0")),
        heartbeat_sec=float(pub_cfg.get("heartbeat_sec", 5.0)),
        max_subscriber_buffer_bytes=int(
            pub_cfg.get("max_subscriber_buffer_bytes", 1_048_576)),
    )
    await publisher.start()

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        log.info(f"[publisher] received {sig.name}, shutting down")
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    venue_tasks = []
    for venue_cfg in config.get("venues", []):
        venue_tasks.append(asyncio.create_task(_stream_venue(
            venue_tag=str(venue_cfg["venue_tag"]),
            exchange=str(venue_cfg["exchange"]),
            market_type=str(venue_cfg["market_type"]),
            symbols=list(venue_cfg["symbols"]),
            interval_ms=int(venue_cfg.get("interval_ms", 100)),
            publisher=publisher,
        )))
    if not venue_tasks:
        log.error("[publisher] no venues configured; exiting")
        await publisher.stop()
        return

    log.info(f"[publisher] running with {len(venue_tasks)} venue task(s)")
    try:
        await shutdown.wait()
    finally:
        for t in venue_tasks:
            t.cancel()
        for t in venue_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await publisher.stop()
        log.info("[publisher] shutdown complete")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="narci live event publisher (echo D9 channel)")
    parser.add_argument("--config", type=str,
                        default="configs/event_publisher.yaml",
                        help="publisher config YAML")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = _load_config(args.config)
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
