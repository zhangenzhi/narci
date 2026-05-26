"""Build v10 CC BTC fill-PnL sample cache for nyx P3 (Path 6 prep).

Sibling of build_v10_bj_btc_cache.py. Differences:
  - cc_venue_tag="cc" instead of "bj" (emit at CC trade events)
  - output filename v10_cc_btc_fillpnl_*

Same 24-day window as v9_bj_midy_36 for apples-to-apples comparison
with v10 BJ binding.

Output:
  {OUT_DIR}/v10_cc_btc_fillpnl_train16d.parquet
  {OUT_DIR}/v10_cc_btc_fillpnl_oos8d.parquet

Schema identical to v10 BJ (per nyx 602333e schema lock):
  ts + X[38] + best_bid_p + best_ask_p + spread_p + quote_side + mid_t

Note: CC trade rate ~60% of BJ (per 5/22 smoke: CC 18,926 / BJ 31,772),
so this cache will be ~60% the sample count of v10 BJ. Build wall similar
(~45-60 min, same 4-venue replay backbone).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from analytics.research.segmented_replay import (
    replay_days_parallel,
    SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL,
    QUOTE_SIDE_BUY,
    QUOTE_SIDE_SELL,
)
from analytics.features import FEATURE_NAMES


# Same as v10 BJ window (train_v9_bj_midy.py:51).
TRAIN_DAYS = [
    "20260417", "20260418", "20260419", "20260420", "20260421",
    "20260422", "20260423", "20260429", "20260430", "20260501",
    "20260502", "20260503", "20260504", "20260505", "20260508",
    "20260509",
]
OOS_DAYS = ["20260510", "20260511", "20260512", "20260513",
            "20260514", "20260515", "20260516", "20260517"]

OUT_DIR = Path("/lustre1/work/c30636/narci/replay_buffer/v10_cache")


def write_cache(out: dict, path: Path, tag: str) -> None:
    n = len(out["ts"])
    if n == 0:
        print(f"!!! {tag}: n_samples=0 !!!")
        return

    cols = {
        "ts":         pa.array(out["ts"], type=pa.int64()),
        "best_bid_p": pa.array(out["best_bid_p"], type=pa.float64()),
        "best_ask_p": pa.array(out["best_ask_p"], type=pa.float64()),
        "spread_p":   pa.array(out["spread_p"], type=pa.float64()),
        "quote_side": pa.array(out["quote_side"], type=pa.int8()),
        "mid_t":      pa.array(out["mid_t"], type=pa.float64()),
    }
    X = out["X"]
    for i, name in enumerate(FEATURE_NAMES):
        cols[f"X_{i:02d}_{name}"] = pa.array(X[:, i], type=pa.float64())
    pq.write_table(pa.table(cols), str(path))

    qs = out["quote_side"]
    sp = out["spread_p"]
    mid = out["mid_t"]
    sp_bps = sp / mid * 10000.0
    n_buy = int((qs == QUOTE_SIDE_BUY).sum())
    n_sell = int((qs == QUOTE_SIDE_SELL).sum())
    n_any_nan = int(np.any(np.isnan(X), axis=1).sum())

    print(f"\n=== {tag}: n={n:,} written to {path.name} ===")
    print(f"  quote_side:   BUY {n_buy:,} ({100*n_buy/n:.1f}%) / "
          f"SELL {n_sell:,} ({100*n_sell/n:.1f}%)")
    print(f"  spread_bps:   min={sp_bps.min():.3f}  med={np.median(sp_bps):.3f}  "
          f"p95={np.quantile(sp_bps, 0.95):.3f}  max={sp_bps.max():.3f}")
    print(f"  mid_t:        med={np.median(mid):,.0f}")
    print(f"  X.shape:      {X.shape}  any-NaN={n_any_nan:,} ({100*n_any_nan/n:.2f}%)")
    print(f"  file size:    {path.stat().st_size/1e6:.1f} MB")


def run(days: list[str], tag: str) -> None:
    print(f"\n{'='*60}\n  {tag.upper()}: {len(days)} days  {days[0]} → {days[-1]}\n{'='*60}")
    t0 = time.time()
    out = replay_days_parallel(
        days=days,
        n_workers=8,
        segment_sec=300,
        warmup_sec=300,
        prune_snapshot_dust=True,
        incremental_ready_threshold=5,
        verbose=True,
        symbol="BTC_JPY",
        cc_venue_tag="cc",            # ← only diff vs BJ builder
        sampling_mode=SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL,
    )
    wall = time.time() - t0
    print(f"\n  replay wall: {wall:.0f}s ({out['n_events_total']/wall:,.0f} ev/s)")
    write_cache(out, OUT_DIR / f"v10_cc_btc_fillpnl_{tag}.parquet", tag)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"v10 CC BTC fill-PnL cache build")
    print(f"  output dir: {OUT_DIR}")
    print(f"  schema:     ts + X[{len(FEATURE_NAMES)}] + best_bid_p + best_ask_p + "
          f"spread_p + quote_side + mid_t")
    print(f"  mode:       event_at_simulated_maker_fill")
    print(f"  venue:      cc (CC-native), symbol BTC_JPY")
    t0 = time.time()
    run(TRAIN_DAYS, "train16d")
    run(OOS_DAYS, "oos8d")
    print(f"\n{'='*60}\n  TOTAL WALL: {time.time()-t0:.0f}s ({(time.time()-t0)/60:.1f} min)\n{'='*60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
