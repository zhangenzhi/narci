"""UM mid → CC trade lead-lag correlation, cleaned data.

For each CC trade at time t, look up UM mid at times {t-N, ..., t+N} for
a grid of lags. Compute correlation between log-returns of:
  - UM mid: log(mid[t+lag] / mid[t+lag-1s])
  - CC trade: log(cc[t+1s] / cc[t])     (1s ahead, target)

Peak correlation lag identifies lead-lag relationship:
  lag > 0 → UM leads CC (UM future moves predict CC now)
  lag < 0 → CC leads UM
  lag = 0 → contemporaneous

Run:
    python -m research.lead_lag_um_cc --days 20260420,20260421,20260422,20260424
"""
from __future__ import annotations

import argparse
import bisect
import math
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


COLD = Path("/lustre1/work/c30636/narci/replay_buffer/cold")

# Lag grid in seconds (positive = UM future, negative = UM past)
LAG_SECONDS = list(range(-5, 11))  # -5s .. +10s


def stream_um_mid(day: str) -> tuple[np.ndarray, np.ndarray]:
    """Reconstruct UM mid as a time series sampled every event.
    Returns (ts_ms, mid) sorted by ts."""
    p = COLD / "binance" / "um_futures" / f"BTCUSDT_RAW_{day}_DAILY.parquet"
    if not p.exists():
        raise FileNotFoundError(p)
    tbl = pq.read_table(str(p), columns=["timestamp", "side", "price", "quantity"])
    ts = np.asarray(tbl.column("timestamp").to_pylist(), dtype=np.int64)
    sd = np.asarray(tbl.column("side").to_pylist(), dtype=np.int8)
    pr = np.asarray(tbl.column("price").to_pylist(), dtype=np.float64)
    qt = np.asarray(tbl.column("quantity").to_pylist(), dtype=np.float64)

    # Sort by (ts, -side) so snapshots before diffs at same ts.
    order = np.lexsort((-sd, ts))
    ts, sd, pr, qt = ts[order], sd[order], pr[order], qt[order]

    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    best_bid = None
    best_ask = None
    last_snap_bid_ts = -1
    last_snap_ask_ts = -1

    out_ts = []
    out_mid = []

    for i in range(ts.size):
        side = int(sd[i])
        price = float(pr[i])
        qty = float(qt[i])
        t = int(ts[i])

        if side in (0, 3):
            if side == 3 and t != last_snap_bid_ts:
                bids.clear()
                best_bid = None
                last_snap_bid_ts = t
            if qty == 0:
                bids.pop(price, None)
                if price == best_bid:
                    best_bid = max(bids) if bids else None
            else:
                bids[price] = qty
                if best_bid is None or price > best_bid:
                    best_bid = price
        elif side in (1, 4):
            if side == 4 and t != last_snap_ask_ts:
                asks.clear()
                best_ask = None
                last_snap_ask_ts = t
            if qty == 0:
                asks.pop(price, None)
                if price == best_ask:
                    best_ask = min(asks) if asks else None
            else:
                asks[price] = qty
                if best_ask is None or price < best_ask:
                    best_ask = price
        # side == 2 (trade) doesn't update book.

        if best_bid is not None and best_ask is not None and best_bid < best_ask:
            mid = (best_bid + best_ask) / 2
            out_ts.append(t)
            out_mid.append(mid)

    return np.asarray(out_ts, dtype=np.int64), np.asarray(out_mid, dtype=np.float64)


