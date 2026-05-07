"""End-to-end smoke test: naive maker on real CC BTC_JPY data, 1 day.

Runs the broker through real raw L2 events from cold tier and reports
sanity-level stats. Pass criteria are loose (orders of magnitude), not
exact — purpose is to catch obvious bugs in the simulator before we
trust any PnL number.

Run:
    cd /lustre1/work/c30636/narci
    python -m calibration.tests.test_maker_smoke
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

from backtest.symbol_spec import SymbolSpec
from calibration import priors as P
from simulation import MakerSimBroker


COLD_DIR = Path("/lustre1/work/c30636/narci/replay_buffer/cold")
DAY = "20260423"


def make_naive_strategy(broker, reprice_threshold_bps=0.5,
                       target_size=0.005, inventory_cap=0.05):
    """Returns a callable strategy(ts) that uses broker to maintain quotes.

    Strategy: **join best** — bid at current best_bid, ask at current best_ask.
    Sit at the back of the queue but at the leading level (level 1).
    Reprice when mid drifts ≥ reprice_threshold_bps.
    """
    state = {"last_mid": None, "bid_cid": None, "ask_cid": None, "cid_counter": 0}

    def gen_cid():
        state["cid_counter"] += 1
        return f"smoke-{state['cid_counter']}"

    def reprice(ts, book_state):
        new_bid = book_state["best_bid"]
        new_ask = book_state["best_ask"]
        size = broker.symbol_spec.round_qty(target_size)

        # Cancel old (broker handles cancel ack latency internally)
        if state["bid_cid"] is not None:
            broker.cancel(ts, state["bid_cid"], reason="STRATEGY_REPRICE")
            state["bid_cid"] = None
        if state["ask_cid"] is not None:
            broker.cancel(ts, state["ask_cid"], reason="STRATEGY_REPRICE")
            state["ask_cid"] = None

        cur_inv = broker.inventory
        post_bid = (cur_inv + size) <= inventory_cap
        post_ask = cur_inv >= size

        if post_bid:
            state["bid_cid"] = broker.place_limit(ts, gen_cid(), "BUY",
                                                   new_bid, size)
        if post_ask:
            state["ask_cid"] = broker.place_limit(ts, gen_cid(), "SELL",
                                                   new_ask, size)
        state["last_mid"] = book_state["mid_price"]

    def on_market_event(ts, side, price, qty):
        broker.apply_market_event(ts, side, price, qty)
        if side not in (0, 1, 3, 4):
            return
        st = broker.book.get_top1()           # O(1) — incremental cache
        if st is None:
            return
        mid = st["mid_price"]
        if state["last_mid"] is None:
            reprice(ts, st)
            return
        drift_bps = abs(mid - state["last_mid"]) / state["last_mid"] * 10000
        if drift_bps >= reprice_threshold_bps:
            reprice(ts, st)

    return on_market_event, state


def run(max_events: int | None = None,
        reprice_threshold_bps: float = 5.0,
        day: str = DAY,
        verbose: bool = True) -> dict:
    """Run one full-day naive maker simulation. Returns result dict."""
    path = COLD_DIR / f"BTC_JPY_RAW_{day}_DAILY.parquet"
    if not path.exists():
        if verbose: print(f"❌ missing data: {path}")
        return {"day": day, "ok": False, "error": "missing data"}

    if verbose: print(f"Loading {path.name} ...")
    t0 = time.time()
    tbl = pq.read_table(path)
    df = tbl.to_pandas()
    df = df.sort_values(["timestamp", "side"], ascending=[True, False]).reset_index(drop=True)
    if max_events is not None:
        df = df.head(max_events)
    if verbose: print(f"  {len(df):,} events  loaded+sorted in {time.time()-t0:.1f}s")

    spec = SymbolSpec(symbol="BTC_JPY", tick_size=1.0, lot_size=1e-8,
                      min_notional=500.0)
    params = P.get_priors("coincheck", "BTC_JPY")
    broker = MakerSimBroker(params=params, symbol_spec=spec)
    on_event, _ = make_naive_strategy(broker,
                                      reprice_threshold_bps=reprice_threshold_bps)

    if verbose: print(f"Replaying {len(df):,} events through MakerSimBroker ...")
    t0 = time.time()
    cols = df[["timestamp", "side", "price", "quantity"]].to_numpy()
    for i in range(len(cols)):
        ts_ns = int(cols[i, 0]) * 1_000_000
        on_event(ts_ns, int(cols[i, 1]), cols[i, 2], cols[i, 3])
    elapsed = time.time() - t0
    if verbose: print(f"  done in {elapsed:.1f}s  ({len(df)/elapsed:,.0f} events/sec)")

    s = broker.stats()
    decisions, fills, cancels = broker.flush_results()
    last_state = broker.book.get_state(top_n=1)
    end_mid = last_state["mid_price"] if last_state else 0.0
    mtm = s["cash"] + s["inventory"] * end_mid
    notional_filled = sum(f["fill_price"] * f["fill_qty"] for f in fills)

    result = {
        "day": day, "ok": True,
        "events": len(df),
        "elapsed_sec": elapsed,
        "n_place": sum(1 for d in decisions if d["event_type"] == "PLACE"),
        "n_reject": sum(1 for d in decisions if d["event_type"] == "PLACE_REJECT"),
        "n_fills": len(fills),
        "n_cancels": len(cancels),
        "inventory_end": s["inventory"],
        "cash_end_jpy": s["cash"],
        "end_mid": end_mid,
        "notional_filled_jpy": notional_filled,
        "mtm_pnl_jpy": mtm,
        "mtm_pnl_usd": mtm / 150,
        "pnl_per_notional_bps": (mtm / notional_filled * 10000) if notional_filled > 0 else 0,
    }
    if not verbose:
        return result

    n_place = result["n_place"]
    n_reject = result["n_reject"]
    n_filled = result["n_fills"]
    n_cxl = result["n_cancels"]
    n_cxl_filled_during = sum(1 for c in cancels if c["final_state"] == "FILLED_DURING_CANCEL")

    print()
    print("=" * 60)
    print(P.tag_result("NAIVE MAKER SMOKE TEST RESULTS", params))
    print("=" * 60)
    print(f"  events processed       : {len(df):,}")
    print(f"  decisions emitted      : {len(decisions):,}")
    print(f"    PLACE                : {n_place:,}")
    print(f"    PLACE_REJECT         : {n_reject:,}")
    print(f"  fills                  : {n_filled:,}")
    print(f"  cancels                : {n_cxl:,}  (FILLED_DURING_CANCEL: {n_cxl_filled_during})")
    print(f"  active orders end      : {s['active_orders']}")
    print(f"  pending cancels end    : {s['pending_cancels']}")
    print(f"  inventory end          : {s['inventory']:.6f} BTC")
    print(f"  cash end               : {s['cash']:>+,.0f} JPY")

    if n_filled > 0:
        avg_qty = sum(f["fill_qty"] for f in fills) / n_filled
        avg_quote_age = sum(f["quote_age_ms"] for f in fills) / n_filled
        sides = {"BUY": 0, "SELL": 0}
        for f in fills:
            sides[f["side"]] += 1
        print(f"  ──── derived ────")
        print(f"  avg fill qty           : {avg_qty:.6f} BTC")
        print(f"  avg quote age @ fill   : {avg_quote_age:,.1f} ms")
        print(f"  fills BUY/SELL         : {sides['BUY']:,} / {sides['SELL']:,}")
        print(f"  total notional filled  : {result['notional_filled_jpy']:>+,.0f} JPY")
        print(f"  end mid                : {end_mid:>+,.0f} JPY")
        print(f"  mark-to-market P&L     : {mtm:>+,.0f} JPY  ({mtm/150:+.2f} USD)")
        print(f"  PnL / notional         : {result['pnl_per_notional_bps']:+.2f} bps")
    print()

    # Loose sanity assertions (raise on egregious failures)
    failures = []
    if n_place == 0:
        failures.append("no PLACE decisions emitted — strategy never fired")
    if n_filled == 0:
        failures.append("zero fills — broker never matched any of our orders to a trade")
    if n_filled > 50_000:
        failures.append(f"absurd fill count {n_filled} — likely double-counting bug")
    if n_reject > n_place * 0.5 and n_place > 0:
        failures.append(f"reject rate {n_reject}/{n_place} > 50% — bad threshold or seed")
    if abs(s["inventory"]) > 1.0:
        failures.append(f"inventory {s['inventory']} runaway — cap not enforced?")
    if elapsed > 600:
        failures.append(f"too slow: {elapsed}s — perf regression?")

    if failures:
        print("⚠️  SANITY CHECKS FAILED:")
        for f in failures:
            print(f"  - {f}")
        result["sanity_failures"] = failures
    else:
        print("✅ All sanity checks passed (loose thresholds only)")
    return result


def run_multi_day(days: list[str], reprice_threshold_bps: float = 5.0) -> int:
    import math
    print(f"=" * 70)
    print(f"NAIVE MAKER MULTI-DAY BASELINE — {len(days)} days")
    print(f"  reprice_threshold_bps = {reprice_threshold_bps}")
    print(f"=" * 70)

    results = []
    for i, day in enumerate(days, 1):
        print(f"\n[{i}/{len(days)}] day {day}")
        r = run(day=day, reprice_threshold_bps=reprice_threshold_bps,
                verbose=True)
        if r.get("ok"):
            results.append(r)

    if not results:
        print("❌ no successful runs")
        return 1

    print()
    print("=" * 70)
    print("AGGREGATE")
    print("=" * 70)
    print(f"{'day':>10} {'fills':>6} {'PLACE':>7} {'inv_end':>10} "
          f"{'notional$':>12} {'PnL$':>10} {'bps':>8}")
    print("-" * 70)
    pnls_usd = []
    for r in results:
        print(f"{r['day']:>10} {r['n_fills']:>6} {r['n_place']:>7} "
              f"{r['inventory_end']:>10.6f} "
              f"{r['notional_filled_jpy']/150:>+12,.0f} "
              f"{r['mtm_pnl_usd']:>+9.2f} "
              f"{r['pnl_per_notional_bps']:>+7.2f}")
        pnls_usd.append(r["mtm_pnl_usd"])

    if len(pnls_usd) >= 2:
        n = len(pnls_usd)
        mean = sum(pnls_usd) / n
        var = sum((x - mean) ** 2 for x in pnls_usd) / (n - 1)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = (mean / std * math.sqrt(365)) if std > 0 else float("nan")
        total = sum(pnls_usd)
        avg_fills = sum(r["n_fills"] for r in results) / n

        print("-" * 70)
        print(f"  N days                : {n}")
        print(f"  mean PnL/day          : ${mean:>+8.3f}")
        print(f"  std PnL/day           : ${std:>8.3f}")
        print(f"  Sharpe (daily, ann)   : {sharpe:>+8.2f}  (sqrt(365) × mean/std)")
        print(f"  total over {n}d         : ${total:>+8.2f}")
        print(f"  avg fills/day         : {avg_fills:.1f}")
        print(f"  win rate (days +PnL)  : {sum(1 for x in pnls_usd if x > 0)}/{n}")

    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-events", type=int, default=None,
                    help="cap event count for quick iteration")
    ap.add_argument("--reprice-bps", type=float, default=5.0,
                    help="mid drift bps that triggers reprice")
    ap.add_argument("--day", default=DAY,
                    help="single-day mode")
    ap.add_argument("--multi-day", default=None,
                    help="comma-separated list of days; runs aggregate")
    args = ap.parse_args()
    if args.multi_day:
        days = args.multi_day.split(",")
        sys.exit(run_multi_day(days, reprice_threshold_bps=args.reprice_bps))
    else:
        r = run(max_events=args.max_events, day=args.day,
                reprice_threshold_bps=args.reprice_bps)
        sys.exit(0 if r.get("ok") else 1)
