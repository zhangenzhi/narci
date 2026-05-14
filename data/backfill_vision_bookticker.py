"""Backfill cold-tier daily files from Binance Vision bookTicker.

Vision bookTicker = top-of-book best_bid + best_ask + qty at every WS
update. Not full L2 depth, but enough to compute mid_price for the v5
basis_um_bps feature (= log(um_mid / bs_mid) * 1e4).

When the live recorder has no L2 for a date (e.g. BTCUSDT spot on
2026-04-17 → 05-12 was Vision aggTrades-backfilled trade-only via
data.backfill_vision_trades), this script adds best_bid/best_ask
events as narci side=3/4 rows. The L2Reconstructor's atomic snapshot-
batch semantics (clear-then-set on a new ts) handle the high-frequency
bookTicker stream cleanly — each bookTicker update is logically a
1-level snapshot batch.

Vision bookTicker schema (CSV, post-2025 header):
    update_id, best_bid_price, best_bid_qty,
    best_ask_price, best_ask_qty,
    transaction_time, event_time

Narci cold tier (4-col):
    timestamp (int64 ms), side (int), price (f64), quantity (f64)
    side=3 for bid snapshot, side=4 for ask snapshot

Usage:
    python -m data.backfill_vision_bookticker \\
        --symbol BTCUSDT --market spot --exchange binance \\
        --days 20260417,20260418,...,20260509
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


def bookticker_to_narci(vision_path: Path) -> pd.DataFrame:
    """Read a Binance Vision bookTicker parquet, return narci 4-col
    DataFrame with side=3 (bid) + side=4 (ask) rows interleaved."""
    df = pd.read_parquet(vision_path)
    # Older files may use different column names; normalise.
    rename = {
        "best_bid_price": "best_bid_price",
        "best_ask_price": "best_ask_price",
        "best_bid_qty":   "best_bid_qty",
        "best_ask_qty":   "best_ask_qty",
        "event_time":     "event_time",
    }
    # Detect alternate naming (some older Vision dumps use camelCase).
    alt = {"bestBidPrice": "best_bid_price", "bestAskPrice": "best_ask_price",
           "bestBidQty":   "best_bid_qty",   "bestAskQty":   "best_ask_qty"}
    df = df.rename(columns={k: v for k, v in alt.items() if k in df.columns})
    # Pick event_time from whichever column exists.
    if "event_time" not in df.columns:
        for cand in ("transaction_time", "E", "T"):
            if cand in df.columns:
                df = df.rename(columns={cand: "event_time"})
                break

    needed = ["best_bid_price", "best_bid_qty", "best_ask_price",
              "best_ask_qty", "event_time"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise ValueError(f"bookTicker file missing columns {missing}; "
                         f"have {list(df.columns)}")

    # event_time may be pandas datetime (if _parse_csv promoted it) or
    # raw int (in s/ms/us). Normalise to int64 ms.
    et = df["event_time"]
    if pd.api.types.is_datetime64_any_dtype(et):
        ts_ms = (et.astype("int64") // 1_000_000).to_numpy()
    else:
        raw = et.astype("int64").to_numpy()
        # Auto-detect unit on max sample to handle s / ms / us.
        sample = raw.max() if len(raw) else 0
        if sample < 1e11:
            ts_ms = raw * 1000          # seconds → ms
        elif sample < 1e14:
            ts_ms = raw                 # already ms
        else:
            ts_ms = raw // 1000         # microseconds → ms

    bid_p = df["best_bid_price"].astype(np.float64).to_numpy()
    bid_q = df["best_bid_qty"].astype(np.float64).to_numpy()
    ask_p = df["best_ask_price"].astype(np.float64).to_numpy()
    ask_q = df["best_ask_qty"].astype(np.float64).to_numpy()

    # Build interleaved (side=3 bid, side=4 ask) rows. Each update
    # becomes one bid + one ask at the SAME ts so L2Reconstructor
    # treats them as one atomic snapshot batch (P3 clear-then-set
    # semantics — best_bid_p and best_ask_p both refresh).
    n = len(df)
    out_ts = np.empty(2 * n, dtype=np.int64)
    out_side = np.empty(2 * n, dtype=np.int64)
    out_price = np.empty(2 * n, dtype=np.float64)
    out_qty = np.empty(2 * n, dtype=np.float64)
    out_ts[0::2] = ts_ms
    out_ts[1::2] = ts_ms
    out_side[0::2] = 3
    out_side[1::2] = 4
    out_price[0::2] = bid_p
    out_price[1::2] = ask_p
    out_qty[0::2] = bid_q
    out_qty[1::2] = ask_q

    return pd.DataFrame({
        "timestamp": out_ts,
        "side": out_side,
        "price": out_price,
        "quantity": out_qty,
    })


def backfill_day(symbol: str, market: str, day: str,
                 *, dry_run: bool = False, exchange: str = "binance",
                 force: bool = False) -> dict:
    """Merge Vision bookTicker into the cold daily for one (symbol, day).

    Behaviour:
      - If cold daily has > 1000 existing depth events (side=0/1/3/4),
        skip (assume already has real L2 from recorder; bookTicker
        retrofit would degrade quality). Pass force=True to override.
      - If cold daily exists but is depth-empty (trade-only from
        backfill_vision_trades --create-if-missing), add side=3/4 rows
        from bookTicker and re-sort. Preserves side=2 trade rows.
      - If cold daily doesn't exist at all, create it as bookTicker-only
        (no trades). Caller should typically run backfill_vision_trades
        first, then this script.

    Returns a summary dict.
    """
    cold_path = COLD / exchange / market / f"{symbol}_RAW_{day}_DAILY.parquet"
    if not cold_path.exists():
        # Legacy flat fallback.
        cold_path = COLD / f"{symbol}_RAW_{day}_DAILY.parquet"

    yyyy, mm, dd = day[:4], day[4:6], day[6:8]
    vision_path = (OFFICIAL / market / "bookTicker" / symbol /
                   f"{symbol}-{yyyy}-{mm}-{dd}.parquet")
    if not vision_path.exists():
        return {"day": day, "status": "missing_vision",
                "vision_path": str(vision_path)}

    book_df = bookticker_to_narci(vision_path)
    n_vision = len(book_df)

    if not cold_path.exists():
        # Create bookTicker-only daily.
        cold_path = COLD / exchange / market / f"{symbol}_RAW_{day}_DAILY.parquet"
        cold_path.parent.mkdir(parents=True, exist_ok=True)
        merged = book_df.sort_values(by=["timestamp", "side"],
                                      ascending=[True, False]).reset_index(drop=True)
        tmp_path = cold_path.with_suffix(".parquet.tmp")
        table = pa.Table.from_pandas(merged, preserve_index=False)
        pq.write_table(table, tmp_path)
        if not dry_run:
            tmp_path.replace(cold_path)
        else:
            tmp_path.unlink()
        return {
            "day": day,
            "status": "created_bookticker_only" if not dry_run else "dry_run_create",
            "cold_path": str(cold_path),
            "n_trade_existing": 0,
            "n_depth_existing": 0,
            "n_depth_added": n_vision,
            "n_total_after": len(merged),
        }

    cold_df = pd.read_parquet(cold_path)
    n_depth_existing = int(cold_df["side"].isin([0, 1, 3, 4]).sum())
    n_trade_existing = int((cold_df["side"] == 2).sum())

    if n_depth_existing > 1000 and not force:
        return {"day": day, "status": "skip_already_has_depth",
                "n_depth_existing": n_depth_existing,
                "n_trade_existing": n_trade_existing}

    # Drop any pre-existing depth (we're replacing with bookTicker);
    # keep side=2 trades.
    trade_df = cold_df[cold_df["side"] == 2]
    merged = pd.concat([trade_df, book_df], ignore_index=True)
    merged = merged.sort_values(by=["timestamp", "side"],
                                 ascending=[True, False]).reset_index(drop=True)

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
        "n_trade_existing": n_trade_existing,
        "n_depth_existing": n_depth_existing,
        "n_depth_added": n_vision,
        "n_total_after": len(merged),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT")
    ap.add_argument("--market", default="spot", choices=["um_futures", "spot"])
    ap.add_argument("--exchange", default="binance",
                    help="cold tier subdir; default 'binance'")
    ap.add_argument("--days", required=True,
                    help="comma-separated YYYYMMDD list")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="bypass skip-if-already-has-depth. Drops existing "
                         "side=0/1/3/4 rows and replaces with bookTicker. "
                         "Use ONLY when you know existing depth is partial.")
    args = ap.parse_args()

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    flags = []
    if args.dry_run: flags.append("DRY RUN")
    if args.force: flags.append("FORCE")
    print(f"Backfilling bookTicker {args.symbol} {args.market} on {len(days)} days "
          f"{'(' + ', '.join(flags) + ')' if flags else ''}")
    print(f"  cold root: {COLD}/{args.exchange}/{args.market}")
    print(f"  vision:    {OFFICIAL}/{args.market}/bookTicker/{args.symbol}")
    print()

    summaries = []
    for d in days:
        result = backfill_day(args.symbol, args.market, d,
                               dry_run=args.dry_run, exchange=args.exchange,
                               force=args.force)
        summaries.append(result)
        ok = ("ok", "dry_run", "created_bookticker_only", "dry_run_create")
        if result["status"] in ok:
            print(f"  {d}: trades {result['n_trade_existing']:>9,}  "
                  f"+depth {result['n_depth_added']:>10,}  "
                  f"= total {result['n_total_after']:>11,}  [{result['status']}]")
        else:
            print(f"  {d}: {result['status']}  ({result})")
    print()
    n_ok = sum(1 for s in summaries
               if s["status"] in ("ok", "dry_run",
                                   "created_bookticker_only", "dry_run_create"))
    print(f"  ✅ {n_ok}/{len(days)} days backfilled")
    return 0 if n_ok == len(days) else 1


if __name__ == "__main__":
    sys.exit(main())
