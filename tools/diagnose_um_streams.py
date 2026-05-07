"""Comprehensive UM WebSocket stream diagnostic.

Tests multiple stream types in isolation to figure out exactly what's
blocked on this host:
  - depth (control: known to work)
  - aggTrade (the suspect)
  - trade (per-trade, NOT aggregated)
  - markPrice (mark price stream)
  - kline_1m (1-minute kline)
  - bookTicker (best bid/ask)

Also tries different symbols and different URL patterns to isolate:
  - is it specific to BTCUSDT or all symbols?
  - is it /stream multiplex vs /ws raw?

Run inside the recorder container:
    docker cp tools/diagnose_um_streams.py narci-recorder-binance-umfut:/tmp/diag.py
    docker compose exec recorder-binance-umfut python3 /tmp/diag.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import websockets


TIMEOUT_PER_TEST = 15  # seconds to wait for first message
MIN_MSGS = 1           # at least 1 msg = pass


async def probe(label: str, url: str, expected_stream_kw: str) -> str:
    """Return one of: PASS / TIMEOUT / NO_MATCH / ERROR:<msg>."""
    print(f"\n--- {label} ---")
    print(f"  URL: {url}")
    t0 = time.time()
    try:
        async with websockets.connect(url) as ws:
            received = 0
            matched = 0
            while time.time() - t0 < TIMEOUT_PER_TEST:
                try:
                    raw = await asyncio.wait_for(ws.recv(),
                                                  timeout=TIMEOUT_PER_TEST - (time.time() - t0))
                except asyncio.TimeoutError:
                    break
                received += 1
                msg = json.loads(raw)
                stream = msg.get("stream") or msg.get("e", "")
                etype = msg.get("data", {}).get("e", "") if "data" in msg else msg.get("e", "")
                if expected_stream_kw in stream or expected_stream_kw in etype:
                    matched += 1
                    if matched == 1:
                        # Print first matching message
                        d = msg.get("data", msg)
                        print(f"  first match: stream={stream} etype={etype}")
                        print(f"    sample fields: {sorted(d.keys()) if isinstance(d, dict) else type(d)}")
                if received >= 50:
                    break
            elapsed = time.time() - t0
            verdict = ("PASS" if matched >= MIN_MSGS
                       else ("NO_MATCH" if received > 0 else "TIMEOUT"))
            print(f"  → received={received} matched={matched} in {elapsed:.1f}s → {verdict}")
            return verdict
    except Exception as e:
        verdict = f"ERROR:{type(e).__name__}:{e}"
        print(f"  → {verdict}")
        return verdict


async def main():
    BASE = "wss://fstream.binance.com"

    tests = [
        # (label, url, expected_kw)
        ("01. depth combined-stream (control)",
         f"{BASE}/stream?streams=btcusdt@depth@100ms",
         "depth"),
        ("02. depth raw /ws (control)",
         f"{BASE}/ws/btcusdt@depth@100ms",
         "depthUpdate"),
        ("03. aggTrade combined-stream",
         f"{BASE}/stream?streams=btcusdt@aggTrade",
         "aggTrade"),
        ("04. aggTrade raw /ws",
         f"{BASE}/ws/btcusdt@aggTrade",
         "aggTrade"),
        ("05. @trade (per-trade, NOT aggregated)",
         f"{BASE}/ws/btcusdt@trade",
         "trade"),
        ("06. @markPrice",
         f"{BASE}/ws/btcusdt@markPrice",
         "markPriceUpdate"),
        ("07. @kline_1m",
         f"{BASE}/ws/btcusdt@kline_1m",
         "kline"),
        ("08. @bookTicker",
         f"{BASE}/ws/btcusdt@bookTicker",
         "bookTicker"),
        ("09. ETHUSDT @aggTrade (different symbol)",
         f"{BASE}/ws/ethusdt@aggTrade",
         "aggTrade"),
        ("10. SOLUSDT @aggTrade (smaller symbol)",
         f"{BASE}/ws/solusdt@aggTrade",
         "aggTrade"),
        ("11. depth + aggTrade COMBO (recorder pattern)",
         f"{BASE}/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",
         "aggTrade"),
        ("12. spot aggTrade (different host)",
         "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
         "aggTrade"),
        # 2026-04-23 binance restructured futures WS endpoints into
        # /public, /market, /private categories. aggTrade now requires
        # /market/ prefix; old /ws and /stream paths got deprecated.
        ("13. NEW /market/ws aggTrade (post-2026-04-23 endpoint)",
         f"{BASE}/market/ws/btcusdt@aggTrade",
         "aggTrade"),
        ("14. NEW /market/stream aggTrade combined",
         f"{BASE}/market/stream?streams=btcusdt@aggTrade",
         "aggTrade"),
        ("15. NEW /market/stream depth+aggTrade combo",
         f"{BASE}/market/stream?streams=btcusdt@depth@100ms/btcusdt@aggTrade",
         "aggTrade"),
        ("16. NEW /public/stream depth (control for new layout)",
         f"{BASE}/public/stream?streams=btcusdt@depth@100ms",
         "depth"),
    ]

    results = {}
    for label, url, kw in tests:
        results[label] = await probe(label, url, kw)
        # short pause between tests so we don't trip rate limits
        await asyncio.sleep(1)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for label, verdict in results.items():
        marker = "✅" if verdict == "PASS" else "❌"
        print(f"  {marker} {label[:55]:<55}  {verdict}")

    # Diagnostic logic
    print("\n=== Interpretation ===")
    pass_set = {l for l, v in results.items() if v == "PASS"}
    if results["01. depth combined-stream (control)"] != "PASS":
        print("  ⚠️ depth control failed too — basic connectivity to fstream broken")
        return
    aggtrade_failures = [l for l, v in results.items()
                         if "aggTrade" in l and v != "PASS"]
    other_streams_pass = [l for l in pass_set
                          if "aggTrade" not in l and "depth" not in l]
    other_aggtrade_streams = ["05. @trade (per-trade, NOT aggregated)"]
    trade_works = results.get(other_aggtrade_streams[0]) == "PASS"

    if not aggtrade_failures and trade_works:
        print("  All streams work — original recorder bug must be elsewhere")
    elif aggtrade_failures and trade_works:
        print("  ❌ aggTrade specifically blocked, but @trade works → "
              "switch recorder to use @trade instead of @aggTrade")
    elif aggtrade_failures and not trade_works:
        if other_streams_pass:
            print("  ❌ trade-related streams (aggTrade + trade) blocked, "
                  "but other streams (markPrice, kline, etc) pass → "
                  "Binance restricts execution data from this IP")
        else:
            print("  ❌ Only depth passes; everything else blocked → "
                  "near-total restriction from this IP region")
    if results.get("12. spot aggTrade (different host)") == "PASS":
        print("  ℹ️  spot aggTrade WORKS → it's specifically futures (fstream)")
    elif results.get("12. spot aggTrade (different host)") == "TIMEOUT":
        print("  ℹ️  spot aggTrade also blocked → broader binance restriction")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
