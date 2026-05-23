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
#
# Per-CC-symbol venue tables (added 2026-05-19 per nyx INTERFACE_NYX_NARCI.md
# §2026-05-19 ask #1). The legacy module-level `VENUE_SOURCES` is preserved
# below as an alias for backward compat with callers that import it
# directly (research/ols_um_cc_e2e.py etc.).
VENUE_SOURCES_BY_SYMBOL: dict[str, list[tuple[str, str, str, str]]] = {
    "BTC_JPY": [
        # (exchange,    market,       symbol,    venue)
        ("coincheck",   "spot",       "BTC_JPY", "cc"),
        ("binance_jp",  "spot",       "BTCJPY",  "bj"),
        ("binance",     "um_futures", "BTCUSDT", "um"),
        ("binance",     "spot",       "BTCUSDT", "bs"),  # v5: perp-spot basis
    ],
    "ETH_JPY": [
        ("coincheck",   "spot",       "ETH_JPY", "cc"),
        ("binance_jp",  "spot",       "ETHJPY",  "bj"),
        ("binance",     "um_futures", "ETHUSDT", "um"),
        ("binance",     "spot",       "ETHUSDT", "bs"),
    ],
}

# Backward-compat: legacy import path. Defaults to BTC_JPY when no symbol
# arg is supplied to replay_days_parallel / discover_day_ts_range etc.
VENUE_SOURCES: list[tuple[str, str, str, str]] = VENUE_SOURCES_BY_SYMBOL["BTC_JPY"]

UM_KEEP_SIDES = None  # None = keep everything; (2,3,4) = drop incrementals

# Sampling modes accepted by build_segment_worker. Kept in sync with
# calibration.alpha_models.SAMPLING_MODES (narci own enum source of truth).
# Default mode preserves legacy 3-field emit (ts/price/X) — existing OLS /
# v9 mid-y / BJ-native bindings all use this code path unchanged.
SAMPLING_MODE_EVENT_AT_CC_TRADE = "event_at_cc_trade"
SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL = "event_at_simulated_maker_fill"

# quote_side encoding (nyx 2026-05-23 schema lock, c13d657). Used ONLY in
# `event_at_simulated_maker_fill` cache schema — does NOT collide with
# the RAW 4-column `side` enum (where 2=trade) because this lives in a
# different cache file with explicit `quote_side` column name.
QUOTE_SIDE_BUY = 1   # simulated BUY maker quote (placed at bid+tick), filled by SELL taker
QUOTE_SIDE_SELL = 2  # simulated SELL maker quote (placed at ask-tick), filled by BUY taker

# Default segment / warmup. WARMUP must match FeatureBuilder lookback so
# the trade-window features (e.g. um_imb_30s_norm, um_vol_5s) compute
# from a non-truncated history at segment start.
DEFAULT_SEGMENT_SEC = 300   # 5-min segments → 288 per day
DEFAULT_WARMUP_SEC = 300    # 5-min warmup overlap


def discover_day_ts_range(day: str, cc_symbol: str = "BTC_JPY") -> tuple[int, int]:
    """Return (first_ts_ms, last_ts_ms) for the day by scanning CC cold
    tier (smallest file). Used as canonical day window.

    `cc_symbol` is the CC-side symbol (e.g. "BTC_JPY", "ETH_JPY"); 2026-05-19
    added per nyx ETH/JPY generalization."""
    path = COLD / "coincheck" / "spot" / f"{cc_symbol}_RAW_{day}_DAILY.parquet"
    if not path.exists():
        path = COLD / f"{cc_symbol}_RAW_{day}_DAILY.parquet"  # legacy fallback
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


