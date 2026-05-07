"""End-to-end OLS test: UM → CC via FeatureBuilder.

Goal: validate that narci.features.realtime.FeatureBuilder reproduces the
research-stage R² (~12% on trade target). If yes, production feature
pipeline is correct and nyx can use it directly.

Run:
    cd /lustre1/work/c30636/narci
    python -m research.ols_um_cc_e2e --days 20260420,20260421,20260422,20260423
"""

from __future__ import annotations

import argparse
import heapq
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from features import FeatureBuilder, FEATURE_NAMES


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

# (exchange, market, symbol, venue) — matches research/segmented_replay.py
VENUE_SOURCES: list[tuple[str, str, str, str]] = [
    ("coincheck",   "spot",       "BTC_JPY", "cc"),
    ("binance_jp",  "spot",       "BTCJPY",  "bj"),
    ("binance",     "um_futures", "BTCUSDT", "um"),
]

# UM keeps all sides — earlier optimization dropped 0/1 incrementals
# but that froze UM best_bid/ask between snapshots (~1/min), driving
# basis_um_bps to 39.8% NaN via the staleness gate. With segment-parallel
# we can afford the full 270M events/day.
UM_KEEP_SIDES = None  # None = keep everything


def stream_cold_file(path: Path, venue: str, batch_size: int = 100_000):
    """Stream events from one parquet shard as tuples. Yields:
        (ts_ms, neg_side_for_sort, venue, side, price, qty)
    The leading (ts, -side) ordering puts snapshots (3,4) before diffs at
    same ts when used in heapq.merge."""
    pf = pq.ParquetFile(str(path))
    for batch in pf.iter_batches(batch_size=batch_size):
        ts_arr = batch.column("timestamp").to_pylist()
        sd_arr = batch.column("side").to_pylist()
        pr_arr = batch.column("price").to_pylist()
        qt_arr = batch.column("quantity").to_pylist()
        for i in range(len(ts_arr)):
            sd = int(sd_arr[i])
            if venue == "um" and UM_KEEP_SIDES is not None and sd not in UM_KEEP_SIDES:
                continue
            yield (int(ts_arr[i]), -sd, venue, sd,
                   float(pr_arr[i]), float(qt_arr[i]))


def stream_days(days: list[str]):
    """heapq-merge stream over all (day, venue) cold tier files."""
    iters = []
    for d in days:
        for exchange, market, sym, venue in VENUE_SOURCES:
            path = COLD / exchange / market / f"{sym}_RAW_{d}_DAILY.parquet"
            if not path.exists():
                path = COLD / f"{sym}_RAW_{d}_DAILY.parquet"  # legacy fallback
            if path.exists():
                iters.append(stream_cold_file(path, venue))
            else:
                print(f"  ⚠️ missing {sym} {d}")
    yield from heapq.merge(*iters)


def build_day_features(day: str) -> dict:
    """Worker: stream one day's events through FeatureBuilder, sample at
    every CC trade. Returns dict with X (feature matrix), y_proxy (price
    series for later target construction), ts.

    Day-level parallelism — each worker is independent, no shared state."""
    fb = FeatureBuilder(lookback_seconds=300)
    ts_list: list[int] = []
    price_list: list[float] = []
    feats_list: list[list[float]] = []
    n_total = 0
    cc_trade_count = 0
    t0 = time.time()
    for ts_ms, _neg_side, venue, side, price, qty in stream_days([day]):
        n_total += 1
        fb.update_event(venue, ts_ms, side, price, qty)
        if venue == "cc" and side == 2:
            cc_trade_count += 1
            feats = fb.get_features(ts_ms)
            x = [feats.get(name, float("nan")) for name in FEATURE_NAMES]
            ts_list.append(ts_ms)
            price_list.append(price)
            feats_list.append(x)
    elapsed = time.time() - t0
    return {
        "day": day,
        "ts": np.asarray(ts_list, dtype=np.int64),
        "price": np.asarray(price_list, dtype=np.float64),
        "X": np.asarray(feats_list, dtype=np.float64),
        "n_events": n_total,
        "cc_trades": cc_trade_count,
        "elapsed_sec": elapsed,
    }


