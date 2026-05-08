"""Profile FeatureBuilder.update_event hot path.

nyx P2 (2026-05-08): perf diagnostic estimated 259 μs/event with
L2Reconstructor.apply_event responsible for 58% (~150 μs). Estimate is
read-from-code, not measured. Validate with cProfile + per-component
timing on a real 5 min cold-tier slice.

Approach:
  1. Read 5 min of UM/BJ/CC events from cold tier 04-23.
  2. Drive FeatureBuilder.update_event sequentially.
  3. cProfile the full hot path; print top functions by cumulative and
     tottime.
  4. Also do a coarse component-level timing (timing FB.update_event
     vs L2Reconstructor.apply_event vs trade/price window pushes).

Usage:
    python -m research.profile_update_event
    python -m research.profile_update_event --minutes 10  # bigger sample
    python -m research.profile_update_event --venue um    # single-venue stress
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

sys.path.insert(0, "/lustre1/work/c30636/narci")
from features import FeatureBuilder
from research.segmented_replay import COLD


VENUE_SOURCES = [
    ("coincheck",   "spot",       "BTC_JPY", "cc"),
    ("binance_jp",  "spot",       "BTCJPY",  "bj"),
    ("binance",     "um_futures", "BTCUSDT", "um"),
]


def load_events(day: str, start_ms: int, end_ms: int,
                venues: set[str] | None = None) -> list[tuple]:
    """Load events from cold tier in [start_ms, end_ms). Returns sorted
    list of (ts, -side, venue, side, price, qty)."""
    rows = []
    for exchange, market, sym, venue in VENUE_SOURCES:
        if venues is not None and venue not in venues:
            continue
        path = COLD / exchange / market / f"{sym}_RAW_{day}_DAILY.parquet"
        if not path.exists():
            continue
        tbl = pq.read_table(
            str(path),
            filters=[("timestamp", ">=", start_ms),
                     ("timestamp", "<", end_ms)],
        )
        ts_arr = tbl.column("timestamp").to_pylist()
        sd_arr = tbl.column("side").to_pylist()
        pr_arr = tbl.column("price").to_pylist()
        qt_arr = tbl.column("quantity").to_pylist()
        for i in range(len(ts_arr)):
            ts_i = int(ts_arr[i])
            if ts_i < start_ms or ts_i >= end_ms:
                continue
            sd = int(sd_arr[i])
            rows.append((ts_i, -sd, venue, sd, float(pr_arr[i]), float(qt_arr[i])))
    rows.sort()
    return rows


def coarse_timing(rows: list[tuple]) -> None:
    """Time the whole loop, then time FB.update_event isolation, then
    time L2Reconstructor.apply_event isolation. Cheap deltas."""
    print("\n=== COARSE TIMING (no profiler overhead) ===")
    n = len(rows)

    # Whole loop
    fb = FeatureBuilder(lookback_seconds=300)
    t0 = time.perf_counter()
    for ts_ms, _neg, venue, side, price, qty in rows:
        fb.update_event(venue, ts_ms, side, price, qty)
    elapsed_full = time.perf_counter() - t0
    print(f"  full update_event loop:     {elapsed_full:>7.3f}s   "
          f"{1e6 * elapsed_full / n:>7.2f} μs/event   ({n / elapsed_full:>7,.0f} ev/s)")

    # apply_event only (skip FB.update_event wrapper)
    fb2 = FeatureBuilder(lookback_seconds=300)
    venues_dict = fb2._venues
    t0 = time.perf_counter()
    for ts_ms, _neg, venue, side, price, qty in rows:
        v = venues_dict.get(venue)
        if v is not None:
            v.book.apply_event(ts_ms, side, price, qty)
    elapsed_apply = time.perf_counter() - t0
    print(f"  L2.apply_event only:        {elapsed_apply:>7.3f}s   "
          f"{1e6 * elapsed_apply / n:>7.2f} μs/event")

    # tuple unpack baseline (loop overhead)
    t0 = time.perf_counter()
    for ts_ms, _neg, venue, side, price, qty in rows:
        pass
    elapsed_loop = time.perf_counter() - t0
    print(f"  loop overhead only:         {elapsed_loop:>7.3f}s   "
          f"{1e6 * elapsed_loop / n:>7.2f} μs/event")

    print(f"\n  FB wrapper overhead:        "
          f"{1e6 * (elapsed_full - elapsed_apply) / n:>7.2f} μs/event "
          f"({100*(elapsed_full - elapsed_apply)/elapsed_full:.0f}% of total)")


def cprofile_run(rows: list[tuple]) -> None:
    """Full cProfile pass, print top functions by tottime + cumtime."""
    print("\n=== cProfile (full update_event loop) ===")
    fb = FeatureBuilder(lookback_seconds=300)
    profiler = cProfile.Profile()
    profiler.enable()
    for ts_ms, _neg, venue, side, price, qty in rows:
        fb.update_event(venue, ts_ms, side, price, qty)
    profiler.disable()

    stats = pstats.Stats(profiler)
    stats.strip_dirs()

    print("\n--- Top 25 by tottime (self time) ---")
    stats.sort_stats("tottime").print_stats(25)

    print("\n--- Top 15 by cumtime (incl. callees) ---")
    stats.sort_stats("cumtime").print_stats(15)


def event_summary(rows: list[tuple]) -> None:
    print(f"\n=== input: {len(rows):,} events ===")
    side_counts: dict[int, int] = {}
    venue_counts: dict[str, int] = {}
    for _, _, venue, side, _, _ in rows:
        side_counts[side] = side_counts.get(side, 0) + 1
        venue_counts[venue] = venue_counts.get(venue, 0) + 1
    print(f"  by venue: {venue_counts}")
    print(f"  by side:  {side_counts}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="20260423")
    ap.add_argument("--start", default="2026-04-23 12:00",
                    help="UTC start of window")
    ap.add_argument("--minutes", type=int, default=5)
    ap.add_argument("--venues", default="cc,bj,um",
                    help="comma-separated venues to load")
    ap.add_argument("--skip-cprofile", action="store_true",
                    help="skip cProfile (which has ~30%% overhead) and just do coarse timing")
    args = ap.parse_args()

    import pandas as pd
    start_ms = pd.Timestamp(args.start, tz="UTC").value // 1_000_000
    end_ms = start_ms + args.minutes * 60_000
    venues = set(args.venues.split(","))

    print(f"window: {args.start} + {args.minutes} min, venues={venues}")
    print("loading events ...", flush=True)
    t0 = time.perf_counter()
    rows = load_events(args.day, start_ms, end_ms, venues=venues)
    print(f"  loaded in {time.perf_counter() - t0:.1f}s", flush=True)

    event_summary(rows)
    coarse_timing(rows)
    if not args.skip_cprofile:
        cprofile_run(rows)


if __name__ == "__main__":
    main()
