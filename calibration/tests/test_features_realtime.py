"""FeatureBuilder smoke tests.

Verifies update_event + get_features + get_feature_sequence behave on
synthetic data. Doesn't validate exact feature values (that requires
spec-level review); just shape + finite-vs-NaN behavior.
"""

from __future__ import annotations

import math
import sys

import numpy as np

from features import FEATURE_NAMES, FEATURES_VERSION, FeatureBuilder


def _seed(fb: FeatureBuilder, ts0: int = 1_700_000_000_000):
    """Seed each venue with a snapshot + a few trades."""
    for v in ("cc", "bj", "um"):
        # snapshots
        fb.update_event(v, ts0, 3, 100, 0.5)
        fb.update_event(v, ts0, 3, 99, 0.5)
        fb.update_event(v, ts0, 4, 101, 0.5)
        fb.update_event(v, ts0, 4, 102, 0.5)
        # trades over 30 seconds
        for k in range(30):
            ts = ts0 + (k + 1) * 1000
            sign = 1 if k % 2 else -1
            fb.update_event(v, ts, 2, 100.5, sign * 0.01)


def test_features_version_constant():
    assert isinstance(FEATURES_VERSION, str)
    assert FEATURES_VERSION


def test_feature_names_include_baseline():
    assert "r_um" in FEATURE_NAMES
    assert "basis_bj_bps" in FEATURE_NAMES
    assert "hour_sin" in FEATURE_NAMES


def test_v4_um_flow_features_exist():
    """v4 (2026-05-10): UM signed trade flow at 4 horizons."""
    for fn in ("um_flow_50ms", "um_flow_100ms", "um_flow_500ms", "um_flow_5s"):
        assert fn in FEATURE_NAMES, fn
    assert FEATURES_VERSION >= "v4"


def test_v4_um_flow_signs_match_taker_direction():
    """Per narci convention side=2 qty<0 = taker BUY, qty>0 = taker SELL.
    flow() returns Σ(taker_buy - taker_sell). Net buy aggression should
    be positive."""
    fb = FeatureBuilder()
    ts0 = 1_700_000_000_000
    # seed both sides
    for v in ("cc", "bj", "um"):
        fb.update_event(v, ts0, 3, 100.0, 1.0)
        fb.update_event(v, ts0, 3, 99.0, 1.0)
        fb.update_event(v, ts0, 4, 101.0, 1.0)
        fb.update_event(v, ts0, 4, 102.0, 1.0)
    # 3 taker BUYs (qty<0) of 0.1 each, 1 taker SELL (qty>0) of 0.05
    fb.update_event("um", ts0 + 100, 2, 100.5, -0.10)   # buy
    fb.update_event("um", ts0 + 200, 2, 100.5, -0.10)   # buy
    fb.update_event("um", ts0 + 300, 2, 100.5, -0.10)   # buy
    fb.update_event("um", ts0 + 4000, 2, 100.5, +0.05)  # sell

    feats = fb.get_features(ts0 + 5000)
    # 5s window catches all 4: 3*0.10 buys - 0.05 sell = +0.25
    assert abs(feats["um_flow_5s"] - 0.25) < 1e-9, feats["um_flow_5s"]
    # 500ms window from ts0+5000 = cutoff ts0+4500. Only ts0+4000 within
    # range? No: ts0+4000 < cutoff ts0+4500 → not included. So 0.
    assert feats["um_flow_500ms"] == 0.0, feats["um_flow_500ms"]


def test_v5_feature_count():
    """v3 had 31 features; v4 added 4 um_flow → 35; v5 adds basis_um_bps → 36."""
    assert len(FEATURE_NAMES) == 36


def test_get_features_returns_dict_after_seeding():
    fb = FeatureBuilder()
    _seed(fb)
    feats = fb.get_features()
    assert isinstance(feats, dict)
    # all canonical names present
    for n in FEATURE_NAMES:
        assert n in feats, f"missing {n}"


def test_get_features_no_seed_returns_nans():
    """Empty builder: features should be mostly NaN, no exception."""
    fb = FeatureBuilder()
    feats = fb.get_features(ts_ms=1_700_000_000_000)
    # at least r_um etc. should be NaN since no data
    assert math.isnan(feats["r_um"])


def test_basis_finite_when_books_aligned():
    fb = FeatureBuilder()
    _seed(fb)
    feats = fb.get_features()
    # basis_bj_bps should be finite (CC and BJ have same prices)
    assert not math.isnan(feats["basis_bj_bps"])


