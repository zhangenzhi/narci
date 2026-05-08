"""Probe Binance UM /market vs /stream endpoints from inside the recorder container.

Run from AWS-SG host:
    git pull
    docker cp tools/probe_um_endpoints.py narci-recorder-binance-umfut:/tmp/p.py
    docker exec narci-recorder-binance-umfut python /tmp/p.py

Reads 5 messages (8s timeout each) from each endpoint. If /market silent and
/stream produces — recorder fix targeted wrong endpoint. If both silent —
AWS-SG egress blocked. If both work — recorder hung; restart fixes it.
"""
from __future__ import annotations

import asyncio

import websockets


ENDPOINTS = [
    ("/market",        "wss://fstream.binance.com/market/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
    ("/stream",        "wss://fstream.binance.com/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
    ("/public",        "wss://fstream.binance.com/public/stream?streams=btcusdt@aggTrade/btcusdt@depth"),
]


async def probe(label: str, url: str, n: int = 5, recv_timeout: float = 8.0) -> None:
    print(f"=== {label}: {url} ===", flush=True)
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            for i in range(n):
                msg = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                print(f"  [{i}] {msg[:220]}", flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)


async def main() -> None:
    for label, url in ENDPOINTS:
        await probe(label, url)


if __name__ == "__main__":
    asyncio.run(main())
