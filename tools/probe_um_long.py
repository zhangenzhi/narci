"""Long-window UM endpoint probe: count depth vs aggTrade events over 30s.

Tells us decisively whether /public or /stream still delivers BOTH
event types (combined mode) or only one. This decides the fix:
  - both on one endpoint → revert URL, single WS
  - split → dual-WS architecture (one /public for depth + one /market for trade)

Run inside container:
    docker cp tools/probe_um_long.py narci-recorder-binance-umfut:/tmp/pl.py
    docker exec narci-recorder-binance-umfut python /tmp/pl.py
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter

import websockets


ENDPOINTS = [
    ("/public", "wss://fstream.binance.com/public/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
    ("/stream", "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
    ("/market", "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
]

DURATION_SEC = 30


async def probe(label: str, url: str) -> None:
    print(f"=== {label}: {url} (run {DURATION_SEC}s) ===", flush=True)
    counts: Counter[str] = Counter()
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            deadline = asyncio.get_event_loop().time() + DURATION_SEC
            while True:
                remain = deadline - asyncio.get_event_loop().time()
                if remain <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remain)
                except asyncio.TimeoutError:
                    break
                try:
                    j = json.loads(msg)
                    stream = j.get("stream", "?")
                    counts[stream] += 1
                except Exception:
                    counts["__parse_error__"] += 1
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        return

    total = sum(counts.values())
    print(f"  total messages: {total}", flush=True)
    for stream, n in sorted(counts.items()):
        print(f"  {stream}: {n}  ({100*n/total:.1f}%)", flush=True)


async def main() -> None:
    for label, url in ENDPOINTS:
        await probe(label, url)


if __name__ == "__main__":
    asyncio.run(main())
