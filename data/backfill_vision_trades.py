"""Backfill cold-tier daily files from Binance Vision aggTrades.

When the live recorder fails to subscribe/receive aggTrades (e.g., UM
0424-0429 had a 6-day side=2 outage), the L2 book sides 0/1/3/4 are
healthy but trades are missing from the daily compacted file. This
script reads the official Binance Vision archive (already pulled to
local via the donor → gdrive:narci_official → rclone pipeline), converts
to narci 4-col format, and merges into the cold daily file.

Vision schema:
    agg_trade_id, price, quantity, first_trade_id, last_trade_id,
    timestamp (ns), is_buyer_maker (bool)

Cold tier schema (narci 4-col):
    timestamp (int64 ms), side (int), price (f64), quantity (f64)

Sign convention (matching data/exchange/binance.py:101-102):
    is_buyer_maker=True  → buyer is maker → taker sells → qty < 0
    is_buyer_maker=False → buyer is taker → qty > 0

Usage:
    python -m data.backfill_vision_trades \\
        --symbol BTCUSDT --market um_futures \\
        --days 20260424,20260425,20260426,20260427,20260428,20260429
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")
OFFICIAL = Path("/lustre1/work/c30636/narci/replay_buffer/official_validation")


def vision_to_narci(vision_path: Path) -> pd.DataFrame:
    """Read a Binance Vision aggTrades parquet, return narci 4-col DataFrame."""
    df = pd.read_parquet(vision_path)
    # timestamp is timestamp[ns] in pandas; convert to int64 ms.
    ts_ms = (df["timestamp"].astype("int64") // 1_000_000).astype(np.int64)
    qty = df["quantity"].to_numpy(dtype=np.float64)
    sign = np.where(df["is_buyer_maker"].to_numpy(dtype=bool), -1.0, 1.0)
    qty_signed = qty * sign
    return pd.DataFrame({
        "timestamp": ts_ms.values,
        "side": np.full(len(df), 2, dtype=np.int64),
        "price": df["price"].astype(np.float64).values,
        "quantity": qty_signed.astype(np.float64),
    })


def backfill_day(symbol: str, market: str, day: str,
                 *, dry_run: bool = False, exchange: str = "binance",
                 force: bool = False) -> dict:
    """Merge vision aggTrades into the cold daily for one (symbol, day).

    By default, days with >1000 existing trades are skipped (assume
    already-backfilled). Pass force=True to drop existing trades and
    replace with the full Vision archive — used for partial-live days
    where recording died mid-day (e.g., 0423 16:30:53 UTC), so cold has
    real but incomplete trade data and Vision's full day is preferred.

    Returns a summary dict.
    """
    cold_path = COLD / exchange / market / f"{symbol}_RAW_{day}_DAILY.parquet"
    if not cold_path.exists():
        # Legacy flat fallback.
        cold_path = COLD / f"{symbol}_RAW_{day}_DAILY.parquet"
    if not cold_path.exists():
        return {"day": day, "status": "missing_cold", "cold_path": str(cold_path)}

    # Vision date format: 2026-04-25
    yyyy, mm, dd = day[:4], day[4:6], day[6:8]
    vision_path = (OFFICIAL / market / "aggTrades" / symbol /
                   f"{symbol}-{yyyy}-{mm}-{dd}.parquet")
    if not vision_path.exists():
        return {"day": day, "status": "missing_vision",
                "vision_path": str(vision_path)}

    cold_df = pd.read_parquet(cold_path)
    n_trade_before = int((cold_df["side"] == 2).sum())
    if n_trade_before > 1000 and not force:
        return {"day": day, "status": "skip_already_has_trades",
                "n_trade_before": n_trade_before}

    vision_df = vision_to_narci(vision_path)
    n_vision = len(vision_df)

    # Drop existing (zero or sparse) side=2 from cold; keep only book events.
    book_df = cold_df[cold_df["side"] != 2]
    merged = pd.concat([book_df, vision_df], ignore_index=True)
    merged = merged.sort_values(by=["timestamp", "side"],
                                 ascending=[True, False]).reset_index(drop=True)

    # Round-trip: write to a tmp path, atomic rename.
    tmp_path = cold_path.with_suffix(".parquet.tmp")
    table = pa.Table.from_pandas(merged, preserve_index=False)
    pq.write_table(table, tmp_path)
    if not dry_run:
        tmp_path.replace(cold_path)
    else:
        tmp_path.unlink()

    return {
        "day": day,
        "status": "ok" if not dry_run else "dry_run",
        "cold_path": str(cold_path),
        "n_book": len(book_df),
        "n_trade_before": n_trade_before,
        "n_trade_added": n_vision,
        "n_total_after": len(merged),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    ap.add_argument("--market", default="um_futures",
                    choices=["um_futures", "spot"])
    ap.add_argument("--exchange", default="binance",
                    help="cold tier subdir; default 'binance'")
    ap.add_argument("--days", required=True,
                    help="comma-separated YYYYMMDD list")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="bypass skip-if-already-has-trades guard. Drops "
                         "existing side=2 from cold and replaces with full "
                         "Vision day. Use for partial-live days.")
    args = ap.parse_args()

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    print(f"Backfilling {args.symbol} {args.market} on {len(days)} days "
          f"{'(DRY RUN)' if args.dry_run else ''}{' (FORCE)' if args.force else ''}")
    print(f"  cold root: {COLD}/{args.exchange}/{args.market}")
    print(f"  vision  : {OFFICIAL}/{args.market}/aggTrades/{args.symbol}")
    print()

    summaries = []
    for d in days:
        result = backfill_day(args.symbol, args.market, d,
                               dry_run=args.dry_run, exchange=args.exchange,
                               force=args.force)
        summaries.append(result)
        if result["status"] == "ok" or result["status"] == "dry_run":
            print(f"  {d}: book {result['n_book']:>11,}  +trades {result['n_trade_added']:>9,}  "
                  f"= total {result['n_total_after']:>11,}")
        else:
            print(f"  {d}: {result['status']}  ({result})")
    print()
    n_ok = sum(1 for s in summaries if s["status"] in ("ok", "dry_run"))
    print(f"  ✅ {n_ok}/{len(days)} days backfilled")
    return 0 if n_ok == len(days) else 1


if __name__ == "__main__":
    sys.exit(main())
