"""Snapshot-segment parallel replay of cold tier through FeatureBuilder.

Splits a day into fixed-time segments. Each segment worker:
  1. Reads events from all venues for [seg_start - WARMUP, seg_end] using
     parquet predicate pushdown.
  2. Sorts events by (ts, -side) so snapshots ingest first.
  3. Drives FeatureBuilder through the merged stream.
  4. Emits feature rows for CC trades in [seg_start, seg_end] only.

Trade-window features (300s lookback) are correct because each worker
includes WARMUP_SEC = 300s of pre-segment events. Book reconstruction is
correct because narci recorder injects a snapshot every save_interval
(~60s), so the warmup window contains multiple snapshots.

Master gathers all worker outputs, sorts by ts, builds OLS-ready X / y.

Used by `research.ols_um_cc_e2e` when --workers > N (snapshot path) and
when SEGMENT_SEC > 0.
"""

from __future__ import annotations

import heapq
import math
import multiprocessing as mp
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from features import FeatureBuilder, FEATURE_NAMES


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

# (exchange, market, symbol) → FeatureBuilder venue tag.
# Cold layout: cold/{exchange}/{market}/{SYMBOL}_RAW_{date}_DAILY.parquet
VENUE_SOURCES: list[tuple[str, str, str, str]] = [
    # (exchange,    market,       symbol,    venue)
    ("coincheck",   "spot",       "BTC_JPY", "cc"),
    ("binance_jp",  "spot",       "BTCJPY",  "bj"),
    ("binance",     "um_futures", "BTCUSDT", "um"),
]

UM_KEEP_SIDES = None  # None = keep everything; (2,3,4) = drop incrementals

# Default segment / warmup. WARMUP must match FeatureBuilder lookback so
# the trade-window features (e.g. um_imb_30s_norm, um_vol_5s) compute
# from a non-truncated history at segment start.
DEFAULT_SEGMENT_SEC = 300   # 5-min segments → 288 per day
DEFAULT_WARMUP_SEC = 300    # 5-min warmup overlap


def discover_day_ts_range(day: str) -> tuple[int, int]:
    """Return (first_ts_ms, last_ts_ms) for the day by scanning CC cold
    tier (smallest file). Used as canonical day window."""
    path = COLD / "coincheck" / "spot" / f"BTC_JPY_RAW_{day}_DAILY.parquet"
    if not path.exists():
        path = COLD / f"BTC_JPY_RAW_{day}_DAILY.parquet"  # legacy fallback
    if not path.exists():
        raise FileNotFoundError(f"missing CC daily: {path}")
    pf = pq.ParquetFile(str(path))
    md = pf.metadata
    if md.num_row_groups == 0:
        raise RuntimeError(f"empty parquet: {path}")
    # Read just timestamp column
    tbl = pf.read(columns=["timestamp"])
    ts = tbl.column("timestamp")
    return int(ts[0].as_py()), int(ts[-1].as_py())


def build_segments(day: str, segment_sec: int = DEFAULT_SEGMENT_SEC) -> list[tuple]:
    """Return list of (day, seg_start_ms, seg_end_ms) tuples. The
    segment_sec window is inclusive of start, exclusive of end."""
    first_ts, last_ts = discover_day_ts_range(day)
    seg_ms = segment_sec * 1000
    segments = []
    s = first_ts
    while s < last_ts:
        e = min(s + seg_ms, last_ts + 1)
        segments.append((day, s, e))
        s = e
    return segments


