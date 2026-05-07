"""Debug: open a clean WS to Binance UM, see if aggTrade messages arrive.

Bypasses the recorder entirely — talks straight to Binance from inside
whatever environment you run it in. Useful for diagnosing the 0424+ UM
aggTrade silence: if this script gets messages, the recorder code is at
fault; if it times out, the issue is binance / network / endpoint.

Run inside the recorder container:
    docker compose exec recorder-binance-umfut python3 tools/test_um_aggtrade_ws.py

Or directly on a host with python3 + websockets installed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets


async def test_one(url: str, timeout_per_msg: int, n_msgs: int,
                    label: str) -> dict:
    print(f"\n--- {label} ---")
    print(f"URL: {url}")
    counts: dict[str, int] = {}
    t0 = time.time()
    try:
        async with websockets.connect(url) as ws:
            print("connected, listening...")
            for i in range(n_msgs):
                try:
                    raw = await asyncio.wait_for(ws.recv(),
                                                 timeout=timeout_per_msg)
                except asyncio.TimeoutError:
                    print(f"  msg {i+1}: TIMEOUT after {timeout_per_msg}s")
                    break
                msg = json.loads(raw)
                stream = msg.get("stream", "<no-stream>")
                etype = msg.get("data", {}).get("e", "<no-e>")
                key = f"{stream}|{etype}"
                counts[key] = counts.get(key, 0) + 1
                if i < 5 or i % 20 == 0:
                    data = msg.get("data", {})
                    summary = (f"price={data.get('p')} qty={data.get('q')} "
                               f"is_buyer_maker={data.get('m')}"
                               if etype == "aggTrade"
                               else f"levels b={len(data.get('b', []))} a={len(data.get('a', []))}")
                    print(f"  msg {i+1:>3}: {key:>40}  {summary}")
    except Exception as e:
        print(f"  connection error: {type(e).__name__}: {e}")
    elapsed = time.time() - t0
    print(f"  → {sum(counts.values())} msgs in {elapsed:.1f}s")
    print(f"  → counts: {counts}")
    return counts


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30,
                    help="messages per test (default 30)")
    ap.add_argument("--timeout", type=int, default=20,
                    help="per-message recv timeout in seconds")
    ap.add_argument("--symbol", default="btcusdt",
                    help="symbol to test (lower case)")
    args = ap.parse_args()

    sym = args.symbol.lower()

    # Test 1: aggTrade alone — isolates whether the trade stream itself is alive.
    url1 = f"wss://fstream.binance.com/stream?streams={sym}@aggTrade"
    counts1 = await test_one(url1, args.timeout, args.n,
                              f"AGGTRADE only, symbol={sym}")

    # Test 2: depth + aggTrade combined — same pattern as the recorder.
    url2 = f"wss://fstream.binance.com/stream?streams={sym}@depth@100ms/{sym}@aggTrade"
    counts2 = await test_one(url2, args.timeout, args.n,
                              f"DEPTH + AGGTRADE combo, symbol={sym}")

    print("\n=== Verdict ===")
    aggtrade_only_ok = any("aggTrade" in k for k in counts1)
    aggtrade_combo_ok = any("aggTrade" in k for k in counts2)
    if aggtrade_only_ok and aggtrade_combo_ok:
        print("  ✅ aggTrade flowing in both modes — recorder code bug")
    elif aggtrade_only_ok and not aggtrade_combo_ok:
        print("  ⚠️ aggTrade dropped when combined with depth — multiplex bug "
              "(URL too long? subscription limit?)")
    elif not aggtrade_only_ok:
        print("  ❌ aggTrade dead even alone — binance side or network issue")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
