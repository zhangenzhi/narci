"""P0 reproducer: bisect why UM features go NaN at 04-23 16:39:17.

nyx report 2026-05-08 (NARCI_NYX_INTERFACE.md): on 04-23 the UM-derived
features (r_um, r_um_2s, r_um_5s, r_um_10s) flip to NaN at 16:39:17 and
stay NaN for the rest of the day (8471 events / 27% of single-day
samples). The cold tier raw is fine — Vision has 5-10M trades/hr in
hour 16-17 of 04-23, and the narci daily file has 1.2M aggTrades.

Bug must be in narci's FeatureBuilder / L2Reconstructor consuming the
events. Approach:

  1. Run the 16:30-16:50 segment via build_segment_worker.
  2. Confirm r_um cutoff and find first-NaN ts.
  3. Dump UM events (all sides, ±5s window around cutoff) to inspect
     for: zero/negative price, zero/negative qty, anomalous side, or
     non-monotonic ts.
  4. Inspect FeatureBuilder UM venue state right before and after the
     cutoff to identify which condition triggers (history empty?
     history[-1].price <= 0?  at_or_before returns None?).

Output: prints diagnostics, no files written.

Run from /lustre1/work/c30636/narci:
    python -m research.p0_um_nan_bisect
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from features import FeatureBuilder, FEATURE_NAMES
from research.segmented_replay import (build_segment_worker, COLD,
                                        VENUE_SOURCES)


DAY = "20260423"
SEG_START = pd.Timestamp("2026-04-23 16:30", tz="UTC").value // 1_000_000
SEG_END   = pd.Timestamp("2026-04-23 16:50", tz="UTC").value // 1_000_000


def stage1_segment_replay() -> dict:
    """Run nyx's reproducer; return (result, first_nan_ts, sample_idx)."""
    print(f"=== STAGE 1: replay segment {DAY} 16:30 → 16:50 ===")
    result = build_segment_worker((DAY, SEG_START, SEG_END))
    n_samples = len(result["ts"])
    print(f"  samples: {n_samples}, events: {result['n_events']}")
    if n_samples == 0:
        raise RuntimeError("no samples — segment empty")

    um_idx = FEATURE_NAMES.index("r_um")
    mask = np.isnan(result["X"][:, um_idx])
    print(f"  r_um NaN rate: {mask.mean()*100:.2f}%   "
          f"({mask.sum()} / {n_samples})")
    if mask.any():
        first_nan_idx = int(np.argmax(mask))
        first_nan_ts = int(result["ts"][first_nan_idx])
        print(f"  first NaN sample idx={first_nan_idx}  "
              f"ts={pd.to_datetime(first_nan_ts, unit='ms', utc=True)}")
        # any non-NaN AFTER first NaN?
        rec_idxs = np.where(~mask[first_nan_idx:])[0]
        if len(rec_idxs):
            print(f"  ⚠ NaN intermittent — {len(rec_idxs)} recovery samples after cutoff")
        else:
            print(f"  ✓ NaN persistent from idx {first_nan_idx} to end "
                  f"({n_samples - first_nan_idx} samples)")
        return {"result": result, "first_nan_ts": first_nan_ts,
                "first_nan_idx": first_nan_idx, "mask": mask}
    print("  no NaN in this segment — bug may be later in day")
    return {"result": result, "first_nan_ts": None,
            "first_nan_idx": None, "mask": mask}


def stage2_dump_um_events_around(cutoff_ts: int, window_ms: int = 5000) -> None:
    """Print all UM cold-tier events within [cutoff - window, cutoff + window]."""
    print(f"\n=== STAGE 2: UM events ±{window_ms/1000}s around cutoff "
          f"{pd.to_datetime(cutoff_ts, unit='ms', utc=True)} ===")
    path = COLD / "binance" / "um_futures" / f"BTCUSDT_RAW_{DAY}_DAILY.parquet"
    if not path.exists():
        print(f"  missing: {path}")
        return
    lo = cutoff_ts - window_ms
    hi = cutoff_ts + window_ms
    tbl = pq.read_table(
        str(path),
        filters=[("timestamp", ">=", lo), ("timestamp", "<", hi)],
    )
    df = tbl.to_pandas()
    df = df.sort_values(["timestamp", "side"]).reset_index(drop=True)
    print(f"  total UM events in window: {len(df)}")
    print(f"  side distribution: {df['side'].value_counts().to_dict()}")
    print(f"  price stats:    min={df['price'].min():.4f}  "
          f"max={df['price'].max():.4f}  any<=0={int((df['price']<=0).sum())}")
    print(f"  quantity stats: min={df['quantity'].min():.6f}  "
          f"max={df['quantity'].max():.6f}  any nan={int(df['quantity'].isna().sum())}")
    # any side=2 (trade) rows in window?
    trades = df[df["side"] == 2]
    print(f"  side=2 trades in window: {len(trades)}")
    if len(trades) > 0:
        print(f"    first trade ts: {pd.to_datetime(int(trades['timestamp'].iloc[0]), unit='ms', utc=True)}")
        print(f"    last trade ts:  {pd.to_datetime(int(trades['timestamp'].iloc[-1]), unit='ms', utc=True)}")
        print(f"    any trade price<=0: {int((trades['price'] <= 0).sum())}")
        # show first 5 + last 5 trade rows
        print("    first 5 trades:")
        print(trades.head().to_string(index=False))
        print("    last 5 trades:")
        print(trades.tail().to_string(index=False))