def run(days: list[str], train_frac: float = 0.75, n_workers: int = 4,
        use_segments: bool = False, segment_sec: int = 300,
        max_hours: float | None = None, start_hour: float = 0.0):
    print(f"=== OLS UM → CC end-to-end ({len(days)} days) ===")
    print(f"  features: {len(FEATURE_NAMES)} ({FEATURE_NAMES[:3]}...{FEATURE_NAMES[-3:]})")
    print()

    # --- Parallel feature extraction ---
    if use_segments:
        print(f"Streaming via SEGMENT-parallel ({segment_sec}s segments, "
              f"{n_workers} workers) ...")
        from .segmented_replay import replay_days_parallel
        seg_result = replay_days_parallel(
            days, n_workers=n_workers, segment_sec=segment_sec,
            max_hours_per_day=max_hours, start_hour=start_hour,
        )
        ts_all = seg_result["ts"]
        price_all = seg_result["price"]
        X_all = seg_result["X"]
        n_total = seg_result["n_events_total"]
        print(f"  streamed {n_total:,} events in {seg_result['elapsed_sec']:.1f}s wall "
              f"({n_total/seg_result['elapsed_sec']:,.0f}/sec aggregate)")
        print(f"  CC trades sampled: {len(ts_all):,}")
    else:
        n_workers_eff = min(n_workers, len(days))
        print(f"Streaming via DAY-parallel, {n_workers_eff} parallel workers ...")
        t0 = time.time()
        if n_workers_eff > 1:
            with mp.get_context("spawn").Pool(n_workers_eff) as pool:
                day_results = pool.map(build_day_features, days)
        else:
            day_results = [build_day_features(d) for d in days]
        pool_elapsed = time.time() - t0
        n_total = sum(r["n_events"] for r in day_results)
        cc_trade_count = sum(r["cc_trades"] for r in day_results)
        print(f"  streamed {n_total:,} events in {pool_elapsed:.1f}s wall "
              f"({n_total/pool_elapsed:,.0f} events/sec)")
        for r in day_results:
            print(f"    {r['day']}: {r['n_events']:>11,} events  "
                  f"{r['cc_trades']:>6,} CC trades  worker {r['elapsed_sec']:.1f}s")
        print(f"  CC trades sampled: {cc_trade_count:,}")
        day_results.sort(key=lambda r: r["day"])
        ts_all = np.concatenate([r["ts"] for r in day_results])
        price_all = np.concatenate([r["price"] for r in day_results])
        X_all = np.concatenate([r["X"] for r in day_results], axis=0)

    # --- Build target: log(cc_price[t+1s] / cc_price[t]) ---
    print()
    print("Building target vector (1s ahead CC trade log return) ...")
    n = len(ts_all)
    X_rows: list[np.ndarray] = []
    y_rows: list[float] = []
    # binary search: for each i, find first j with ts[j] >= ts[i] + 1000
    j = 0
    for i in range(n):
        target_ts = ts_all[i] + 1000
        if j <= i:
            j = i + 1
        while j < n and ts_all[j] < target_ts:
            j += 1
        if j >= n:
            continue
        p_now = price_all[i]
        p_next = price_all[j]
        if p_now <= 0 or p_next <= 0:
            continue
        x = X_all[i]
        if np.any(np.isnan(x)):
            continue
        X_rows.append(x)
        y_rows.append(math.log(p_next / p_now))

    X = np.asarray(X_rows)
    y = np.asarray(y_rows)
    print(f"  usable samples: {len(y):,}  (NaN dropped: {n - len(y):,})")
    print(f"  target σ (bps): {y.std() * 10000:.3f}" if len(y) else "")

    # Diagnostic: count NaN per feature in the FULL X_all (pre-NaN-drop)
    if X_all.shape[0] > 0:
        nan_counts = np.isnan(X_all).sum(axis=0)
        any_nan_rows = np.isnan(X_all).any(axis=1).sum()
        print(f"\n  NaN diagnostic: {any_nan_rows:,}/{X_all.shape[0]:,} rows have ≥1 NaN")
        for name, count in sorted(zip(FEATURE_NAMES, nan_counts), key=lambda x: -x[1])[:8]:
            if count > 0:
                pct = count / X_all.shape[0] * 100
                print(f"    {name:>26}: {count:>6,} NaN ({pct:>5.1f}%)")

    if len(y) < 100:
        print("❌ too few usable samples; check feature availability")
        return 1

    # --- 4) OLS train/test split ---
    print()
    print("OLS fit ...")
    n_samples = len(y)
    split = int(n_samples * train_frac)
    Xtr, Xte = X[:split], X[split:]
    ytr, yte = y[:split], y[split:]

    mu_x = Xtr.mean(0)
    mu_y = ytr.mean()
    Xtr_c = Xtr - mu_x
    Xte_c = Xte - mu_x
    ytr_c = ytr - mu_y
    yte_c = yte - mu_y

    beta, *_ = np.linalg.lstsq(Xtr_c, ytr_c, rcond=None)

    yhat_tr = Xtr_c @ beta
    yhat_te = Xte_c @ beta
    R2_tr = 1 - ((ytr_c - yhat_tr) ** 2).sum() / (ytr_c ** 2).sum()
    R2_te = 1 - ((yte_c - yhat_te) ** 2).sum() / (yte_c ** 2).sum()

    print(f"  train: {len(ytr):,} samples → R² = {R2_tr*100:.3f}%")
    print(f"  test:  {len(yte):,} samples → R² = {R2_te*100:.3f}%")
    print()

    # --- 5) Per-feature standardized importance ---
    print("=== Top features by |β·σ_x| (bps) ===")
    contribs = []
    for f, b in zip(FEATURE_NAMES, beta):
        c = b * Xtr[:, FEATURE_NAMES.index(f)].std() * 10000
        contribs.append((f, b, c))
    contribs.sort(key=lambda x: -abs(x[2]))
    for f, b, c in contribs[:10]:
        print(f"  {f:>22}  β·σ_x = {c:>+8.4f} bps")

    # --- 6) Verdict vs research baseline ---
    print()
    print("=== Verdict ===")
    if R2_te > 0.08:
        print(f"  ✅ test R² {R2_te*100:.2f}% above 8% — pipeline reproduces "
              "research-stage signal")
    elif R2_te > 0.04:
        print(f"  ⚠️  test R² {R2_te*100:.2f}% lower than expected (~12%) — "
              "feature drift possible; check FeatureBuilder vs research code")
    else:
        print(f"  ❌ test R² {R2_te*100:.2f}% too low — broken pipeline")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="20260420,20260421,20260422,20260423")
    ap.add_argument("--train-frac", type=float, default=0.75)
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel workers")
    ap.add_argument("--segments", action="store_true",
                    help="use snapshot-segment parallel replay (much faster "
                         "on big-core machines; default day-parallel)")
    ap.add_argument("--segment-sec", type=int, default=300,
                    help="segment length in seconds (default 300 = 5min)")
    ap.add_argument("--hours", type=float, default=None,
                    help="if set, only replay N hours of each day "
                         "(fast smoke; segment-mode only)")
    ap.add_argument("--start-hour", type=float, default=0.0,
                    help="skip first N hours of each day (avoid early-day "
                         "recorder outages). segment-mode only.")
    args = ap.parse_args()
    days = args.days.split(",")
    sys.exit(run(days, args.train_frac, args.workers,
                 use_segments=args.segments, segment_sec=args.segment_sec,
                 max_hours=args.hours, start_hour=args.start_hour))
