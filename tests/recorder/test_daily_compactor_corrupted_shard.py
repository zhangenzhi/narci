"""Regression test for DailyCompactor.compact_small_files corrupted-shard
handling.

2026-05-22 incident: narci-reco stopped gmo container 02:16:56 UTC of
2026-05-21 (PARKED decision). The container's writer left a half-written
4KB parquet (no footer magic bytes) at
`gmo/leverage/l2/BTC_JPY_RAW_20260521_021655.parquet`. Next 12:00 JST cron
run threw `pyarrow.lib.ArrowInvalid: Parquet magic bytes not found in
footer` inside `ds.dataset(files, format='parquet').to_table()`, crashing
the whole cron loop. Result: every venue iterated AFTER gmo/leverage
(bitbank/spot in this case) missed its daily compact for that UTC day,
discovered only when narci-side researcher audited cold tier next day.

Fix: pre-validate per-file via `pq.read_metadata(f)`; quarantine
corrupted shards to `<file>.corrupted` (suffix excludes them from the
RAW pattern glob next run) and skip them in the dataset call. Single
bad file no longer poisons the rest of the cron run.

This test sets up a mix of valid + corrupted shards and verifies:
  1. Valid shards still compact into the daily parquet
  2. Corrupted shards get renamed to `.corrupted` (not deleted)
  3. compact_small_files returns True (success) when at least one valid
     shard survived
  4. The aggregated row count equals only-valid shards
  5. If ALL shards are corrupted, returns False without crashing
"""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from recorder.daily_compactor import DailyCompactor


def _build_compactor(tmpdir: Path, exchange: str = "gmo",
                      market_type: str = "leverage",
                      symbol: str = "BTC_JPY") -> DailyCompactor:
    raw_dir = tmpdir / "raw"
    official_dir = tmpdir / "official"
    cold_dir = tmpdir / "cold"
    for d in (raw_dir, official_dir, cold_dir):
        d.mkdir(parents=True, exist_ok=True)

    return DailyCompactor(
        symbol=symbol,
        target_date=date(2026, 5, 21),
        raw_dir=str(raw_dir),
        official_dir=str(official_dir),
        cold_dir=str(cold_dir),
        retain_days=999,
        exchange=exchange,
        market_type=market_type,
    )


def _write_valid_shard(path: Path, n_rows: int = 100) -> None:
    tbl = pa.table({
        "timestamp": pa.array(list(range(n_rows)), type=pa.int64()),
        "side":      pa.array([2] * n_rows, type=pa.int8()),
        "price":     pa.array([100.0 + i for i in range(n_rows)]),
        "quantity":  pa.array([0.01] * n_rows),
    })
    pq.write_table(tbl, path)


def _write_corrupted_shard(path: Path) -> None:
    """Write a non-parquet 4KB blob simulating mid-write container kill."""
    with open(path, "wb") as f:
        f.write(b"PAR1" + b"\x00" * (4 * 1024 - 8) + b"BAD!")


def test_compact_skips_and_quarantines_corrupted_shards():
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor(Path(tmp))
        raw = Path(c.raw_dir)
        # 3 valid + 1 corrupted, all stamped 20260521
        valid_files = [
            raw / f"{c.symbol}_RAW_20260521_00{h:04d}.parquet" for h in (1000, 2000, 3000)
        ]
        for f in valid_files:
            _write_valid_shard(f, n_rows=100)
        corrupted = raw / f"{c.symbol}_RAW_20260521_002500.parquet"
        _write_corrupted_shard(corrupted)
        assert corrupted.exists()

        ok = c.compact_small_files()
        assert ok is True, "compact must succeed when at least one valid shard survives"

        # corrupted file got renamed to .corrupted
        assert not corrupted.exists(), "original corrupted file should be gone"
        assert corrupted.with_suffix(".parquet.corrupted").exists(), (
            "corrupted file should be quarantined with .corrupted suffix"
        )

        # all 3 valid files still present
        for f in valid_files:
            assert f.exists()

        # daily parquet has only valid rows (3 valid × 100 = 300)
        daily = Path(c.daily_file_path)
        assert daily.exists()
        df = pd.read_parquet(daily)
        assert len(df) == 300, f"expected 300 rows from 3 valid shards, got {len(df)}"


def test_compact_finds_partitioned_shards():
    """新 symbol/day 分区:shard 落在 {raw}/{SYMBOL}/{YYYYMMDD}/ 下,
    compactor 递归 glob 应能找到并聚合(数据完整性关键路径)。"""
    from recorder.wal import raw_shard_path
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor(Path(tmp))
        raw = Path(c.raw_dir)
        for hh in ("010000", "020000", "030000"):
            p = Path(raw_shard_path(str(raw), c.symbol, f"20260521_{hh}"))
            assert p.parent.name == "20260521" and p.parent.parent.name == c.symbol.upper()
            _write_valid_shard(p, n_rows=50)
        assert c.compact_small_files() is True
        df = pd.read_parquet(Path(c.daily_file_path))
        assert len(df) == 150, f"分区下 3×50 shard 应被找到聚合,得 {len(df)}"


def test_compact_returns_false_when_all_corrupted():
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor(Path(tmp))
        raw = Path(c.raw_dir)
        # 2 corrupted shards, no valid shards
        bad_files = [
            raw / f"{c.symbol}_RAW_20260521_001000.parquet",
            raw / f"{c.symbol}_RAW_20260521_002000.parquet",
        ]
        for f in bad_files:
            _write_corrupted_shard(f)

        ok = c.compact_small_files()
        assert ok is False, (
            "compact must return False when no valid shard survives"
        )
        # both files quarantined
        for f in bad_files:
            assert not f.exists()
            assert f.with_suffix(".parquet.corrupted").exists()

        # no daily parquet written
        assert not Path(c.daily_file_path).exists()


def test_compact_returns_false_when_no_files():
    """Empty dir continues to return False (existing behavior unchanged)."""
    with tempfile.TemporaryDirectory() as tmp:
        c = _build_compactor(Path(tmp))
        ok = c.compact_small_files()
        assert ok is False
