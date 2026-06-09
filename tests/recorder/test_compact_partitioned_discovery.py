"""Regression test: compact driver must recurse the symbol/day-partitioned
RAW layout (and stay back-compatible with the old flat layout).

Background. Since 2026-05 the recorder writes RAW shards partitioned as
``l2/{SYMBOL}/{YYYYMMDD}/{SYMBOL}_RAW_{ts}.parquet`` (``wal.raw_shard_path``).
The healthcheck and ``DailyCompactor.compact_small_files`` were migrated to a
recursive ``**`` glob, but the *driver* in ``main.cmd_compact`` /
``_compact_one_dir`` still discovered symbols via flat ``os.listdir(raw_dir)``,
and ``DailyCompactor.cleanup_old_fragments`` still used a flat glob. On the
partitioned layout ``l2/`` contains only SYMBOL directories (no parquet files),
so discovery silently found **zero** symbols → produced **zero** DAILY, and
cleanup matched nothing → fragments piled up. Both are now recursive.

These tests lock that behaviour end-to-end through the public CLI entry point
(``main.cmd_compact``), exercising both the new partitioned layout and a legacy
flat file in the same venue, plus a focused unit test on the cleanup recursion.

Venue is ``coincheck`` on purpose: it's in ``NO_OFFICIAL_ARCHIVE_VENUES`` so
``validate_data`` takes the offline gap-report path and never touches the
network, keeping the test hermetic.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import main
from recorder.daily_compactor import DailyCompactor


def _write_shard(path: Path) -> None:
    """Minimal valid 4-column RAW shard (incl. a side=2 trade row)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "timestamp": [1, 2],
        "side": [0, 2],
        "price": [100.0, 100.5],
        "quantity": [1.0, 0.3],
    }).to_parquet(path)


def _past_date():
    """A date safely in the past (UTC) so archive_to_cold allows it and, with
    NARCI_RETAIN_DAYS=0, cleanup_old_fragments fires."""
    return datetime.utcnow().date() - timedelta(days=3)


def test_compact_all_handles_partitioned_and_flat(monkeypatch):
    """`compact --symbol ALL` over a partitioned + flat coincheck venue:
    both symbols discovered → DAILY produced + archived to cold; partitioned
    fragments cleaned (recursive) while the DAILY survives."""
    d = _past_date()
    ds = d.strftime("%Y%m%d")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monkeypatch.setattr(main, "current_dir", str(root))
        monkeypatch.setenv("NARCI_RETAIN_DAYS", "0")   # force fragment cleanup

        l2 = root / "replay_buffer" / "realtime" / "coincheck" / "spot" / "l2"

        # --- new partitioned layout: l2/{SYMBOL}/{YYYYMMDD}/...
        _write_shard(l2 / "BTC_JPY" / ds / f"BTC_JPY_RAW_{ds}_000000.parquet")
        _write_shard(l2 / "BTC_JPY" / ds / f"BTC_JPY_RAW_{ds}_001000.parquet")
        # --- legacy flat layout: file directly under l2/
        _write_shard(l2 / f"ETH_JPY_RAW_{ds}_000000.parquet")

        main.cmd_compact("ALL")

        # Both venues' DAILY produced at the l2/ root...
        btc_daily = l2 / f"BTC_JPY_RAW_{ds}_DAILY.parquet"
        eth_daily = l2 / f"ETH_JPY_RAW_{ds}_DAILY.parquet"
        assert btc_daily.exists(), "partitioned symbol produced no DAILY (driver not recursing?)"
        assert eth_daily.exists(), "flat symbol produced no DAILY (back-compat broken?)"

        # ...and archived to cold/{exchange}/{market}/
        cold = root / "replay_buffer" / "cold" / "coincheck" / "spot"
        assert (cold / btc_daily.name).exists(), "partitioned DAILY not archived to cold"
        assert (cold / eth_daily.name).exists(), "flat DAILY not archived to cold"

        # Partitioned fragments (in the nested subdir) must be cleaned recursively.
        import glob as _glob
        leftover = [
            f for f in _glob.glob(str(l2 / "**" / f"BTC_JPY_RAW_{ds}_*.parquet"),
                                  recursive=True)
            if "DAILY" not in f
        ]
        assert leftover == [], f"partitioned fragments not cleaned: {leftover}"
        assert btc_daily.exists(), "cleanup must not delete the DAILY itself"


def test_cleanup_old_fragments_recurses_partitioned():
    """Focused unit: cleanup_old_fragments deletes fragments living in the
    {SYMBOL}/{YYYYMMDD}/ partition subdir, and keeps the DAILY (at l2/ root)."""
    d = _past_date()
    ds = d.strftime("%Y%m%d")

    with tempfile.TemporaryDirectory() as tmp:
        raw_dir = Path(tmp) / "l2"
        part = raw_dir / "BTC_JPY" / ds
        _write_shard(part / f"BTC_JPY_RAW_{ds}_000000.parquet")
        _write_shard(part / f"BTC_JPY_RAW_{ds}_001000.parquet")

        c = DailyCompactor(
            symbol="BTC_JPY", target_date=d,
            raw_dir=str(raw_dir), official_dir=str(Path(tmp) / "off"),
            cold_dir=None, retain_days=0,
            exchange="coincheck", market_type="spot",
        )
        assert c.compact_small_files(), "merge from partition subdir failed"
        assert os.path.exists(c.daily_file_path)

        frags = list(part.glob(f"BTC_JPY_RAW_{ds}_*.parquet"))
        assert len(frags) == 2

        c.cleanup_old_fragments()

        survivors = [f for f in part.glob(f"BTC_JPY_RAW_{ds}_*.parquet")
                     if "DAILY" not in f.name]
        assert survivors == [], f"fragments not cleaned recursively: {survivors}"
        assert os.path.exists(c.daily_file_path), "DAILY must survive cleanup"