def test_basis_um_nan_when_bs_absent():
    """v5: basis_um_bps requires BS book ready. With only cc/bj/um
    seeded, BS book is empty → basis_um_bps must be NaN. Mirrors the
    pre-2026-05-13 / Vision-backfilled-trade-only days where BS has
    no depth events."""
    fb = FeatureBuilder()
    _seed(fb)   # seeds cc/bj/um, not bs
    feats = fb.get_features()
    assert math.isnan(feats["basis_um_bps"])


def test_basis_um_finite_and_signed_correctly():
    """v5: basis_um_bps = log(um_mid / bs_mid) * 1e4.

    Setup UM mid=100 (10000 / 100), BS mid=99 (perp 1% over spot).
    Expected basis = log(100/99) * 1e4 ≈ +100.5 bps (positive = perp
    premium, contango)."""
    fb = FeatureBuilder()
    ts = 1_700_000_000_000

    # UM around 100
    fb.update_event("um", ts, 3, 99.5, 1.0)
    fb.update_event("um", ts, 4, 100.5, 1.0)
    fb.update_event("um", ts + 1, 0, 99.5, 0.5)

    # BS around 99 (1% lower = spot under perp = contango)
    fb.update_event("bs", ts, 3, 98.5, 1.0)
    fb.update_event("bs", ts, 4, 99.5, 1.0)
    fb.update_event("bs", ts + 1, 0, 98.5, 0.5)

    # Also seed cc/bj so the FB is otherwise healthy (basis_bj path not tested here).
    fb.update_event("cc", ts, 3, 100, 0.5)
    fb.update_event("cc", ts, 4, 101, 0.5)
    fb.update_event("cc", ts + 1, 0, 100.5, 0.5)
    fb.update_event("bj", ts, 3, 100, 0.5)
    fb.update_event("bj", ts, 4, 101, 0.5)
    fb.update_event("bj", ts + 1, 0, 100.5, 0.5)

    feats = fb.get_features(ts_ms=ts + 1)
    assert "basis_um_bps" in feats
    b = feats["basis_um_bps"]
    assert not math.isnan(b)
    # log(100/99) * 1e4 ≈ 100.5
    assert 90.0 < b < 110.0, f"expected ~+100 bps perp premium, got {b}"


def test_hour_features_in_range():
    fb = FeatureBuilder()
    _seed(fb)
    feats = fb.get_features()
    assert -1.0 <= feats["hour_sin"] <= 1.0
    assert -1.0 <= feats["hour_cos"] <= 1.0


def test_is_healthy_after_seed():
    fb = FeatureBuilder()
    _seed(fb)
    assert fb.is_healthy(max_stale_sec=120.0)


def test_is_healthy_returns_false_without_data():
    fb = FeatureBuilder()
    assert not fb.is_healthy()


def test_get_feature_sequence_shape():
    fb = FeatureBuilder()
    _seed(fb)
    # call get_features a few times to populate snapshot history
    for k in range(10):
        fb.update_event("cc", 1_700_000_030_000 + k * 1000, 2, 100.5, 0.001)
        fb.get_features()
    arr = fb.get_feature_sequence(window_sec=5, step_sec=1)
    if arr is not None:
        # shape (5, len(FEATURE_NAMES))
        assert arr.shape[0] == 5
        assert arr.shape[1] == len(FEATURE_NAMES)


def test_tier2_features_finite_after_book_updates():
    """All tier2 (L2 imbalance / microprice) should be finite once the
    book has been updated with bid+ask snapshot + at least one book event.
    Regression from v1 where these were placeholder NaN."""
    import math
    fb = FeatureBuilder(lookback_seconds=300)
    ts = 1_700_000_000_000
    # snapshots — populate book_metric_history
    for v in ("cc", "bj"):
        fb.update_event(v, ts, 3, 100, 0.5)
        fb.update_event(v, ts, 3, 99, 0.3)
        fb.update_event(v, ts, 4, 101, 0.4)
        fb.update_event(v, ts, 4, 102, 0.2)
        # P2.1 contract: close the snapshot batch via a diff event so the
        # first _record_book_metric sample fires (mid-batch sampling
        # produces dust-bid outliers — see test_no_mid_batch_micro_dev_outlier).
        fb.update_event(v, ts + 1, 0, 100.5, 0.5)
    # also feed UM (basis_um needs UM mid)
    fb.update_event("um", ts, 3, 100, 0.5)
    fb.update_event("um", ts, 4, 101, 0.5)
    fb.update_event("um", ts + 1, 0, 100.5, 0.5)
    # need at least one trade for r_cc lag
    for v in ("cc", "bj", "um"):
        for k in range(5):
            fb.update_event(v, ts + 1000 * k, 2, 100.5, 0.001)

    feats = fb.get_features()
    tier2 = [
        "cc_l2_top1_imb", "cc_l2_top5_imb",
        "cc_l2_imb_top1_5s", "cc_l2_imb_top5_5s",
        "cc_l2_micro_dev_bps", "cc_micro_dev_5s",
        "cc_l2_spread_bps",
        "bj_l2_top1_imb", "bj_l2_top5_imb", "l2_imb_diff",
    ]
    for f in tier2:
        assert not math.isnan(feats[f]), f"{f} should be finite, got NaN"


