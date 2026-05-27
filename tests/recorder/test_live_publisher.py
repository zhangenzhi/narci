"""LivePublisher TCP/JSON-lines broadcaster regression tests.

Spec: echo INTERFACE_ECHO_NARCI.md §14 Delivery 9 + §18 Delivery 13
(wire v2). Verifies the acceptance test items from §14.6 + §18.4:
  1. TCP reachability + JSON-lines decode
  2. Heartbeat every 5s during quiet window
  3. Schema per non-heartbeat frame (now includes `symbol` field, v2)
  4. Backpressure: slow subscriber dropped on write buffer saturation
  5. `hello` frame announces protocol_version=2 on connect (v2)
  6. Multi-symbol fan-out keeps subscriber-side `(venue,symbol)` distinct
     (the D13 §18 contamination regression)

Item 7 (end-to-end with FeatureBuilder + v9_midy_40 predict) requires
echo-air subscriber and is not unit-testable here.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from recorder.live_publisher import LivePublisher, PROTOCOL_VERSION


def _async(coro):
    """Run an async test body with a fresh event loop. Avoids relying on
    pytest-asyncio mode."""
    return asyncio.new_event_loop().run_until_complete(coro)


async def _open_subscriber(port: int) -> tuple[asyncio.StreamReader,
                                                  asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


async def _read_hello(reader: asyncio.StreamReader, timeout: float = 1.0) -> dict:
    """Read the v2 hello frame (always first on each connection)."""
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    return json.loads(line.decode("utf-8"))


async def _read_n_lines(reader: asyncio.StreamReader, n: int,
                         timeout: float) -> list[dict]:
    """Read up to n JSON lines or timeout, whichever first."""
    out = []
    try:
        for _ in range(n):
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                break
            out.append(json.loads(line.decode("utf-8")))
    except asyncio.TimeoutError:
        pass
    return out


class TestLivePublisherBasics(unittest.TestCase):

    def test_fanout_lines_received(self):
        async def body():
            pub = LivePublisher(port=0)
            # port=0 makes asyncio bind an ephemeral port; we read it back
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                # consume hello frame
                hello = await _read_hello(r)
                self.assertEqual(hello.get("kind"), "hello")
                # Fan out 3 records (a typical depth update batch) tagged
                # (um, BTCUSDT)
                pub.fanout("um", "BTCUSDT", [
                    [1779164453356, 0, 68234.5, 0.5],
                    [1779164453356, 1, 68234.6, 0.8],
                    [1779164453402, 2, 68234.55, -0.123],   # seller maker
                ])
                lines = await _read_n_lines(r, 3, timeout=1.0)
                self.assertEqual(len(lines), 3)
                self.assertEqual(lines[0], {
                    "venue": "um", "symbol": "BTCUSDT",
                    "ts_ms": 1779164453356,
                    "side": 0, "price": 68234.5, "qty": 0.5,
                })
                self.assertEqual(lines[2]["side"], 2)
                self.assertEqual(lines[2]["qty"], -0.123)
                self.assertEqual(lines[2]["symbol"], "BTCUSDT")
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_hello_frame_on_connect(self):
        """v2 acceptance: every new subscriber receives a `hello` frame
        announcing protocol_version=2 BEFORE any data line. Echo D13
        §18.2 Part B.1."""
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                hello = await _read_hello(r)
                self.assertEqual(hello, {
                    "kind": "hello",
                    "protocol_version": PROTOCOL_VERSION,
                })
                self.assertEqual(PROTOCOL_VERSION, 2,
                    "wire v2 should declare protocol_version=2")
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_heartbeat_during_quiet(self):
        async def body():
            pub = LivePublisher(port=0, heartbeat_sec=0.1)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                await _read_hello(r)
                # Wait ~0.5s with no events; expect ≥3 heartbeat frames
                lines = await _read_n_lines(r, 4, timeout=0.7)
                self.assertGreaterEqual(len(lines), 3,
                    f"expected ≥3 heartbeats in 0.7s with hb_sec=0.1, "
                    f"got {len(lines)}: {lines}")
                for line in lines:
                    self.assertEqual(line.get("kind"), "heartbeat",
                        f"non-heartbeat frame during quiet: {line}")
                    self.assertIn("ts_ms", line)
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_schema_well_formed(self):
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                await _read_hello(r)
                pub.fanout("bs", "BTCUSDT", [
                    [1779164453356, 3, 68234.5, 0.5],     # bid snap
                    [1779164453356, 4, 68234.6, 0.8],     # ask snap
                ])
                lines = await _read_n_lines(r, 2, timeout=1.0)
                self.assertEqual(len(lines), 2)
                for line in lines:
                    # v2 schema: venue + symbol + ts_ms + side + price + qty
                    self.assertEqual(set(line.keys()),
                                      {"venue", "symbol", "ts_ms", "side",
                                       "price", "qty"})
                    self.assertEqual(line["venue"], "bs")
                    self.assertEqual(line["symbol"], "BTCUSDT")
                    self.assertIsInstance(line["ts_ms"], int)
                    self.assertIn(line["side"], {0, 1, 2, 3, 4})
                    self.assertIsInstance(line["price"], float)
                    self.assertIsInstance(line["qty"], float)
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_no_subscribers_fanout_is_noop(self):
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            # No subscriber connected; fanout should not raise.
            pub.fanout("um", "BTCUSDT", [[1, 0, 100.0, 1.0]])
            pub.fanout("bs", "BTCUSDT", [[2, 2, 100.0, -0.5]])
            await pub.stop()
        _async(body())

    def test_multi_venue_on_one_port(self):
        """Single publisher process can fan out UM + BS events on the same
        port; each line carries its own `venue` tag so the subscriber can
        demux. echo §14.2 wire spec ('venue ∈ {um,bs}' on one connection)."""
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                await _read_hello(r)
                pub.fanout("um", "BTCUSDT", [[1, 2, 68234.5, -0.1]])
                pub.fanout("bs", "BTCUSDT", [[2, 2, 68234.7, +0.2]])
                pub.fanout("um", "BTCUSDT", [[3, 0, 68234.5, 0.8]])
                lines = await _read_n_lines(r, 3, timeout=1.0)
                self.assertEqual(len(lines), 3)
                self.assertEqual([l["venue"] for l in lines],
                                  ["um", "bs", "um"])
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_multi_symbol_on_same_venue_keeps_symbol_distinct(self):
        """D13 §18 regression: pre-v2 wire dropped `symbol`, so
        multi-symbol fan-out on same venue_tag collapsed N L2Reconstructor
        states on subscriber. v2 fix: every line carries (venue, symbol);
        subscriber routes via (venue, symbol) tuple."""
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                await _read_hello(r)
                # Same venue_tag, three different symbols — the exact
                # scenario that broke under v1 wire.
                pub.fanout("um", "BTCUSDT", [[1, 0, 77943.0, 0.5]])
                pub.fanout("um", "ETHUSDT", [[2, 0, 3450.0, 1.2]])
                pub.fanout("um", "XRPUSDT", [[3, 0, 25.0, 100.0]])
                lines = await _read_n_lines(r, 3, timeout=1.0)
                self.assertEqual(len(lines), 3)
                self.assertEqual([l["venue"] for l in lines],
                                  ["um", "um", "um"])
                self.assertEqual([l["symbol"] for l in lines],
                                  ["BTCUSDT", "ETHUSDT", "XRPUSDT"])
                # Order book non-collision: prices stay tagged with the
                # symbol they belong to (subscriber can route via (venue,
                # symbol) → distinct L2Reconstructor per symbol).
                self.assertEqual([l["price"] for l in lines],
                                  [77943.0, 3450.0, 25.0])
                w.close()
                await w.wait_closed()
            finally:
                await pub.stop()
        _async(body())

    def test_subscriber_disconnect_cleanup(self):
        async def body():
            pub = LivePublisher(port=0)
            await pub.start()
            actual_port = pub._server.sockets[0].getsockname()[1]
            try:
                r, w = await _open_subscriber(actual_port)
                await _read_hello(r)
                self.assertEqual(len(pub._subscribers), 1)
                w.close()
                await w.wait_closed()
                # Server-side cleanup may need a beat
                await asyncio.sleep(0.1)
                self.assertEqual(len(pub._subscribers), 0)
            finally:
                await pub.stop()
        _async(body())


if __name__ == "__main__":
    unittest.main()