def build_segment_worker(args: tuple,
                          warmup_sec: int = DEFAULT_WARMUP_SEC,
                          book_staleness_seconds: float = 0.0,
                          prune_snapshot_dust: bool = False) -> dict:
    """Worker: process events in [seg_start - warmup, seg_end), emit
    feature rows for CC trades in [seg_start, seg_end).

    book_staleness_seconds: P1 fix 2026-05-08. Forwarded to FeatureBuilder
    so transient book invalidation (BJ short-run NaN, ~57 events median)
    falls back to last-valid top1/state instead of emitting NaN. Default 0
    keeps strict behaviour.

    prune_snapshot_dust: P1-C fix 2026-05-08. When True, strip cross-side
    dust at every snapshot batch close. Pairs cleanly with staleness."""
    day, seg_start_ms, seg_end_ms = args
    read_start_ms = seg_start_ms - warmup_sec * 1000

    fb = FeatureBuilder(lookback_seconds=warmup_sec,
                        book_staleness_seconds=book_staleness_seconds,
                        prune_snapshot_dust=prune_snapshot_dust)

    # Collect events from all 3 venues in time range using parquet filter
    rows: list[tuple] = []
    for exchange, market, sym, venue in VENUE_SOURCES:
        path = COLD / exchange / market / f"{sym}_RAW_{day}_DAILY.parquet"
        if not path.exists():
            # Backwards compat: fall back to flat layout.
            path = COLD / f"{sym}_RAW_{day}_DAILY.parquet"
        if not path.exists():
            continue
        try:
            tbl = pq.read_table(
                str(path),
                filters=[("timestamp", ">=", read_start_ms),
                         ("timestamp", "<", seg_end_ms)],
            )
        except Exception:
            # Some parquet versions don't accept range filter via pyarrow.
            # Fallback: read whole + slice.
            tbl = pq.read_table(str(path))
        ts_arr = tbl.column("timestamp").to_pylist()
        sd_arr = tbl.column("side").to_pylist()
        pr_arr = tbl.column("price").to_pylist()
        qt_arr = tbl.column("quantity").to_pylist()
        for i in range(len(ts_arr)):
            ts_i = int(ts_arr[i])
            if ts_i < read_start_ms or ts_i >= seg_end_ms:
                continue
            sd = int(sd_arr[i])
            if venue == "um" and UM_KEEP_SIDES is not None and sd not in UM_KEEP_SIDES:
                continue
            rows.append((ts_i, -sd, venue, sd, float(pr_arr[i]), float(qt_arr[i])))

    # Sort merged events: (ts, -side) so snapshots before diffs at same ts
    rows.sort()

    samples_ts: list[int] = []
    samples_price: list[float] = []
    samples_x: list[list[float]] = []

    for ts_ms, _neg, venue, side, price, qty in rows:
        fb.update_event(venue, ts_ms, side, price, qty)
        # Only emit features for CC trades INSIDE the segment proper
        # (warmup events are processed but don't produce samples).
        if ts_ms < seg_start_ms:
            continue
        if venue == "cc" and side == 2:
            feats = fb.get_features(ts_ms)
            x = [feats.get(name, float("nan")) for name in FEATURE_NAMES]
            samples_ts.append(ts_ms)
            samples_price.append(price)
            samples_x.append(x)

    return {
        "day": day,
        "seg_start": seg_start_ms,
        "seg_end": seg_end_ms,
        "ts": np.asarray(samples_ts, dtype=np.int64),
        "price": np.asarray(samples_price, dtype=np.float64),
        "X": np.asarray(samples_x, dtype=np.float64) if samples_x else
             np.zeros((0, len(FEATURE_NAMES)), dtype=np.float64),
        "n_events": len(rows),
        "n_samples": len(samples_ts),
    }


def replay_days_parallel(
    days: list[str],
    *,
    n_workers: int = 30,
    segment_sec: int = DEFAULT_SEGMENT_SEC,
    warmup_sec: int = DEFAULT_WARMUP_SEC,
    max_hours_per_day: float | None = None,
    start_hour: float = 0.0,
    book_staleness_seconds: float = 0.0,
    prune_snapshot_dust: bool = False,
    verbose: bool = True,
) -> dict:
    """Top-level: split given days into segments, run workers in parallel,
    return concatenated arrays sorted by ts.

    max_hours_per_day: if set, only replay N hours per day (smoke).
    start_hour: skip first N hours of each day (e.g., to avoid known
        recorder outages early in day)."""
    all_segments = []
    for d in days:
        segs = build_segments(d, segment_sec)
        if start_hour > 0:
            skip = int(start_hour * 3600 / segment_sec)
            segs = segs[skip:]
        if max_hours_per_day is not None:
            keep = max(1, int(max_hours_per_day * 3600 / segment_sec))
            segs = segs[:keep]
        all_segments.extend(segs)

    if verbose:
        print(f"  {len(days)} days × ~{len(all_segments)//len(days)} segments "
              f"= {len(all_segments)} total tasks")
        print(f"  pool size: {n_workers}")

    t0 = time.time()
    if book_staleness_seconds > 0 or prune_snapshot_dust:
        from functools import partial
        worker_fn = partial(build_segment_worker,
                            book_staleness_seconds=book_staleness_seconds,
                            prune_snapshot_dust=prune_snapshot_dust)
    else:
        worker_fn = build_segment_worker
    with mp.get_context("spawn").Pool(n_workers) as pool:
        results = pool.map(worker_fn, all_segments)
    elapsed = time.time() - t0

    if verbose:
        print(f"  pool wall {elapsed:.1f}s "
              f"({sum(r['n_events'] for r in results)/elapsed:,.0f} events/sec aggregate)")

    # Concat results, sort by ts to recover global order
    results = [r for r in results if r["n_samples"] > 0]
    if not results:
        return {"ts": np.array([]), "price": np.array([]),
                "X": np.zeros((0, len(FEATURE_NAMES))),
                "elapsed_sec": elapsed,
                "n_events_total": 0}

    ts_all = np.concatenate([r["ts"] for r in results])
    price_all = np.concatenate([r["price"] for r in results])
    X_all = np.concatenate([r["X"] for r in results], axis=0)

    # Sort by ts (parallel results may be out of order)
    order = np.argsort(ts_all, kind="stable")
    return {
        "ts": ts_all[order],
        "price": price_all[order],
        "X": X_all[order],
        "elapsed_sec": elapsed,
        "n_events_total": sum(r["n_events"] for r in results),
    }
