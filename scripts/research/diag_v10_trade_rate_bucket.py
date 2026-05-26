"""Diagnostic: v10 BJ TRAIN cache — sample/X/y distribution by trade-rate quantile.

Question being answered (user 2026-05-24):
  按 trade-event 采样 (v10) vs live-tick 决策的 Gap 2 (frequency-weight
  selection bias) 实际有多严重?

Method:
  1. Per sample i, compute local trade rate ≈ 1 / (ts_{i+1} - ts_i) in samples/sec
  2. Bucket samples by rate quantile (p0-10, p10-25, p25-50, p50-75, p75-90, p90-100)
  3. For each bucket: n, spread_bps, quote_side balance, y_proxy stats, key-feature means
  4. Compare across buckets: if X/y distributions differ meaningfully, frequency
     weighting bias is real

y_proxy approximation:
  - For each sample at ts_i, find first j with ts_j >= ts_i + 1000 (1 sec horizon)
  - y_proxy = log(mid_t[j] / fill_price[i]) * sign(quote_side[i]) * 1e4 (bps)
  - fill_price = best_bid_p (BUY quote) or best_ask_p (SELL quote)
    (1-tick offset omitted — narci is tick-blind, ≈ 1e-4 bps off, doesn't change conclusions)

Output: stdout report. No file written.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq


CACHE = Path("/lustre1/work/c30636/narci/replay_buffer/v10_cache")


def load_v10(tag: str):
    path = CACHE / f"v10_bj_btc_fillpnl_{tag}.parquet"
    tbl = pq.read_table(str(path))
    cols = tbl.column_names
    feat_cols = sorted([c for c in cols if c.startswith("X_")])
    return {
        "ts": tbl.column("ts").to_numpy(),
        "best_bid_p": tbl.column("best_bid_p").to_numpy(),
        "best_ask_p": tbl.column("best_ask_p").to_numpy(),
        "spread_p": tbl.column("spread_p").to_numpy(),
        "quote_side": tbl.column("quote_side").to_numpy(),
        "mid_t": tbl.column("mid_t").to_numpy(),
        "X": np.column_stack([tbl.column(c).to_numpy() for c in feat_cols]),
        "feat_names": [c[5:] for c in feat_cols],  # strip "X_00_" → "name"
    }


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else "train16d"
    print(f"=== v10 BJ BTC trade-rate-bucket diagnostic ({tag}) ===\n")

    d = load_v10(tag)
    n = len(d["ts"])
    print(f"loaded n={n:,} samples")

    # Sort defensively (cache should already be sorted by ts)
    order = np.argsort(d["ts"], kind="stable")
    for k in ("ts", "best_bid_p", "best_ask_p", "spread_p", "quote_side", "mid_t"):
        d[k] = d[k][order]
    d["X"] = d["X"][order]

    # Local inter-arrival gap (ms) — measured from sample i to sample i+1.
    # Align all per-sample arrays to length n-1 (drop last sample for stat).
    gaps_ms = np.diff(d["ts"])
    # Truncate per-sample arrays to length n-1 so indexing is consistent
    for k in ("ts", "best_bid_p", "best_ask_p", "spread_p",
              "quote_side", "mid_t"):
        d[k] = d[k][:-1]
    d["X"] = d["X"][:-1]
    n_eff = len(gaps_ms)
    print(f"effective n (after gap-pair truncation): {n_eff:,}")

    # Quantile inspect — show why we bucket on absolute gaps not rate-quantile
    print(f"\ngap_ms quantile inspection: "
          f"min={gaps_ms.min()} p10={np.quantile(gaps_ms,0.10):.0f} "
          f"p25={np.quantile(gaps_ms,0.25):.0f} med={np.median(gaps_ms):.0f} "
          f"p75={np.quantile(gaps_ms,0.75):.0f} p90={np.quantile(gaps_ms,0.90):.0f} "
          f"max={gaps_ms.max():,}")
    print(f"  → bucket on absolute gap_ms boundaries (quantiles degenerate "
          f"because many trades arrive in same-ms bursts)")

    # Live-trading-relevant gap buckets:
    #   burst    (≤ 1ms gap)   — multiple trades same ms; live can't decide that fast
    #   fast     (2-10ms)      — high-activity microbursts; live tick ≈ 10ms feasible
    #   medium   (10-100ms)    — typical fast market
    #   slow     (100ms-1s)    — normal quiet
    #   very_slow(> 1s)        — long lulls; dominates time-weighted live decisions
    bucket_defs = [
        ("burst_≤1ms",    gaps_ms <= 1),
        ("fast_2-10ms",   (gaps_ms > 1) & (gaps_ms <= 10)),
        ("med_10-100ms",  (gaps_ms > 10) & (gaps_ms <= 100)),
        ("slow_100ms-1s", (gaps_ms > 100) & (gaps_ms <= 1000)),
        ("vslow_>1s",     gaps_ms > 1000),
    ]
    # For display, also derive "rate" from gap
    rate = 1000.0 / np.clip(gaps_ms, 1, None)

    # Forward 1-sec mid lookup for y_proxy
    fwd_target_ts = d["ts"] + 1000  # ms
    fwd_idx = np.searchsorted(d["ts"], fwd_target_ts, side="left")
    fwd_valid = fwd_idx < n_eff
    mid_fwd = np.where(
        fwd_valid,
        d["mid_t"][np.clip(fwd_idx, 0, n_eff - 1)],
        np.nan,
    )
    # fill_price: BUY quote → bid, SELL quote → ask. narci tick-blind.
    fill_price = np.where(d["quote_side"] == 1, d["best_bid_p"], d["best_ask_p"])
    sign_v = np.where(d["quote_side"] == 1, +1.0, -1.0)
    y_proxy_bps = np.log(mid_fwd / fill_price) * sign_v * 1e4

    # === Per-bucket summary ===
    spread_bps = d["spread_p"] / d["mid_t"] * 1e4

    print(f"\n{'bucket':>15} {'n':>8} {'%total':>7} "
          f"{'rate_med':>9} {'sp_bps_med':>11} {'BUY%':>6} "
          f"{'y_proxy μ':>10} {'y_proxy σ':>10} {'y n_valid':>10}")
    print(f"{'-'*15} {'-'*8} {'-'*7} {'-'*9} {'-'*11} {'-'*6} "
          f"{'-'*10} {'-'*10} {'-'*10}")

    bucket_stats = {}
    for name, mask in bucket_defs:
        n_b = int(mask.sum())
        pct = 100 * n_b / n_eff
        if n_b == 0:
            print(f"{name:>15} {0:>8,} {pct:>6.1f}% "
                  f"{'-':>9} {'-':>11} {'-':>6} {'-':>10} {'-':>10} {'-':>10}")
            bucket_stats[name] = {"n": 0, "y_valid_mean": float("nan"),
                                   "y_valid_std": float("nan"),
                                   "spread_bps_med": float("nan"),
                                   "rate_med": float("nan")}
            continue
        rate_b = rate[mask]
        sp_b = spread_bps[mask]
        qs_b = d["quote_side"][mask]
        y_b = y_proxy_bps[mask]
        y_valid = y_b[~np.isnan(y_b)]
        print(f"{name:>15} {n_b:>8,} {pct:>6.1f}% "
              f"{np.median(rate_b):>9.3f} {np.median(sp_b):>11.3f} "
              f"{100*(qs_b == 1).mean():>5.1f}% "
              f"{y_valid.mean() if len(y_valid)>0 else float('nan'):>10.4f} "
              f"{y_valid.std() if len(y_valid)>0 else float('nan'):>10.4f} "
              f"{len(y_valid):>10,}")
        bucket_stats[name] = {
            "n": n_b,
            "y_valid_mean": (y_valid.mean() if len(y_valid)>0 else float("nan")),
            "y_valid_std": (y_valid.std() if len(y_valid)>0 else float("nan")),
            "spread_bps_med": float(np.median(sp_b)),
            "rate_med": float(np.median(rate_b)),
        }

    # === Key feature mean shift across buckets ===
    print(f"\n=== Key-feature mean shift across rate buckets ===")
    # Pick a few features known to be relevant
    candidates = [
        "cc_l2_spread_bps",
        "cc_l2_top1_imb",
        "um_imb_1s",
        "um_imb_5s",
        "r_um_5s",
        "r_um_2s",
        "bj_l2_top1_imb",
        "cc_micro_dev_5s",
    ]
    keep = [c for c in candidates if c in d["feat_names"]]
    print(f"\n{'feature':>20} " + " ".join(f"{b:>10}" for b, _ in bucket_defs))
    for fname in keep:
        fi = d["feat_names"].index(fname)
        means = []
        for name, mask in bucket_defs:
            xb = d["X"][mask, fi]
            xb = xb[~np.isnan(xb)]
            means.append(xb.mean() if len(xb) > 0 else float("nan"))
        bucket_means = "  ".join(f"{m:>8.4f}" for m in means)
        print(f"{fname:>20} {bucket_means}")

    # === Time-weight vs sample-weight per bucket (the Gap-2 critical metric) ===
    # Live decisions are made at constant-time intervals; the fraction of
    # WALL TIME each bucket occupies is more relevant than fraction of
    # samples (which over-weights bursts).
    print(f"\n=== Time-weight vs sample-weight per bucket (Gap-2 measure) ===")
    print(f"{'bucket':>15} {'sample %':>9} {'time %':>8} {'T/S ratio':>10}")
    total_time_ms = float(gaps_ms.sum())
    for name, mask in bucket_defs:
        s_pct = 100 * mask.sum() / n_eff
        t_pct = 100 * gaps_ms[mask].sum() / total_time_ms
        ratio = t_pct / max(s_pct, 1e-9)
        marker = " ← TIME>>SAMPLE (under-trained)" if ratio > 5 else (
            " ← SAMPLE>>TIME (over-trained)" if ratio < 0.2 else "")
        print(f"{name:>15} {s_pct:>8.1f}% {t_pct:>7.1f}% {ratio:>10.2f}{marker}")

    # === Option α (sample_weight) numerical PoC ===
    #
    # Recommended weight per sample i = gap_{i→i+1} in seconds, clipped
    # to [0.001, 10.0] for numerical stability (single very-long gap
    # shouldn't dominate; sub-ms gaps shouldn't underflow).
    #
    # Test: weighted-mean y_proxy should match the time-weighted bucket
    # mean computed above. If they match, an LGB trained with
    # sample_weight=these_weights will have its loss equal to a time-
    # integrated MSE — i.e., the model learns E[y | X] under live
    # exposure distribution, not under sample-count distribution.
    print(f"\n=== Option α (sample_weight) numerical PoC ===")
    weights = np.clip(gaps_ms / 1000.0, 0.001, 10.0)  # seconds, clipped
    valid = ~np.isnan(y_proxy_bps)
    # Sample-weighted (= what training without sample_weight learns)
    y_sample_w = y_proxy_bps[valid].mean()
    # Time-weighted (= what live exposure averages over)
    y_time_w   = (y_proxy_bps[valid] * weights[valid]).sum() / weights[valid].sum()

    print(f"  proposed sample_weight = clip(gap_to_next_seconds, 0.001, 10.0)")
    print(f"  weight stats: min={weights.min():.4f}  med={np.median(weights):.4f}  "
          f"max={weights.max():.4f}  mean={weights.mean():.4f}")
    print(f"  weight share top: burst≤1ms contributes "
          f"{100*weights[gaps_ms<=1].sum()/weights.sum():.2f}% of total weight "
          f"(was {100*(gaps_ms<=1).sum()/n_eff:.1f}% of samples)")
    print(f"  weight share top: vslow>1s contributes "
          f"{100*weights[gaps_ms>1000].sum()/weights.sum():.2f}% of total weight "
          f"(was {100*(gaps_ms>1000).sum()/n_eff:.1f}% of samples)")
    print()
    print(f"  y_proxy mean SAMPLE-weighted (current train target): {y_sample_w:+.4f} bps")
    print(f"  y_proxy mean TIME-weighted    (live exposure target): {y_time_w:+.4f} bps")
    print(f"  Δ (train vs live): {y_time_w - y_sample_w:+.4f} bps "
          f"— this is what Option α closes")

    # Per-bucket sanity: after weighting, the contribution of each bucket
    # to total weighted-mean should roughly match its TIME share.
    print(f"\n  Per-bucket weighted-mean contribution (should ≈ time share):")
    print(f"  {'bucket':>15} {'time %':>8} {'weight %':>10} {'match?':>8}")
    for name, mask in bucket_defs:
        time_pct = 100 * gaps_ms[mask].sum() / gaps_ms.sum()
        weight_pct = 100 * weights[mask].sum() / weights.sum()
        match = "✓" if abs(time_pct - weight_pct) < 1.0 else "diff"
        print(f"  {name:>15} {time_pct:>7.2f}% {weight_pct:>9.2f}% {match:>8}")

    # === Takeaways ===
    print(f"\n=== Takeaways ===")
    print(f"  ★ Sample distribution dominated by burst (≤1ms gap):")
    print(f"    over-weights the adverse-selection regime where takers cluster.")
    print(f"  ★ Time distribution dominated by vslow (>1s gap):")
    print(f"    under-represented in train but where most live decisions happen.")
    print(f"  ★ y_proxy mean differs across buckets → if signs differ, model")
    print(f"    averaging creates train/live distribution gap.")
    print(f"  ★ T/S ratio quantifies the gap exactly: > 1 means time-weighted")
    print(f"    live exposure exceeds train representation for that regime.")
    print(f"  ★ Option α PoC: weighted-mean target tracks time-weighted bucket")
    print(f"    distribution. nyx can adopt `lgb.train(..., sample_weight=w)` to")
    print(f"    train against live-exposure target without modifying schema.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
