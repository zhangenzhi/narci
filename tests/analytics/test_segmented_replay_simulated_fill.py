"""Regression tests for the `event_at_simulated_maker_fill` sampling mode.

Added 2026-05-23 per nyx 2026-05-23 Phase 1 ask (676b734 + c13d657 +
602333e schema lock) for the v10 conditional fill-PnL binding family.

Tests cover:
  1. Emit cardinality == opposite-direction taker count
  2. quote_side encoding: SELL taker (qty<0) → BUY(1), BUY taker (qty>0) → SELL(2)
  3. Schema matches the nyx-locked 7-field layout (ts + X[36] + best_bid_p +
     best_ask_p + spread_p + quote_side + mid_t), and `price` is absent
  4. Top-of-book values are consistent with the snapshot fed in (spread_p
     == ask - bid, mid_t == (bid+ask)/2)
  5. Skip-on-not-ready: trades arriving before any book snapshot don't emit
  6. Legacy mode (default `event_at_cc_trade`) still returns the
     3-field schema unchanged — no regression for v9 callers
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def _make_cc_parquet(root: Path, day: str, rows: list[tuple]) -> Path:
    """Drop a minimal CC parquet into a temp `coincheck/spot/` cold layout.

    Each row tuple: (ts_ms, side, price, qty). Caller assembles a snapshot
    pair (side 3 + 4) before any trades so the book becomes ready.
    """
    cc_dir = root / "coincheck" / "spot"
    cc_dir.mkdir(parents=True, exist_ok=True)
    tbl = pa.table({
        "timestamp": pa.array([r[0] for r in rows], type=pa.int64()),
        "side":      pa.array([r[1] for r in rows], type=pa.int8()),
        "price":     pa.array([r[2] for r in rows], type=pa.float64()),
        "quantity":  pa.array([r[3] for r in rows], type=pa.float64()),
    })
    path = cc_dir / f"BTC_JPY_RAW_{day}_DAILY.parquet"
    pq.write_table(tbl, str(path))
    return path


class TestSimulatedMakerFillEmission(unittest.TestCase):
    """Tests #1-#5: trigger / encoding / schema / book values / ready-skip."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Day: 2026-01-01. Single CC venue, snapshot then 4 trades.
        # Snapshot: bid=100, ask=102 → mid=101, spread=2.
        # Trade pattern: SELL / BUY / SELL / BUY (4 emits expected).
        t0 = 1_767_225_600_000
        self.day = "20260101"
        self.t0 = t0
        rows = [
            # Snapshot pair: makes book ready.
            (t0,        3, 100.0, 1.0),    # bid
            (t0,        4, 102.0, 1.0),    # ask
            # Trades (all at t0+10s, after warmup). Negative qty = SELL taker.
            (t0 +  10_000, 2, 100.0, -0.01),  # SELL → emit BUY quote
            (t0 +  10_001, 2, 102.0,  0.01),  # BUY  → emit SELL quote
            (t0 +  10_002, 2, 100.0, -0.02),  # SELL → emit BUY quote
            (t0 +  10_003, 2, 102.0,  0.02),  # BUY  → emit SELL quote
            # Extend day so discover_day_ts_range covers all above.
            (t0 + 100_000, 3, 100.0, 1.0),
            (t0 + 100_000, 4, 102.0, 1.0),
        ]
        _make_cc_parquet(self.root, self.day, rows)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_single_segment(self, sampling_mode: str) -> dict:
        from analytics import segmented_replay as sr
        with mock.patch.object(sr, "COLD", self.root), \
             mock.patch.object(sr, "VENUE_SOURCES",
                                [("coincheck", "spot", "BTC_JPY", "cc")]):
            segs = sr.build_segments(self.day, segment_sec=300)
            # Use warmup=0 so the t0 snapshot is included even when seg_start
            # equals t0 (discover_day_ts_range returns t0 as first_ts).
            results = [sr.build_segment_worker(
                s, warmup_sec=0, sampling_mode=sampling_mode) for s in segs]
        return results

    def test_emit_count_and_quote_side_encoding(self):
        """Tests #1 + #2: 4 trades, 4 emits, alternating BUY/SELL."""
        from analytics import segmented_replay as sr
        results = self._run_single_segment(
            sr.SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)

        ts_all = np.concatenate([r["ts"] for r in results if r["n_samples"] > 0])
        qs_all = np.concatenate(
            [r["quote_side"] for r in results if r["n_samples"] > 0])

        self.assertEqual(len(ts_all), 4,
                          f"expected 4 emits (4 trades), got {len(ts_all)}: ts={ts_all.tolist()}")
        # SELL/BUY/SELL/BUY → BUY/SELL/BUY/SELL quote_side
        self.assertEqual(qs_all.tolist(),
                          [sr.QUOTE_SIDE_BUY, sr.QUOTE_SIDE_SELL,
                           sr.QUOTE_SIDE_BUY, sr.QUOTE_SIDE_SELL])

    def test_extended_schema_present_and_legacy_absent(self):
        """Test #3: required keys present, `price` absent."""
        from analytics import segmented_replay as sr
        results = self._run_single_segment(
            sr.SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)

        first = next(r for r in results if r["n_samples"] > 0)
        required = {"ts", "X", "best_bid_p", "best_ask_p", "spread_p",
                    "quote_side", "mid_t"}
        missing = required - set(first)
        self.assertFalse(missing, f"missing schema fields: {missing}")
        self.assertNotIn("price", first,
                          "legacy `price` key must not be returned in sim-fill mode")
        # dtype sanity
        self.assertEqual(first["ts"].dtype, np.int64)
        self.assertEqual(first["quote_side"].dtype, np.int8)
        self.assertEqual(first["best_bid_p"].dtype, np.float64)

    def test_book_values_consistent(self):
        """Test #4: best_bid/ask/spread/mid values match snapshot."""
        from analytics import segmented_replay as sr
        results = self._run_single_segment(
            sr.SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)

        bid_all = np.concatenate(
            [r["best_bid_p"] for r in results if r["n_samples"] > 0])
        ask_all = np.concatenate(
            [r["best_ask_p"] for r in results if r["n_samples"] > 0])
        sp_all = np.concatenate(
            [r["spread_p"] for r in results if r["n_samples"] > 0])
        mid_all = np.concatenate(
            [r["mid_t"] for r in results if r["n_samples"] > 0])

        np.testing.assert_array_equal(bid_all, [100.0, 100.0, 100.0, 100.0])
        np.testing.assert_array_equal(ask_all, [102.0, 102.0, 102.0, 102.0])
        np.testing.assert_array_equal(sp_all, [2.0, 2.0, 2.0, 2.0])
        np.testing.assert_array_equal(mid_all, [101.0, 101.0, 101.0, 101.0])

    def test_skip_when_book_not_ready(self):
        """Test #5: trades before any snapshot don't emit."""
        from analytics import segmented_replay as sr

        # Build a fixture where a trade precedes the snapshot
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t0 = 1_767_225_600_000
            rows = [
                # Trade before any snapshot — book not ready
                (t0,        2, 100.0, -0.01),
                # Snapshot afterward
                (t0 +  500, 3, 100.0, 1.0),
                (t0 +  500, 4, 102.0, 1.0),
                # Trade after snapshot — should emit
                (t0 + 1000, 2, 100.0, -0.01),
                # Tail
                (t0 + 5000, 3, 100.0, 1.0),
                (t0 + 5000, 4, 102.0, 1.0),
            ]
            _make_cc_parquet(root, "20260102", rows)
            with mock.patch.object(sr, "COLD", root), \
                 mock.patch.object(sr, "VENUE_SOURCES",
                                    [("coincheck", "spot", "BTC_JPY", "cc")]):
                segs = sr.build_segments("20260102", segment_sec=300)
                results = [sr.build_segment_worker(
                    s, warmup_sec=0,
                    sampling_mode=sr.SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL)
                    for s in segs]
            ts_all = np.concatenate(
                [r["ts"] for r in results if r["n_samples"] > 0])
            self.assertEqual(len(ts_all), 1,
                              f"expected 1 emit (skip pre-snapshot trade), got "
                              f"{len(ts_all)}: ts={ts_all.tolist()}")
            self.assertEqual(int(ts_all[0]), t0 + 1000)

    def test_legacy_mode_schema_unchanged(self):
        """Test #6: default sampling_mode still returns ts/price/X."""
        from analytics import segmented_replay as sr
        results = self._run_single_segment(
            sr.SAMPLING_MODE_EVENT_AT_CC_TRADE)
        first = next(r for r in results if r["n_samples"] > 0)
        self.assertIn("price", first)
        self.assertNotIn("best_bid_p", first)
        self.assertNotIn("quote_side", first)


class TestSimulatedMakerFillEnums(unittest.TestCase):
    """Cross-module sanity: enum values agree with calibration.alpha_models."""

    def test_sampling_mode_in_alpha_models(self):
        from analytics.calibration.alpha_models import SAMPLING_MODES
        from analytics.segmented_replay import (
            SAMPLING_MODE_EVENT_AT_CC_TRADE,
            SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL,
        )
        self.assertIn(SAMPLING_MODE_EVENT_AT_CC_TRADE, SAMPLING_MODES)
        self.assertIn(SAMPLING_MODE_EVENT_AT_SIMULATED_MAKER_FILL, SAMPLING_MODES)

    def test_target_kind_registered(self):
        from analytics.calibration.alpha_models import TARGET_KINDS
        self.assertIn("fill_pnl_1s_log_return", TARGET_KINDS)


if __name__ == "__main__":
    unittest.main()
