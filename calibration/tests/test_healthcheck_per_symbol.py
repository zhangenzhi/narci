"""Regression tests for per-(venue,symbol) staleness check (#85).

2026-05-23 incident: UM recorder alignment-loop death for 28 hours.
container 仍 healthy + 0 restarts because the previous healthcheck took
`max(mtime)` across the **shared narci-data volume** — CC/BJ/BS were
still writing fresh shards there, so the aggregate mtime looked fresh
even though UM's own 6 symbols (BTCUSDT/ETHUSDT/...) hadn't written
anything for 28h.

Fix: walk DATA_DIR, group shards by `(venue_relpath, symbol)`, flag any
pair whose newest shard age > PER_SYMBOL_STALE_THRESHOLD_SEC and ≤
RETIRED_AGE_THRESHOLD_SEC (PARKED venues like gmo are skipped).
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _touch_shard(root: Path, venue_rel: str, symbol: str,
                  age_sec: float) -> Path:
    """Create an empty `{SYMBOL}_RAW_{date}_{time}.parquet` file under
    `{root}/{venue_rel}/`, then set its mtime to `now - age_sec`."""
    sub = root / venue_rel
    sub.mkdir(parents=True, exist_ok=True)
    # File name needs to match _SHARD_NAME_RE: SYMBOL_RAW_YYYYMMDD_HHMMSS.parquet
    path = sub / f"{symbol}_RAW_20260524_120000.parquet"
    path.write_bytes(b"")
    now = time.time()
    os.utime(path, (now - age_sec, now - age_sec))
    return path


class TestPerSymbolStaleness(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Patch DATA_DIR + thresholds before importing/reloading module
        self._patches = []
        import deploy.healthcheck as hc
        # Snapshot original module-level values so we can restore them
        self.orig = {
            "DATA_DIR": hc.DATA_DIR,
            "PER_SYMBOL_STALE_THRESHOLD_SEC": hc.PER_SYMBOL_STALE_THRESHOLD_SEC,
            "RETIRED_AGE_THRESHOLD_SEC": hc.RETIRED_AGE_THRESHOLD_SEC,
        }
        hc.DATA_DIR = str(self.root)
        hc.PER_SYMBOL_STALE_THRESHOLD_SEC = 900       # 15 min
        hc.RETIRED_AGE_THRESHOLD_SEC = 7 * 86400      # 7 days
        self.hc = hc

    def tearDown(self):
        # Restore module-level
        for k, v in self.orig.items():
            setattr(self.hc, k, v)
        self.tmp.cleanup()

    def test_um_dead_other_venues_fresh_still_flags_um(self):
        """The exact UM 2026-05-23 incident shape:
        - UM 6 symbols all stale 28h
        - CC/BJ/BS still writing fresh
        Per-symbol check should flag UM symbols even though global mtime
        is fresh."""
        # CC/BJ/BS fresh (60s old, well within threshold)
        _touch_shard(self.root, "coincheck/spot/l2",      "BTC_JPY",  60)
        _touch_shard(self.root, "binance_jp/spot/l2",     "BTCJPY",   60)
        _touch_shard(self.root, "binance/spot/l2",        "BTCUSDT",  60)
        # UM 6 symbols all dead 28h (= 100,800s)
        for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT",
                    "XRPUSDT", "BNBUSDT"):
            _touch_shard(self.root, "binance/um_futures/l2", sym, 100_800)

        issues, info = self.hc.check_per_symbol_staleness(time.time())
        # Should have flagged at least the 6 UM symbols
        stale_msgs = [m for m in issues if "per_symbol_stale" in m
                      and "um_futures" in m]
        self.assertEqual(len(stale_msgs), 6,
                          f"expected 6 UM symbol staleness flags, got "
                          f"{len(stale_msgs)}: {stale_msgs}")
        # CC/BJ/BS should NOT be in the issues list
        for venue in ("coincheck", "binance_jp"):
            self.assertFalse(any(venue in m for m in stale_msgs),
                              f"{venue} should be fresh, not stale")
        # info reports counts
        self.assertEqual(info["per_symbol_stale"], 6)
        self.assertEqual(info["per_symbol_pairs"], 9)
        self.assertEqual(info["per_symbol_parked"], 0)

    def test_parked_venue_skipped(self):
        """gmo PARKED (>7d old shards) must NOT trigger stale alarm."""
        # gmo parked since 7+ days ago
        _touch_shard(self.root, "gmo/spot/l2",     "BTC", 10 * 86400)
        _touch_shard(self.root, "gmo/leverage/l2", "BTC_JPY", 10 * 86400)
        # Active venues fresh
        _touch_shard(self.root, "binance/um_futures/l2", "BTCUSDT", 60)

        issues, info = self.hc.check_per_symbol_staleness(time.time())
        gmo_flags = [m for m in issues if "gmo" in m]
        self.assertEqual(gmo_flags, [], f"gmo should be PARKED, not stale: "
                                         f"{gmo_flags}")
        self.assertEqual(info["per_symbol_parked"], 2)
        self.assertEqual(info["per_symbol_stale"], 0)

    def test_fresh_venues_no_issues(self):
        """All fresh → empty issues."""
        _touch_shard(self.root, "binance/um_futures/l2", "BTCUSDT", 60)
        _touch_shard(self.root, "coincheck/spot/l2",      "BTC_JPY", 60)
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        self.assertEqual(issues, [])
        self.assertEqual(info["per_symbol_stale"], 0)

    def test_just_under_threshold(self):
        """Age = threshold - 1s → still fresh."""
        _touch_shard(self.root, "binance/um_futures/l2", "BTCUSDT", 899)
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        self.assertEqual(issues, [])

    def test_just_over_threshold(self):
        """Age = threshold + 1s → flagged."""
        _touch_shard(self.root, "binance/um_futures/l2", "BTCUSDT", 901)
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        self.assertEqual(len(issues), 1)
        self.assertIn("per_symbol_stale", issues[0])
        self.assertIn("binance/um_futures/l2/BTCUSDT", issues[0])

    def test_more_than_10_stale_truncates(self):
        """If >10 stale pairs, list is truncated with summary line."""
        for i in range(15):
            _touch_shard(self.root, f"venue_{i}/l2", "SYM", 3600)
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        # 10 individual + 1 summary line
        self.assertEqual(len(issues), 11)
        self.assertIn("5 more", issues[-1])
        self.assertEqual(info["per_symbol_stale"], 15)

    def test_empty_dir_returns_empty(self):
        """Empty DATA_DIR — no parquets, no issues (caught by earlier check)."""
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        self.assertEqual(issues, [])
        self.assertEqual(info.get("per_symbol_pairs"), 0)

    def test_daily_files_ignored(self):
        """*_DAILY.parquet files should not count toward per-symbol latest."""
        # Old DAILY + no recent — should NOT prevent stale flag on RAW
        sub = self.root / "binance/um_futures/l2"
        sub.mkdir(parents=True)
        daily = sub / "BTCUSDT_RAW_20260523_DAILY.parquet"
        daily.write_bytes(b"")
        now = time.time()
        os.utime(daily, (now - 60, now - 60))  # fresh DAILY
        # RAW shard 2h old
        _touch_shard(self.root, "binance/um_futures/l2", "BTCUSDT", 7200)
        issues, info = self.hc.check_per_symbol_staleness(time.time())
        # Stale RAW shard flagged even though DAILY is fresh
        self.assertEqual(len(issues), 1)
        self.assertIn("BTCUSDT", issues[0])


if __name__ == "__main__":
    unittest.main()
