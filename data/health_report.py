"""Per-minute data health scan for cold-tier files.

For each (venue, day), bucket events by minute and flag minutes with
fewer than DEAD_THRESHOLD_EVENTS as dead. Output JSON consumed by
sanity_gate for fine-grained segment filtering.

Run:
    python -m data.health_report
    python -m data.health_report --out data/cold_health.json
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

# Per-exchange short tag (used as venue label in features layer).
# Layout: cold/{exchange}/{market}/{SYMBOL}_RAW_{date}_DAILY.parquet
EXCHANGE_TO_VENUE = {
    ("coincheck",   "spot"):       "cc",
    ("binance_jp",  "spot"):       "bj",
    ("binance",     "spot"):       "bnc_spot",  # JPY pairs on binance global
    ("binance",     "um_futures"): "um",
}

# A minute is "dead" when total book-update events (sides 0/1/3/4) is
# below this threshold (= 1 event/sec average; well below any healthy
# venue, but generous enough to allow normal thin minutes).
DEAD_THRESHOLD_EVENTS = 60

# Side encoding (project-wide):
BOOK_UPDATE_SIDES = (0, 1, 3, 4)


def scan_file(path: Path) -> dict:
    """Scan one daily parquet, return per-minute book-update counts.

    Path is expected to be cold/{exchange}/{market}/{SYMBOL}_RAW_{date}_DAILY.parquet.
    """
    # Path: .../cold/{exchange}/{market}/{SYMBOL}_RAW_{date}_DAILY.parquet
    market = path.parent.name
    exchange = path.parent.parent.name
    venue = EXCHANGE_TO_VENUE.get((exchange, market))
    if venue is None:
        return {}
    sym_token = path.stem.replace("_RAW_", "|").replace("_DAILY", "").split("|")[0]
    day = path.stem.split("_")[-2]

    pf = pq.ParquetFile(str(path))
    if pf.metadata.num_rows == 0:
        return {
            "venue": venue, "exchange": exchange, "symbol": sym_token,
            "day": day, "first_ts_ms": 0, "total_events": 0,
            "minute_counts": [], "dead_intervals": [[0, 1440]],
            "max_gap_sec": 86400.0,
            "side_distribution": {s: 0 for s in range(5)},
        }

    tbl = pf.read(columns=["timestamp", "side"])
    ts = np.asarray(tbl.column("timestamp").to_pylist(), dtype=np.int64)
    sd = np.asarray(tbl.column("side").to_pylist(), dtype=np.int8)
    total = ts.size
    side_dist = {int(s): int((sd == s).sum()) for s in range(5)}

    first_ts = int(ts[0])

    # Book-update count per minute since first_ts.
    book_mask = np.isin(sd, BOOK_UPDATE_SIDES)
    minute_idx = ((ts - first_ts) // 60_000).astype(np.int64)
    n_min = int(minute_idx[-1]) + 1
    counts = np.bincount(minute_idx[book_mask], minlength=n_min).astype(int)

    # Find contiguous dead-minute runs.
    dead_mask = counts < DEAD_THRESHOLD_EVENTS
    intervals: list[list[int]] = []
    if dead_mask.any():
        # Locate runs of True.
        diffs = np.diff(np.concatenate(([0], dead_mask.astype(int), [0])))
        starts = np.where(diffs == 1)[0]
        ends = np.where(diffs == -1)[0]
        intervals = [[int(s), int(e)] for s, e in zip(starts, ends)]

    # Max inter-event gap.
    if total >= 2:
        max_gap_ms = int(np.max(np.diff(ts)))
    else:
        max_gap_ms = 0

    return {
        "venue": venue,
        "exchange": exchange,
        "symbol": sym_token,
        "day": day,
        "first_ts_ms": first_ts,
        "total_events": total,
        "minute_counts": counts.tolist(),
        "dead_intervals": intervals,
        "max_gap_sec": max_gap_ms / 1000.0,
        "side_distribution": side_dist,
    }


def scan_all(workers: int = 8, symbol: str | None = None) -> dict:
    """Recursively scan cold/{exchange}/{market}/*.parquet.
    symbol: if set, only scan that symbol (e.g. 'BTCJPY')."""
    files = sorted(COLD.rglob("*_RAW_*_DAILY.parquet"))
    if symbol:
        sym_upper = symbol.upper()
        files = [f for f in files if f.stem.startswith(f"{sym_upper}_RAW_")]
    print(f"Scanning {len(files)} cold-tier files with {workers} workers ...")
    t0 = time.time()
    with mp.get_context("spawn").Pool(workers) as pool:
        results = pool.map(scan_file, files)
    elapsed = time.time() - t0
    results = [r for r in results if r]
    print(f"  done in {elapsed:.1f}s")

    # Re-shape: venue → day → entry (drop minute_counts to keep JSON small).
    out = {"version": "v1",
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "dead_threshold_events_per_min": DEAD_THRESHOLD_EVENTS,
           "venues": {}}
    for r in results:
        v = r["venue"]
        d = r["day"]
        out["venues"].setdefault(v, {})[d] = {
            "exchange": r["exchange"],
            "symbol": r["symbol"],
            "first_ts_ms": r["first_ts_ms"],
            "total_events": r["total_events"],
            "n_minutes": len(r["minute_counts"]),
            "n_dead_minutes": sum(e - s for s, e in r["dead_intervals"]),
            "dead_intervals": r["dead_intervals"],
            "max_gap_sec": r["max_gap_sec"],
            "side_distribution": r["side_distribution"],
        }
    return out


def print_summary(report: dict) -> None:
    print()
    print("=== Health Summary ===")
    for venue, days in sorted(report["venues"].items()):
        print(f"\n[{venue}]")
        print(f"  {'day':>10}  {'total':>14}  {'n_min':>6}  {'dead':>5}  "
              f"{'max_gap_s':>9}  intervals")
        for day in sorted(days):
            e = days[day]
            iv = "; ".join(f"[{s}–{e_})" for s, e_ in e["dead_intervals"]) or "-"
            print(f"  {day:>10}  {e['total_events']:>14,}  "
                  f"{e['n_minutes']:>6}  {e['n_dead_minutes']:>5}  "
                  f"{e['max_gap_sec']:>9.1f}  {iv}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default="data/cold_health.json",
                    help="output JSON path (relative to project root)")
    ap.add_argument("--symbol", default=None,
                    help="only scan this symbol (e.g. BTCJPY); default = all")
    args = ap.parse_args()
    report = scan_all(workers=args.workers, symbol=args.symbol)
    print_summary(report)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
