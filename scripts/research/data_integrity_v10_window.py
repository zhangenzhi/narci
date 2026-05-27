"""Per-venue per-day data integrity scan for the v10 24-day window.

Output: csv-style table + per-venue summary printed to stdout. Used by
docs/archive/DATA_INTEGRITY_v10_BJ_WINDOW_2026-05-24.md.

Venues scanned (v10 BJ BTC pipeline):
  binance_jp/spot BTCJPY        — emit-venue (sampling triggers here)
  coincheck/spot  BTC_JPY       — feature venue (cc_micro_dev, cc_l2_spread)
  binance/um_futures BTCUSDT    — feature venue (UM ladder, umvtb, etc.)
  binance/spot       BTCUSDT    — feature venue (basis_um_bps — DROP_NAMES)

For each (venue, day):
  - in-day event count (ts within [day_start, day_end))
  - trade count (side==2)
  - head_missing (sec gap from day_start to first event)
  - tail_missing (sec gap from last event to day_end)
  - internal gaps >= 60s (n_count, total_sec)
  - top gap (longest single gap, with start time)
  - coverage % = 1 - outage / 86400

Pattern detection:
  - flag any 1797 ≤ gap ≤ 1807 sec (the BJ "30-min magic gap")
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

TRAIN_DAYS = [
    "20260417", "20260418", "20260419", "20260420", "20260421",
    "20260422", "20260423", "20260429", "20260430", "20260501",
    "20260502", "20260503", "20260504", "20260505", "20260508",
    "20260509",
]
OOS_DAYS = ["20260510", "20260511", "20260512", "20260513",
            "20260514", "20260515", "20260516", "20260517"]

# (venue_tag, exchange, market, symbol, role)
VENUES = [
    ("BJ",  "binance_jp",  "spot",       "BTCJPY",  "emit"),
    ("CC",  "coincheck",   "spot",       "BTC_JPY", "feature"),
    ("UM",  "binance",     "um_futures", "BTCUSDT", "feature"),
    ("BS",  "binance",     "spot",       "BTCUSDT", "feature(dropped)"),
]


def fmt_hhmmss(ms_ts: int) -> str:
    return datetime.fromtimestamp(ms_ts / 1000, tz=timezone.utc).strftime("%H:%M:%S")


def scan_day(venue_tag, exchange, market, symbol, day) -> dict | None:
    path = COLD / exchange / market / f"{symbol}_RAW_{day}_DAILY.parquet"
    if not path.exists():
        return None
    tbl = pq.read_table(str(path), columns=["timestamp", "side"])
    ts = tbl.column("timestamp").to_numpy()
    sides = tbl.column("side").to_numpy()

    day_start = int(datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    day_end = day_start + 86_400_000

    in_day = (ts >= day_start) & (ts < day_end)
    if not in_day.any():
        return {"day": day, "venue": venue_tag, "missing": True}

    ts_in = np.sort(ts[in_day])
    sides_in = sides[in_day]
    head_ms = max(0, int(ts_in[0]) - day_start)
    tail_ms = max(0, day_end - int(ts_in[-1]))
    diffs = np.diff(ts_in)
    big = diffs[diffs >= 60_000]
    n_big = int(len(big))
    total_gap = int(big.sum())
    outage_ms = head_ms + tail_ms + total_gap

    # Top gap details
    if len(diffs) > 0:
        top_idx = int(np.argmax(diffs))
        top_gap_sec = int(diffs[top_idx]) // 1000
        top_gap_start = fmt_hhmmss(int(ts_in[top_idx]))
    else:
        top_gap_sec = 0
        top_gap_start = "-"

    # 1802s magic-gap pattern (1797 ≤ gap ≤ 1807)
    has_1802 = int(((big >= 1_797_000) & (big <= 1_807_000)).sum())

    return {
        "day": day,
        "venue": venue_tag,
        "events_in_day": int(in_day.sum()),
        "trades": int((sides_in == 2).sum()),
        "head_min": head_ms / 60_000.0,
        "tail_min": tail_ms / 60_000.0,
        "n_gaps_60s": n_big,
        "outage_min": outage_ms / 60_000.0,
        "coverage_pct": 100 * (1 - outage_ms / 86_400_000),
        "top_gap_sec": top_gap_sec,
        "top_gap_start_utc": top_gap_start,
        "has_1802s": has_1802,
    }


def main():
    all_days = TRAIN_DAYS + OOS_DAYS

    # Print per-venue tables
    for venue_tag, exchange, market, symbol, role in VENUES:
        print(f"\n=== {venue_tag} ({exchange}/{market}/{symbol}, {role}) ===")
        print(f"{'day':>10}  {'phase':>5}  {'trades':>10}  {'head':>7}  {'tail':>7}  "
              f"{'gaps':>5}  {'outage':>9}  {'cov%':>6}  {'1802s':>5}  {'top_gap':>20}")
        venue_total = {"trades": 0, "outage_min": 0.0, "coverage_days": 0,
                       "has_1802s": 0, "n_gaps_60s": 0}
        for day in all_days:
            r = scan_day(venue_tag, exchange, market, symbol, day)
            if r is None:
                print(f"{day:>10}  {'MISS':>5}  {'--':>10}  {'--':>7}  {'--':>7}  "
                      f"{'--':>5}  {'--':>9}  {'--':>6}  {'--':>5}  {'(no file)':>20}")
                continue
            if r.get("missing"):
                print(f"{day:>10}  {'EMPTY':>5}  {'--':>10}  ...")
                continue
            phase = "TR" if day in TRAIN_DAYS else "OOS"
            print(f"{day:>10}  {phase:>5}  {r['trades']:>10,}  "
                  f"{r['head_min']:>6.1f}m  {r['tail_min']:>6.1f}m  "
                  f"{r['n_gaps_60s']:>5}  {r['outage_min']:>7.1f}m  "
                  f"{r['coverage_pct']:>5.2f}%  {r['has_1802s']:>5}  "
                  f"{r['top_gap_sec']:>5}s @ {r['top_gap_start_utc']}")
            venue_total["trades"] += r["trades"]
            venue_total["outage_min"] += r["outage_min"]
            venue_total["coverage_days"] += 1
            venue_total["has_1802s"] += r["has_1802s"]
            venue_total["n_gaps_60s"] += r["n_gaps_60s"]
        if venue_total["coverage_days"] > 0:
            avg_cov = 100 * (1 - venue_total["outage_min"] / 60 / 24 / venue_total["coverage_days"])
            print(f"\n  TOTAL: {venue_total['trades']:,} trades, avg coverage {avg_cov:.2f}%, "
                  f"{venue_total['n_gaps_60s']} gaps≥60s ({venue_total['has_1802s']} of which are 1802s magic-gap)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