def stream_cc_trades(day: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (ts_ms, price) for CC BTC_JPY trades (side=2)."""
    p = COLD / "coincheck" / "spot" / f"BTC_JPY_RAW_{day}_DAILY.parquet"
    if not p.exists():
        raise FileNotFoundError(p)
    tbl = pq.read_table(str(p), columns=["timestamp", "side", "price"],
                         filters=[("side", "=", 2)])
    ts = np.asarray(tbl.column("timestamp").to_pylist(), dtype=np.int64)
    pr = np.asarray(tbl.column("price").to_pylist(), dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    return ts[order], pr[order]


def lookup_mid_at(ts_arr: np.ndarray, mid_arr: np.ndarray,
                   target_ts: np.ndarray) -> np.ndarray:
    """For each target ts, return the most recent mid <= ts (right-closed).
    Returns NaN if target ts is before first mid."""
    idx = np.searchsorted(ts_arr, target_ts, side="right") - 1
    out = np.full(target_ts.size, np.nan, dtype=np.float64)
    valid = idx >= 0
    out[valid] = mid_arr[idx[valid]]
    return out


def per_day(day: str) -> dict:
    print(f"  [{day}] streaming UM mid + CC trades ...")
    t0 = time.time()
    um_ts, um_mid = stream_um_mid(day)
    cc_ts, cc_px = stream_cc_trades(day)
    print(f"  [{day}]   UM mid points: {um_ts.size:,}  "
          f"CC trades: {cc_ts.size:,}  ({time.time()-t0:.1f}s)")

    # For each CC trade, also need cc_price 1s ahead.
    cc_ts_plus1 = cc_ts + 1000
    j = np.searchsorted(cc_ts, cc_ts_plus1, side="left")
    valid_target = j < cc_ts.size
    cc_future_px = np.full(cc_ts.size, np.nan)
    cc_future_px[valid_target] = cc_px[j[valid_target]]
    y_cc = np.where(
        np.logical_and(valid_target, cc_future_px > 0),
        np.log(cc_future_px / cc_px), np.nan)

    # For each lag, look up UM mid at (cc_ts + lag) and (cc_ts + lag - 1s).
    rows = {}
    for lag_s in LAG_SECONDS:
        lag_ms = lag_s * 1000
        m_now = lookup_mid_at(um_ts, um_mid, cc_ts + lag_ms)
        m_prev = lookup_mid_at(um_ts, um_mid, cc_ts + lag_ms - 1000)
        x_um = np.where((m_now > 0) & (m_prev > 0), np.log(m_now / m_prev), np.nan)

        mask = np.isfinite(x_um) & np.isfinite(y_cc)
        if mask.sum() < 100:
            rows[lag_s] = (0, np.nan)
            continue
        x = x_um[mask]
        y = y_cc[mask]
        # Pearson correlation
        corr = np.corrcoef(x, y)[0, 1]
        rows[lag_s] = (int(mask.sum()), float(corr))
    return {"day": day, "rows": rows}


def run(days: list[str]):
    print(f"=== UM mid → CC trade 1s-ahead lead-lag ({len(days)} days) ===\n")
    print(f"  lag grid: {LAG_SECONDS[0]}s .. {LAG_SECONDS[-1]}s\n")

    with mp.get_context("spawn").Pool(min(len(days), 4)) as pool:
        results = pool.map(per_day, days)

    print()
    print(f"{'lag (s)':>8}", end="")
    for r in results:
        print(f"  {r['day']:>10}", end="")
    print(f"  {'agg':>10}")

    # Aggregate by stacking samples across days (weighted by n).
    for lag_s in LAG_SECONDS:
        line = f"{lag_s:>+8}"
        all_xy = []  # accumulate samples? simpler: weighted-mean of corrs by n
        weighted_num = 0.0
        weighted_den = 0
        for r in results:
            n, c = r["rows"][lag_s]
            if math.isnan(c):
                line += f"  {'-':>10}"
                continue
            line += f"  {c*100:>+8.3f}%"
            weighted_num += c * n
            weighted_den += n
        agg = weighted_num / weighted_den if weighted_den > 0 else float("nan")
        line += f"  {agg*100:>+8.3f}%" if not math.isnan(agg) else f"  {'-':>10}"
        # Mark peak
        print(line)

    # Identify peak lag from aggregate
    agg_corrs = []
    for lag_s in LAG_SECONDS:
        weighted_num = 0.0
        weighted_den = 0
        for r in results:
            n, c = r["rows"][lag_s]
            if not math.isnan(c):
                weighted_num += c * n
                weighted_den += n
        agg_corrs.append(weighted_num / weighted_den if weighted_den > 0 else float("nan"))
    arr = np.asarray(agg_corrs)
    if np.isfinite(arr).any():
        peak_idx = int(np.nanargmax(np.abs(arr)))
        peak_lag = LAG_SECONDS[peak_idx]
        peak_corr = arr[peak_idx]
        print(f"\nPeak |correlation|: lag = {peak_lag:+}s, corr = {peak_corr*100:.3f}%")
        if peak_lag > 0:
            print(f"  → UM leads CC by ~{peak_lag}s")
        elif peak_lag < 0:
            print(f"  → CC leads UM by ~{-peak_lag}s (unexpected)")
        else:
            print("  → contemporaneous, no clear lead")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="20260420,20260421,20260422,20260424")
    args = ap.parse_args()
    sys.exit(run(args.days.split(",")) or 0)