def stage3_inspect_um_state(cutoff_ts: int) -> None:
    """Replay events sequentially up to cutoff and inspect UM venue state.

    Re-uses the same event-merge logic as build_segment_worker but stops
    just BEFORE and just AFTER the first sample-NaN, dumping UM
    prices.history + book state at each point.
    """
    print(f"\n=== STAGE 3: UM venue state at cutoff ===")
    warmup_sec = 300
    read_start_ms = SEG_START - warmup_sec * 1000

    fb = FeatureBuilder(lookback_seconds=warmup_sec)

    rows = []
    for exchange, market, sym, venue in VENUE_SOURCES:
        path = COLD / exchange / market / f"{sym}_RAW_{DAY}_DAILY.parquet"
        if not path.exists():
            continue
        tbl = pq.read_table(
            str(path),
            filters=[("timestamp", ">=", read_start_ms),
                     ("timestamp", "<", SEG_END)],
        )
        ts_arr = tbl.column("timestamp").to_pylist()
        sd_arr = tbl.column("side").to_pylist()
        pr_arr = tbl.column("price").to_pylist()
        qt_arr = tbl.column("quantity").to_pylist()
        for i in range(len(ts_arr)):
            ts_i = int(ts_arr[i])
            sd = int(sd_arr[i])
            rows.append((ts_i, -sd, venue, sd, float(pr_arr[i]), float(qt_arr[i])))
    rows.sort()
    print(f"  total events to replay: {len(rows)}")

    def dump_um_state(label: str, before_event_idx: int) -> None:
        um = fb._venues["um"]
        history = list(um.prices.history)
        print(f"  --- UM state {label} (after event idx {before_event_idx-1}) ---")
        print(f"      prices.history len={len(history)}")
        if history:
            print(f"      history[0]:  ts={pd.to_datetime(history[0][0], unit='ms', utc=True)}  price={history[0][1]:.4f}")
            print(f"      history[-1]: ts={pd.to_datetime(history[-1][0], unit='ms', utc=True)}  price={history[-1][1]:.4f}")
            # Verify monotonic
            ts_arr = [h[0] for h in history]
            non_mono = sum(1 for i in range(1, len(ts_arr)) if ts_arr[i] < ts_arr[i-1])
            if non_mono:
                print(f"      ⚠ non-monotonic ts in history: {non_mono} reversals")
            # Any non-positive prices?
            bad = [(t, p) for t, p in history if p <= 0]
            if bad:
                print(f"      ⚠ non-positive prices in history: {len(bad)}")
                print(f"         first bad: ts={pd.to_datetime(bad[0][0], unit='ms', utc=True)}  price={bad[0][1]}")
        # Book state
        book_state = um.book.get_state(top_n=5)
        if book_state is None:
            print(f"      book.get_state(): None  ← BOOK NOT READY")
        else:
            print(f"      book.mid_price:    {book_state.get('mid_price', 'NA')}")
            print(f"      book.microprice:   {book_state.get('microprice', 'NA')}")
            print(f"      book.imb_top1:     {book_state.get('imbalance_top1', 'NA')}")
        # Compute r_um now to confirm
        r = um.prices.log_return(cutoff_ts, 1000)
        print(f"      log_return(cutoff_ts, 1000ms) = {r}")

    # Replay events; dump state at 1s before cutoff and right at first NaN sample
    dumped_before = False
    dumped_after = False
    for i, (ts_ms, _neg, venue, side, price, qty) in enumerate(rows):
        fb.update_event(venue, ts_ms, side, price, qty)
        if not dumped_before and ts_ms >= cutoff_ts - 1000:
            dump_um_state("at cutoff_ts - 1000ms", i)
            dumped_before = True
        if not dumped_after and ts_ms >= cutoff_ts:
            dump_um_state("at cutoff_ts", i)
            dumped_after = True
            # also dump 5 events later
            for j in range(min(5, len(rows) - i - 1)):
                ts2, _, ven2, sd2, pr2, qt2 = rows[i+1+j]
                fb.update_event(ven2, ts2, sd2, pr2, qt2)
            dump_um_state("at cutoff + 5 events", i + 5)
            break


def main() -> None:
    s1 = stage1_segment_replay()
    if s1["first_nan_ts"] is not None:
        cutoff = s1["first_nan_ts"]
        stage2_dump_um_events_around(cutoff, window_ms=5000)
        stage3_inspect_um_state(cutoff)


if __name__ == "__main__":
    main()