def test_long_window_no_count_truncation():
    """Regression: maxlen=400 used to silently drop 30s data on UM
    (14 trades/sec → 30s × 14 = 420 trades > 400). Now history is
    time-bounded, not count-bounded."""
    fb = FeatureBuilder(lookback_seconds=300)
    ts0 = 1_700_000_000_000
    # snapshots so book ready
    for v in ("um", "cc", "bj"):
        fb.update_event(v, ts0, 3, 100, 0.5)
        fb.update_event(v, ts0, 4, 101, 0.5)
    # 14 UM trades/sec for 30 seconds = 420 trades. All should be retained.
    for k in range(420):
        ts = ts0 + (k * 71)  # 71 ms intervals → 14.08 trades/sec
        fb.update_event("um", ts, 2, 100.5, (-1) ** k * 0.01)
    # All 420 in window since lookback=300s
    um_state = fb._venues["um"]
    assert len(um_state.trades.history) == 420
    # 30s imbalance should reflect all 420 trades, not just last 400
    feats = fb.get_features(ts0 + 420 * 71)
    # imb_30s should be near 0 since alternating signs
    assert abs(feats["um_imb_30s_norm"]) < 0.05


def test_trim_drops_old_data():
    """When new event arrives with ts > old + lookback, old data is trimmed."""
    fb = FeatureBuilder(lookback_seconds=10)  # 10s lookback for fast test
    ts0 = 1_700_000_000_000
    for v in ("um", "cc", "bj"):
        fb.update_event(v, ts0, 3, 100, 0.5)
        fb.update_event(v, ts0, 4, 101, 0.5)
    # Inject 100 trades in window
    for k in range(100):
        fb.update_event("um", ts0 + k * 50, 2, 100.5, 0.001)
    assert len(fb._venues["um"].trades.history) == 100
    # Far-future event should trim everything past window
    fb.update_event("um", ts0 + 60_000, 2, 100.5, 0.001)  # 60s later
    # Only the last (60s event) should remain
    assert len(fb._venues["um"].trades.history) == 1


def test_metric_sample_throttle_caps_at_interval():
    """P2 fix 2026-05-09: snapshot-batch throttle bypass regression.

    A snapshot batch consists of thousands of side=3/4 events sharing the
    same ts. The pre-fix throttle ("only update last_metric_ts on success")
    let every event in the batch attempt _record_book_metric — which
    failed during early-bootstrap (one side empty), didn't lock the
    throttle, and so the next event tried again. Profile showed 67,651
    metric attempts on a 3-min UM slice (94x over the expected ~720).

    This test seeds the book, closes the snapshot batch with a diff
    event, then injects 1000 side=4 events at a NEW snapshot ts and
    asserts that book_metric_history grows by AT MOST 1 (not 1000)."""
    from features import FeatureBuilder

    fb = FeatureBuilder(metric_sample_interval_ms=500)
    base_ts = 1_700_000_000_000
    # Seed both sides via snapshot.
    fb.update_event("um", base_ts, 3, 100.0, 1.0)
    fb.update_event("um", base_ts, 4, 101.0, 1.0)
    # Close batch + advance throttle by 500ms via a diff event.
    fb.update_event("um", base_ts + 500, 0, 100.5, 0.5)
    initial = len(fb._venues["um"].book_metric_history)
    assert initial >= 1, "expected at least one metric sample after batch close"

    # Inject 1000 side=4 events at the SAME ts (simulating a snapshot batch).
    for i in range(1000):
        fb.update_event("um", base_ts + 1000, 4, 110.0 + i, 0.1)

    final = len(fb._venues["um"].book_metric_history)
    # Throttle limits to 1 sample per 500ms; same-ts events all blocked.
    # P2.1 additionally suppresses sampling DURING the batch.
    assert final - initial <= 1, (
        f"throttle bypass regression: {final - initial} samples added during "
        f"same-ts snapshot batch (expected ≤ 1)"
    )