def build_segments(day: str, segment_sec: int = DEFAULT_SEGMENT_SEC,
                    cc_symbol: str = "BTC_JPY") -> list[tuple]:
    """Return list of (day, seg_start_ms, seg_end_ms) tuples. The
    segment_sec window is inclusive of start, exclusive of end."""
    first_ts, last_ts = discover_day_ts_range(day, cc_symbol=cc_symbol)
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
                          prune_snapshot_dust: bool = False,
                          incremental_ready_threshold: int = 5,
                          venues: list[tuple[str, str, str, str]] | None = None,
                          cc_venue_tag: str = "cc",
                          sampling_mode: str = SAMPLING_MODE_EVENT_AT_CC_TRADE) -> dict:
    """Worker: process events in [seg_start - warmup, seg_end), emit
    feature rows for CC trades in [seg_start, seg_end).

    venues: list of (exchange, market, symbol, venue_tag) tuples. Defaults
    to module-level VENUE_SOURCES (BTC_JPY layout). Pass an entry from
    VENUE_SOURCES_BY_SYMBOL to drive replay on a non-BTC asset (e.g.
    ETH_JPY). Added 2026-05-19 per nyx INTERFACE_NYX_NARCI.md
    §2026-05-19 ask #1.

    cc_venue_tag: which venue tag (4th element of `venues` tuples) is the
    "CC" emitter that should produce feature samples. Defaults to "cc"
    matching the existing tables. Kept configurable for symmetry; callers
    rarely override.

    book_staleness_seconds: P1 fix 2026-05-08. Forwarded to FeatureBuilder
    so transient book invalidation (BJ short-run NaN, ~57 events median)
    falls back to last-valid top1/state instead of emitting NaN. Default 0
    keeps strict behaviour.

    prune_snapshot_dust: P1-C fix 2026-05-08. When True, strip cross-side
    dust at every snapshot batch close. Pairs cleanly with staleness.

    incremental_ready_threshold: P3 fix 2026-05-09. Number of levels
    each side must reach (via incrementals OR snapshots) before
    L2Reconstructor.is_ready flips True. Default 5 unblocks BJ-style
    venues where the warmup window doesn't span a 600s snapshot
    batch — the pre-fix behavior left is_ready=False permanently for
    ~50% of segments, defeating the staleness fallback (cache stays
    empty so fresh-fail can't fall back). Pass 0 to disable
    (snapshot-only legacy behavior).

    sampling_mode: emission semantics. Added 2026-05-23 per nyx 2026-05-23
    Phase 1 ask (676b734 + c13d657 + 602333e schema lock).
      - "event_at_cc_trade" (default, legacy): emit (ts, price, X[36]) on
        every trade event at cc_venue_tag.
      - "event_at_simulated_maker_fill": for each opposite-direction taker
        at cc_venue_tag, emit a simulated-maker-fill sample with extended
        schema (ts, X[36], best_bid_p, best_ask_p, spread_p, quote_side,
        mid_t). Used by the v10 binding family (conditional fill-PnL
        target). Triggers on side==2 trade events of either qty sign,
        encodes quote_side = BUY(1) when taker is SELL (qty<0), SELL(2)
        when taker is BUY (qty>0). Skips emit when book is not ready /
        top1 unavailable. narci is intentionally tick-blind — nyx-side
        train pipeline applies tick_size in fill_price computation."""
    day, seg_start_ms, seg_end_ms = args
    read_start_ms = seg_start_ms - warmup_sec * 1000
    if venues is None:
        venues = VENUE_SOURCES

    fb = FeatureBuilder(lookback_seconds=warmup_sec,
                        book_staleness_seconds=book_staleness_seconds,
                        prune_snapshot_dust=prune_snapshot_dust,
                        incremental_ready_threshold=incremental_ready_threshold)

    # Collect events from all 3 venues in time range using parquet filter
    rows: list[tuple] = []
    for exchange, market, sym, venue in venues:
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

    # Sort merged events: (ts, -side) so snapshots before diffs at same ts.
    # Use key= to limit comparison to the first two tuple elements ONLY:
    # full-tuple sort would fall through to (venue, side, price, qty) for
    # rows with same (ts, -side), and the price-ascending tiebreaker
    # systematically reordered duplicate-ts CC trades by price → biased
    # forward-target y distributions (mean -0.44 bps / pos% 40.5 / tail
    # 1.7× vs raw parquet's mean -0.001 / pos% 46.4 / tail 1.04×; see
    # `research/diag_segreplay_vs_raw.py` and nyx INTERFACE_NYX_NARCI.md
    # 2026-05-17 §D). Python list.sort is stable, so ties preserve
    # insertion order = parquet ts-asc within each venue iteration.
    rows.sort(key=lambda r: (r[0], r[1]))

    samples_ts: list[int] = []
    samples_price: list[float] = []
    samples_x: list[list[float]] = []
    # event_at_simulated_maker_fill extra columns (only populated when that
    # mode is active; left empty otherwise to keep allocation trivial).
    samples_bid: list[float] = []
    samples_ask: list[float] = []
    samples_spread: list[float] = []
    samples_quote_side: list[int] = []
    samples_mid: list[float] = []
    sim_fill_mode = (sampling_mode == SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)

    for ts_ms, _neg, venue, side, price, qty in rows:
        fb.update_event(venue, ts_ms, side, price, qty)
        # Only emit features for CC trades INSIDE the segment proper
        # (warmup events are processed but don't produce samples).
        if ts_ms < seg_start_ms:
            continue
        if venue == cc_venue_tag and side == 2:
            feats = fb.get_features(ts_ms)
            x = [feats.get(name, float("nan")) for name in FEATURE_NAMES]
            if sim_fill_mode:
                # Trade events do not mutate the L2 book (apply_event only
                # accumulates trade volume on side==2), so get_top1() returns
                # the book state immediately BEFORE the taker arrives —
                # which is exactly the state a resting maker quote would
                # have observed. Skip when book is not yet ready (warmup
                # boundary) or top1 unavailable (e.g. crossed book that
                # can't be resolved).
                top = fb._venues[cc_venue_tag].book.get_top1()
                if top is None:
                    continue
                bid = top["best_bid"]
                ask = top["best_ask"]
                if bid is None or ask is None:
                    continue
                q_side = QUOTE_SIDE_BUY if qty < 0 else QUOTE_SIDE_SELL
                samples_ts.append(ts_ms)
                samples_x.append(x)
                samples_bid.append(bid)
                samples_ask.append(ask)
                samples_spread.append(ask - bid)
                samples_quote_side.append(q_side)
                samples_mid.append(top["mid_price"])
            else:
                samples_ts.append(ts_ms)
                samples_price.append(price)
                samples_x.append(x)

    result = {
        "day": day,
        "seg_start": seg_start_ms,
        "seg_end": seg_end_ms,
        "ts": np.asarray(samples_ts, dtype=np.int64),
        "X": np.asarray(samples_x, dtype=np.float64) if samples_x else
             np.zeros((0, len(FEATURE_NAMES)), dtype=np.float64),
        "n_events": len(rows),
        "n_samples": len(samples_ts),
    }
    if sim_fill_mode:
        result["best_bid_p"] = np.asarray(samples_bid, dtype=np.float64)
        result["best_ask_p"] = np.asarray(samples_ask, dtype=np.float64)
        result["spread_p"]   = np.asarray(samples_spread, dtype=np.float64)
        result["quote_side"] = np.asarray(samples_quote_side, dtype=np.int8)
        result["mid_t"]      = np.asarray(samples_mid, dtype=np.float64)
    else:
        result["price"] = np.asarray(samples_price, dtype=np.float64)
    return result


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
    incremental_ready_threshold: int = 5,
    verbose: bool = True,
    symbol: str = "BTC_JPY",
    cc_venue_tag: str = "cc",
    sampling_mode: str = SAMPLING_MODE_EVENT_AT_CC_TRADE,
) -> dict:
    """Top-level: split given days into segments, run workers in parallel,
    return concatenated arrays sorted by ts.

    max_hours_per_day: if set, only replay N hours per day (smoke).
    start_hour: skip first N hours of each day (e.g., to avoid known
        recorder outages early in day).
    symbol: CC-side symbol selecting the venue table from
        VENUE_SOURCES_BY_SYMBOL. Default "BTC_JPY" preserves legacy
        behavior. Added 2026-05-19 per nyx ETH/JPY generalization.
    cc_venue_tag: which venue tag (4th element of venue tuples) should
        emit feature samples. Default "cc" (legacy: emit at CC trades).
        Pass "bj" for BJ-native bindings (e.g. v9_bj_midy_36) — workers
        will emit features on every BJ trade arrival instead of CC.
        Added 2026-05-21 per nyx 2026-05-20 晚 Ask #3 (lift
        emit-at-venue param from build_segment_worker to top level).
    sampling_mode: see build_segment_worker docstring. Lifted to top-level
        2026-05-23 per nyx Phase 1 ask (676b734 + c13d657 + 602333e).
        Default `event_at_cc_trade` preserves legacy emit semantics.
        Pass `event_at_simulated_maker_fill` for the v10 family
        (conditional fill-PnL target); aggregator returns the extended
        7-field schema (ts/X + best_bid_p/best_ask_p/spread_p/quote_side/
        mid_t) instead of the 3-field legacy (ts/price/X)."""
    venues = VENUE_SOURCES_BY_SYMBOL.get(symbol)
    if venues is None:
        raise ValueError(
            f"unknown symbol {symbol!r}; available: "
            f"{sorted(VENUE_SOURCES_BY_SYMBOL.keys())}. Extend "
            f"VENUE_SOURCES_BY_SYMBOL to add a new asset.")
    all_segments = []
    for d in days:
        segs = build_segments(d, segment_sec, cc_symbol=symbol)
        if start_hour > 0:
            skip = int(start_hour * 3600 / segment_sec)
            segs = segs[skip:]
        if max_hours_per_day is not None:
            keep = max(1, int(max_hours_per_day * 3600 / segment_sec))
            segs = segs[:keep]
        all_segments.extend(segs)

    if verbose:
        print(f"  {symbol}: {len(days)} days × ~"
              f"{len(all_segments)//len(days)} segments = "
              f"{len(all_segments)} total tasks")
        print(f"  pool size: {n_workers}")

    t0 = time.time()
    # Always wrap in partial when venues != default BTC table, so workers
    # get the per-symbol venue list. Also handle the existing P1/P1-C/P3
    # kwargs for staleness/dust/ready-threshold, plus the new top-level
    # cc_venue_tag (BJ-native binding support).
    needs_partial = (
        venues is not VENUE_SOURCES
        or book_staleness_seconds > 0
        or prune_snapshot_dust
        or incremental_ready_threshold != 5
        or cc_venue_tag != "cc"
        or sampling_mode != SAMPLING_MODE_EVENT_AT_CC_TRADE)
    if needs_partial:
        from functools import partial
        worker_fn = partial(build_segment_worker,
                            book_staleness_seconds=book_staleness_seconds,
                            prune_snapshot_dust=prune_snapshot_dust,
                            incremental_ready_threshold=incremental_ready_threshold,
                            venues=venues,
                            cc_venue_tag=cc_venue_tag,
                            sampling_mode=sampling_mode)
    else:
        worker_fn = build_segment_worker
    with mp.get_context("spawn").Pool(n_workers) as pool:
        results = pool.map(worker_fn, all_segments)
    elapsed = time.time() - t0

    if verbose:
        print(f"  pool wall {elapsed:.1f}s "
              f"({sum(r['n_events'] for r in results)/elapsed:,.0f} events/sec aggregate)")

    # Concat results, sort by ts to recover global order
    sim_fill_mode = (sampling_mode == SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)
    results = [r for r in results if r["n_samples"] > 0]
    if not results:
        empty = {"ts": np.array([], dtype=np.int64),
                 "X": np.zeros((0, len(FEATURE_NAMES))),
                 "elapsed_sec": elapsed,
                 "n_events_total": 0}
        if sim_fill_mode:
            empty["best_bid_p"] = np.array([], dtype=np.float64)
            empty["best_ask_p"] = np.array([], dtype=np.float64)
            empty["spread_p"]   = np.array([], dtype=np.float64)
            empty["quote_side"] = np.array([], dtype=np.int8)
            empty["mid_t"]      = np.array([], dtype=np.float64)
        else:
            empty["price"] = np.array([], dtype=np.float64)
        return empty

    ts_all = np.concatenate([r["ts"] for r in results])
    X_all = np.concatenate([r["X"] for r in results], axis=0)

    # Sort by ts (parallel results may be out of order)
    order = np.argsort(ts_all, kind="stable")
    out = {
        "ts": ts_all[order],
        "X": X_all[order],
        "elapsed_sec": elapsed,
        "n_events_total": sum(r["n_events"] for r in results),
    }
    if sim_fill_mode:
        for k in ("best_bid_p", "best_ask_p", "spread_p", "quote_side", "mid_t"):
            arr = np.concatenate([r[k] for r in results])
            out[k] = arr[order]
    else:
        price_all = np.concatenate([r["price"] for r in results])
        out["price"] = price_all[order]
    return out
