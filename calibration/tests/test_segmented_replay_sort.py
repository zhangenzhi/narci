"""Regression test for segmented_replay sort stability.

Bug 2026-05-17 (narci #86): `rows.sort()` was full-tuple sort. For events
sharing (ts, -side, venue, side) — common for CC duplicate-ts trades
(up to ~50 events at same ms) — fallback sort by price ascending
systematically biased forward-y distributions toward negative.

Fix: `rows.sort(key=lambda r: (r[0], r[1]))` — stable sort preserves
parquet ts-asc insertion order within ties = recorder arrival order.

This test builds a synthetic 4-column raw parquet with multiple CC
trades at the same ts, runs `build_segment_worker`, and asserts:
  1. Emitted CC trade events are 1:1 with raw side==2 rows
  2. The emission order matches raw row-insertion order (NOT price-asc)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


class TestSegmentedReplaySortStability(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Build a tiny synthetic CC parquet with duplicate-ts trades in a
        # NON-price-sorted order so a buggy sort would reorder them.
        cc_dir = self.root / "coincheck" / "spot"
        cc_dir.mkdir(parents=True)
        # Day: 2026-01-01. ts in ms.
        # Schema: one snapshot pair (sd=3,4) at t0 → book is non-empty
        # so FeatureBuilder.update_event accepts trades. Then a burst of
        # 5 trades at t0+100 with prices [100, 105, 102, 110, 103] —
        # arrival order non-sorted; buggy code would reorder to
        # [100, 102, 103, 105, 110].
        t0 = 1_767_225_600_000  # 2026-01-01 00:00:00 UTC
        rows = [
            # ts, side, price, qty
            (t0,       3, 100.0, 1.0),    # bid snapshot
            (t0,       4, 101.0, 1.0),    # ask snapshot
            (t0 + 100, 2, 100.0, 0.01),   # trade 1
            (t0 + 100, 2, 105.0, 0.01),   # trade 2 (price up)
            (t0 + 100, 2, 102.0, 0.01),   # trade 3 (price down)
            (t0 + 100, 2, 110.0, 0.01),   # trade 4 (price up)
            (t0 + 100, 2, 103.0, 0.01),   # trade 5 (price down)
            # Extend day so discover_day_ts_range covers all above.
            (t0 + 1000, 3, 100.0, 1.0),
            (t0 + 1000, 4, 101.0, 1.0),
        ]
        tbl = pa.table({
            "timestamp": pa.array([r[0] for r in rows], type=pa.int64()),
            "side":      pa.array([r[1] for r in rows], type=pa.int8()),
            "price":     pa.array([r[2] for r in rows], type=pa.float64()),
            "quantity":  pa.array([r[3] for r in rows], type=pa.float64()),
        })
        self.day = "20260101"
        path = cc_dir / f"BTC_JPY_RAW_{self.day}_DAILY.parquet"
        pq.write_table(tbl, str(path))
        self.expected_trade_prices = [100.0, 105.0, 102.0, 110.0, 103.0]
        self.t0 = t0

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_ts_trades_emit_in_arrival_order(self):
        """Critical regression: cache emits CC trades in parquet row
        order, not sorted by price."""
        # Point COLD at our temp root for both module-level constants
        from research import segmented_replay as sr
        with mock.patch.object(sr, "COLD", self.root), \
             mock.patch.object(sr, "VENUE_SOURCES",
                                [("coincheck", "spot", "BTC_JPY", "cc")]):
            segs = sr.build_segments(self.day, segment_sec=300)
            self.assertGreaterEqual(len(segs), 1)
            # Process the first (only) segment — covers full day
            results = [sr.build_segment_worker(s) for s in segs]
            # Concatenate all sample arrays
            cache_ts = np.concatenate([r["ts"] for r in results
                                        if r["n_samples"] > 0])
            cache_price = np.concatenate([r["price"] for r in results
                                           if r["n_samples"] > 0])

        # Should have emitted exactly 5 CC trades
        self.assertEqual(len(cache_ts), 5,
                          f"expected 5 trades, got {len(cache_ts)}: "
                          f"ts={cache_ts.tolist()} price={cache_price.tolist()}")
        # All at the same ts
        self.assertTrue(np.all(cache_ts == self.t0 + 100))
        # Price order MUST match arrival order (insertion order in parquet)
        self.assertEqual(cache_price.tolist(), self.expected_trade_prices,
                          f"trades reordered by sort! got {cache_price.tolist()}, "
                          f"expected {self.expected_trade_prices}. If sorted asc "
                          f"the bug from 2026-05-17 has regressed.")


if __name__ == "__main__":
    unittest.main()