def test_metric_sample_skipped_when_one_side_empty():
    """The throttle gate must not burn its window on a sample attempt
    when one side of the book is still empty (mid-init). P2.1 extends
    this to "also when we are inside a snapshot batch": the first
    sample fires only after a non-snapshot event closes the batch."""
    from features import FeatureBuilder

    fb = FeatureBuilder(metric_sample_interval_ms=500)
    base_ts = 1_700_000_000_000

    # Feed only bid snapshot — asks empty.
    fb.update_event("bj", base_ts, 3, 100.0, 1.0)
    fb.update_event("bj", base_ts, 3, 99.0, 1.0)
    # No sample possible (asks empty), throttle should NOT lock.
    assert len(fb._venues["bj"].book_metric_history) == 0

    # Add ask side at SAME ts. Both sides are now populated, but the
    # snapshot batch is still open (P2.1) — no sample yet.
    fb.update_event("bj", base_ts, 4, 101.0, 1.0)
    assert len(fb._venues["bj"].book_metric_history) == 0, (
        "should not sample inside a snapshot batch"
    )

    # A diff event closes the batch — first sample fires here.
    fb.update_event("bj", base_ts + 1, 0, 100.5, 0.5)
    assert len(fb._venues["bj"].book_metric_history) >= 1


def test_no_mid_batch_micro_dev_outlier():
    """P2.1 fix 2026-05-13 (segment-0 outlier).

    Reproduces the day-boundary cc_micro_dev_5s = -1469 outlier that
    nyx flagged. Pre-fix mechanism:
      1. Snapshot batch arrives sorted by (ts, -side) then by price
         ascending → side=4 events fully populate asks first, then
         side=3 events arrive lowest-price first.
      2. The first side=3 event adds the deepest dust bid (e.g. 60% of
         market price) → book has 1 bid level + 347 ask levels.
      3. Pre-P2.1 throttle gate passes (both sides non-empty) →
         _record_book_metric fires → micropx weighted heavily to dust
         bid → micro_dev_bps ~= -2900 written to history.
      4. 5s rolling later picks up this entry, polluting cc_micro_dev_5s
         for the next 5 seconds.
    """
    from features import FeatureBuilder

    fb = FeatureBuilder(metric_sample_interval_ms=500)
    base_ts = 1_700_000_000_000

    # Simulate the snapshot batch in the worst-case order: asks first
    # (small to large), then bids (small to large — so dust bid lands
    # before legitimate top bids).
    for p in [101.0, 102.0, 105.0, 110.0]:          # ask side, ascending
        fb.update_event("cc", base_ts, 4, p, 1.0)
    # Bid side: first event is the deep dust bid, then legit ones.
    fb.update_event("cc", base_ts, 3, 50.0, 0.001)  # dust deep bid
    fb.update_event("cc", base_ts, 3, 95.0, 1.0)
    fb.update_event("cc", base_ts, 3, 99.0, 1.0)
    fb.update_event("cc", base_ts, 3, 100.0, 1.0)

    # Batch is still open at this point — no sample should have fired.
    assert len(fb._venues["cc"].book_metric_history) == 0, (
        "P2.1 contract: no sample inside open snapshot batch"
    )

    # A diff event closes the batch. With prune_snapshot_dust default
    # off, the dust bid stays in the book, but get_state heapq.nlargest
    # picks the TOP-5 = legit bids → micropx is sane.
    fb.update_event("cc", base_ts + 100, 0, 99.5, 0.5)
    hist = fb._venues["cc"].book_metric_history
    assert len(hist) >= 1, "expected sample after batch close"
    md = hist[-1][3]  # micro_dev_bps
    assert abs(md) < 50, (
        f"micro_dev_bps after batch close should be sane, got {md} "
        f"(pre-fix would write ~ -2900 from the dust-only top-1 state)"
    )


def test_stats_shape():
    fb = FeatureBuilder()
    _seed(fb)
    s = fb.stats()
    assert s["features_version"] == FEATURES_VERSION
    assert s["feature_count"] == len(FEATURE_NAMES)
    assert "cc" in s["venues_state"]


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
