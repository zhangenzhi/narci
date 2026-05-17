"""Diag: segmented_replay cache vs raw parquet asymmetry for CC trades.

Context: nyx 2026-05-17 reports that narci `segmented_replay` output of
CC trade events vs raw parquet `side==2` events have only ~40% ts overlap
on 04-17, with y distribution mean shift -0.4 bps and tail ratio 1.0×→1.7×.

This script reproduces the comparison empirically on a chosen day and
dumps a diagnostic report. Run as:
    python -m research.diag_segreplay_vs_raw 20260417

Tests several hypotheses:
1. Cardinality: do raw vs cache emit same N events?
2. Set overlap: how many cache ts appear in raw side==2 ts set?
3. Where are cache-extra ts coming from? (snapshot side 3/4? other side?
   ts shift?)
4. Where are raw-extra ts going? (dropped by segment boundary? warmup
   filter?)
5. y distribution: forward 1s trade-price log-return on both sets.

Output: a single-screen summary table + tail of mismatched ts examples.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from research.segmented_replay import (
    COLD, replay_days_parallel, discover_day_ts_range
)


def read_raw_cc_parquet(day: str) -> dict:
    """Read CC cold-tier parquet; return arrays of ts/side/price/qty
    plus per-side ts sets."""
    path = COLD / "coincheck" / "spot" / f"BTC_JPY_RAW_{day}_DAILY.parquet"
    tbl = pq.read_table(str(path))
    ts = np.asarray(tbl.column("timestamp").to_pylist(), dtype=np.int64)
    sd = np.asarray(tbl.column("side").to_pylist(), dtype=np.int8)
    pr = np.asarray(tbl.column("price").to_pylist(), dtype=np.float64)
    qt = np.asarray(tbl.column("quantity").to_pylist(), dtype=np.float64)
    return {
        "ts": ts, "side": sd, "price": pr, "qty": qt,
        "n_total": len(ts),
        "n_side0": int((sd == 0).sum()),
        "n_side1": int((sd == 1).sum()),
        "n_side2": int((sd == 2).sum()),
        "n_side3": int((sd == 3).sum()),
        "n_side4": int((sd == 4).sum()),
        "ts_sorted": bool(np.all(np.diff(ts) >= 0)),
        "ts_first": int(ts[0]),
        "ts_last": int(ts[-1]),
        "ts_min": int(ts.min()),
        "ts_max": int(ts.max()),
    }


def compute_forward_log_return(ts: np.ndarray, price: np.ndarray,
                                horizon_ms: int = 1000) -> np.ndarray:
    """For each event i, find first j with ts[j] >= ts[i] + horizon_ms;
    return y = log(price[j] / price[i]) in bps. NaN where no future event."""
    n = len(ts)
    y = np.full(n, np.nan, dtype=np.float64)
    j = 0
    for i in range(n):
        target_ts = ts[i] + horizon_ms
        if j <= i:
            j = i + 1
        while j < n and ts[j] < target_ts:
            j += 1
        if j >= n:
            continue
        if price[i] > 0 and price[j] > 0:
            y[i] = np.log(price[j] / price[i]) * 1e4
    return y


def y_dist_summary(y: np.ndarray, label: str) -> str:
    valid = y[~np.isnan(y)]
    if len(valid) == 0:
        return f"{label}: 0 valid"
    pos = (valid > 0).sum()
    neg = (valid < 0).sum()
    p99 = np.quantile(valid, 0.99)
    p1 = np.quantile(valid, 0.01)
    tail_ratio = abs(p1) / max(p99, 1e-9)
    return (f"{label}: n={len(valid):>6,} mean={valid.mean():+7.3f}bps "
            f"pos%={pos/len(valid)*100:5.2f} p1={p1:+7.3f} p99={p99:+7.3f} "
            f"|p1|/p99={tail_ratio:.2f}×")


def main(day: str) -> int:
    print(f"\n{'='*78}\n  DIAG: segmented_replay vs raw parquet  day={day}\n{'='*78}\n")

    # ---- 1) Raw parquet stats ----
    print("[1] Reading raw cold parquet ...")
    raw = read_raw_cc_parquet(day)
    print(f"    file rows: {raw['n_total']:,}")
    print(f"    side breakdown: 0={raw['n_side0']:,} 1={raw['n_side1']:,} "
          f"2={raw['n_side2']:,} 3={raw['n_side3']:,} 4={raw['n_side4']:,}")
    print(f"    ts sorted ascending? {raw['ts_sorted']}")
    print(f"    ts[0]={raw['ts_first']}  ts[-1]={raw['ts_last']}")
    print(f"    ts.min={raw['ts_min']}  ts.max={raw['ts_max']}")
    if raw['ts_first'] != raw['ts_min'] or raw['ts_last'] != raw['ts_max']:
        print(f"    ⚠️  parquet NOT ts-sorted — discover_day_ts_range will use "
              f"wrong window!")

    # ---- 2) discover_day_ts_range — what window does segmented_replay see? ----
    seg_first, seg_last = discover_day_ts_range(day)
    print(f"\n[2] discover_day_ts_range() → first={seg_first} last={seg_last}")
    raw_in_window = (raw["ts"] >= seg_first) & (raw["ts"] <= seg_last)
    raw_out_window = ~raw_in_window
    raw_side2 = raw["side"] == 2
    raw_side2_in_window = raw_side2 & raw_in_window
    raw_side2_out_window = raw_side2 & raw_out_window
    print(f"    raw rows in segment window: {int(raw_in_window.sum()):,}  "
          f"out: {int(raw_out_window.sum()):,}")
    print(f"    raw side==2 in window: {int(raw_side2_in_window.sum()):,}  "
          f"out (dropped by seg window!): {int(raw_side2_out_window.sum()):,}")

    # ---- 3) Build raw side==2 ts list (the comparison reference) ----
    raw_s2_ts = raw["ts"][raw_side2]
    raw_s2_price = raw["price"][raw_side2]
    raw_s2_qty = raw["qty"][raw_side2]
    raw_s2_ts_set = set(raw_s2_ts.tolist())
    print(f"\n[3] Raw side==2 events: {len(raw_s2_ts):,} "
          f"({len(raw_s2_ts_set):,} unique ts → "
          f"{len(raw_s2_ts)-len(raw_s2_ts_set):,} duplicate-ts events)")

    # ---- 4) Run segmented_replay for this day ----
    print(f"\n[4] Running segmented_replay for {day} (8 workers) ...")
    result = replay_days_parallel(
        [day], n_workers=8, segment_sec=300, verbose=False,
    )
    cache_ts = result["ts"]
    cache_price = result["price"]

    # ---- 4b) Quick patched-sort verification: reorder cache events at
    # tied ts back to parquet order, then recompute y. If asymmetry
    # disappears, the price-tie sort is the bug. ----
    print(f"\n[4b] Patched check: reorder cache events at tied ts to parquet "
          f"arrival order ...")
    # Build mapping: (ts, price) → arrival_idx from raw side==2 parquet
    raw_arrival_idx = {}
    for idx, (ts_v, pr_v) in enumerate(zip(raw_s2_ts, raw_s2_price)):
        key = (int(ts_v), float(pr_v))
        raw_arrival_idx.setdefault(key, []).append(idx)
    # For each cache event, find an arrival_idx from raw map. Within a
    # (ts, price) group, multiple cache events match multiple raw events
    # 1:1 in remaining-pool order.
    raw_pool = {k: list(v) for k, v in raw_arrival_idx.items()}
    cache_arrival_idx = np.full(len(cache_ts), -1, dtype=np.int64)
    for i, (ts_v, pr_v) in enumerate(zip(cache_ts, cache_price)):
        key = (int(ts_v), float(pr_v))
        pool = raw_pool.get(key)
        if pool:
            cache_arrival_idx[i] = pool.pop(0)
    matched = (cache_arrival_idx >= 0).sum()
    print(f"    matched cache events to raw arrival idx: {matched:,} / "
          f"{len(cache_ts):,}")
    # Reorder cache by arrival_idx and recompute y
    if matched == len(cache_ts):
        order_p = np.argsort(cache_arrival_idx, kind="stable")
        cache_ts_p = cache_ts[order_p]
        cache_price_p = cache_price[order_p]
    else:
        cache_ts_p = None  # signal failure
    cache_ts_set = set(cache_ts.tolist())
    print(f"    cache events: {len(cache_ts):,} "
          f"({len(cache_ts_set):,} unique ts)")
    print(f"    wall: {result['elapsed_sec']:.1f}s")

    # ---- 5) Set overlap ----
    overlap = cache_ts_set & raw_s2_ts_set
    cache_only = cache_ts_set - raw_s2_ts_set
    raw_only = raw_s2_ts_set - cache_ts_set
    print(f"\n[5] Set overlap (unique ts):")
    print(f"    cache ∩ raw_s2: {len(overlap):,}")
    print(f"    cache \\ raw_s2: {len(cache_only):,}  ← cache extras")
    print(f"    raw_s2 \\ cache: {len(raw_only):,}  ← raw extras")
    print(f"    cache ts in raw_s2 (event-level): "
          f"{int(np.isin(cache_ts, list(raw_s2_ts_set), assume_unique=False).sum()):,} "
          f"/ {len(cache_ts):,}")

    # ---- 6) Where are cache-extra ts coming from? ----
    # For each cache_only ts, check what side(s) it has in raw parquet
    if cache_only:
        print(f"\n[6] Cache-extra ts: where are they in raw?")
        cache_only_arr = np.array(sorted(cache_only)[:200], dtype=np.int64)
        side_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, "none": 0}
        for ts_val in cache_only_arr:
            matches = raw["ts"] == ts_val
            if not matches.any():
                side_counts["none"] += 1
            else:
                for sd_val in raw["side"][matches]:
                    side_counts[int(sd_val)] += 1
        print(f"    sampled first 200 cache-extra ts; raw-side dist: "
              f"{side_counts}")
        print(f"    (if 'none' > 0 → cache emitted ts that's not in raw at all!)")

    # ---- 7) Where are raw-extra ts going? ----
    if raw_only:
        print(f"\n[7] Raw-extra ts: why missing from cache?")
        raw_only_arr = np.array(sorted(raw_only)[:200], dtype=np.int64)
        in_window = ((raw_only_arr >= seg_first) & (raw_only_arr <= seg_last))
        print(f"    sampled first 200 raw-extra ts; in segment window: "
              f"{int(in_window.sum())}/200 (rest dropped by seg window)")

    # ---- 8) y distribution comparison ----
    print(f"\n[8] y forward-1s log-return (bps) distributions:")
    # Sort cache by ts for forward lookup
    cache_order = np.argsort(cache_ts, kind="stable")
    cache_ts_sorted = cache_ts[cache_order]
    cache_price_sorted = cache_price[cache_order]
    y_cache = compute_forward_log_return(cache_ts_sorted, cache_price_sorted,
                                          horizon_ms=1000)
    # Raw side==2 sorted by ts
    raw_order = np.argsort(raw_s2_ts, kind="stable")
    raw_ts_sorted = raw_s2_ts[raw_order]
    raw_price_sorted = raw_s2_price[raw_order]
    y_raw = compute_forward_log_return(raw_ts_sorted, raw_price_sorted,
                                        horizon_ms=1000)
    print(f"    {y_dist_summary(y_raw, 'raw side==2  ')}")
    print(f"    {y_dist_summary(y_cache, 'cache (seg)  ')}")
    if cache_ts_p is not None:
        y_cache_p = compute_forward_log_return(
            cache_ts_p, cache_price_p, horizon_ms=1000)
        print(f"    {y_dist_summary(y_cache_p, 'cache reordered → arrival')}")

    # ---- 9) Duplicate-ts analysis ----
    cache_unique, cache_counts = np.unique(cache_ts, return_counts=True)
    raw_unique, raw_counts = np.unique(raw_s2_ts, return_counts=True)
    cache_dup = cache_counts[cache_counts > 1]
    raw_dup = raw_counts[raw_counts > 1]
    print(f"\n[9] Duplicate-ts events:")
    print(f"    cache: {len(cache_dup):,} unique ts have >1 event "
          f"(max dup={cache_counts.max() if len(cache_counts) else 0})")
    print(f"    raw  : {len(raw_dup):,} unique ts have >1 event "
          f"(max dup={raw_counts.max() if len(raw_counts) else 0})")

    # ---- 10) Sanity: print a few examples of mismatched ts ----
    if cache_only:
        print(f"\n[10] First 5 cache-only ts (with raw rows at each ts):")
        for ts_val in sorted(cache_only)[:5]:
            matches = raw["ts"] == ts_val
            rows = list(zip(raw["side"][matches], raw["price"][matches],
                            raw["qty"][matches]))
            print(f"    ts={ts_val}: raw has {int(matches.sum())} rows "
                  f"→ {rows[:5]}")
    if raw_only:
        print(f"\n[11] First 5 raw-only ts:")
        for ts_val in sorted(raw_only)[:5]:
            matches = raw["ts"] == ts_val
            rows = list(zip(raw["side"][matches], raw["price"][matches],
                            raw["qty"][matches]))
            print(f"    ts={ts_val}: raw has {int(matches.sum())} rows "
                  f"→ {rows[:3]}  (in seg window? {seg_first<=ts_val<=seg_last})")

    print(f"\n{'='*78}\n  Done.\n{'='*78}\n")
    return 0


if __name__ == "__main__":
    day = sys.argv[1] if len(sys.argv) > 1 else "20260417"
    sys.exit(main(day))
