"""Regression test for DailyCompactor.archive_to_cold UTC-day guard.

2026-05-18 incident: a manual `python main.py compact --symbol ALL` invoked
at 12:30 JST = 03:30 UTC produced 72 venue/symbol partial daily parquets
for the in-progress UTC day (only ~2.5h of 24h). archive_to_cold's
idempotent skip then locked the partial versions in cold tier permanently
(re-running compact rebuilt the realtime daily from fresh fragments but
the cold copy didn't update). Files had to be manually deleted and
re-compacted with --date yesterday-UTC.

Fix: archive_to_cold refuses to archive when target_date >= today UTC.
Realtime daily is left alone (it's regenerated each run anyway); only
the cold archive step is blocked, with a clear WARNING log.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from data.daily_compactor import DailyCompactor


def _build_compactor_with_fake_daily(tmpdir: Path, target: date) -> DailyCompactor:
    """Construct a DailyCompactor with a stubbed-in realtime daily file
    so archive_to_cold has something to copy."""
    raw_dir = tmpdir / "raw"
    official_dir = tmpdir / "official"
    cold_dir = tmpdir / "cold"
    for d in (raw_dir, official_dir, cold_dir):
        d.mkdir(parents=True, exist_ok=True)

    c = DailyCompactor(
        symbol="BTC_JPY",
        target_date=target,
        raw_dir=str(raw_dir),
        official_dir=str(official_dir),
        cold_dir=str(cold_dir),
        retain_days=999,
        exchange="coincheck",
        market_type="spot",
    )
    # Write a minimal realtime daily parquet so archive_to_cold doesn't
    # early-return on missing source.
    tbl = pa.table({
        "timestamp": pa.array([1, 2, 3], type=pa.int64()),
        "side":      pa.array([2, 2, 2], type=pa.int8()),
        "price":     pa.array([100.0, 101.0, 102.0]),
        "quantity":  pa.array([0.01, 0.02, 0.03]),
    })
    pq.write_table(tbl, c.daily_file_path)
    return c


def test_archive_to_cold_refuses_today_utc(caplog):
    from datetime import datetime
    today_utc = datetime.utcnow().date()
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor_with_fake_daily(Path(tmp), today_utc)
        cold_dest = os.path.join(c.cold_dir, os.path.basename(c.daily_file_path))
        with caplog.at_level(logging.WARNING):
            c.archive_to_cold()
        assert not os.path.exists(cold_dest), (
            f"archive_to_cold must refuse to copy in-progress UTC day "
            f"({today_utc}); cold file unexpectedly created at {cold_dest}")
        assert any("拒绝归档" in r.message and "尚未结束" in r.message
                    for r in caplog.records), (
            "expected a WARNING log line explaining the refusal; got: "
            + repr([r.message for r in caplog.records]))


def test_archive_to_cold_refuses_future_date(caplog):
    """A target_date in the future is also refused (same guard branch)."""
    from datetime import datetime
    future = datetime.utcnow().date() + timedelta(days=3)
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor_with_fake_daily(Path(tmp), future)
        cold_dest = os.path.join(c.cold_dir, os.path.basename(c.daily_file_path))
        with caplog.at_level(logging.WARNING):
            c.archive_to_cold()
        assert not os.path.exists(cold_dest)
        assert any("拒绝归档" in r.message for r in caplog.records)


def test_archive_to_cold_allows_yesterday_utc():
    """Sanity: yesterday UTC (or earlier) must still archive normally."""
    from datetime import datetime
    yesterday = datetime.utcnow().date() - timedelta(days=1)
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor_with_fake_daily(Path(tmp), yesterday)
        cold_dest = os.path.join(c.cold_dir, os.path.basename(c.daily_file_path))
        c.archive_to_cold()
        assert os.path.exists(cold_dest), (
            f"yesterday UTC ({yesterday}) must be archivable; cold file "
            f"missing at {cold_dest}")
