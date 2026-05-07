"""Writer round-trip + edge-case tests."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq

from calibration import EchoLogWriter
from calibration import schema as S
from calibration.tests.test_schema import (
    _build_decision, _build_fill, _build_cancel,
)


def _make_meta(session_id: str) -> S.SessionMeta:
    return S.SessionMeta(
        session_id=session_id,
        schema_version=S.SCHEMA_VERSION,
        start_wall_ns=time.time_ns(), end_wall_ns=0,
        strategy_class="echo.strategies.maker_naive.NaiveMakerStrategy",
        strategy_config_hash="abc123",
        exchange="coincheck", symbols=["BTC_JPY"], trade_symbols=["BTC_JPY"],
        hostname="testhost", az="ap-northeast-1a",
        instance_type="c7g.large", ntp_source="169.254.169.123",
        python_version="3.13.x", echo_git_sha="deadbeef", narci_git_sha="cafef00d",
    )


def test_writer_creates_layout():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "20260506-100000-coincheck-btc_jpy-naive_v1"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        sd = Path(tmp) / sid
        assert (sd / "decisions").is_dir()
        assert (sd / "fills").is_dir()
        assert (sd / "cancels").is_dir()
        assert (sd / "raw_l2").is_dir()


def test_meta_persisted_atomic():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "20260506-100000-coincheck-btc_jpy-naive_v1"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        meta = _make_meta(sid)
        w.write_session_meta(meta)
        meta_path = Path(tmp) / sid / "meta.json"
        assert meta_path.exists()
        # tmp file gone
        assert not Path(str(meta_path) + ".tmp").exists()
        with open(meta_path) as f:
            d = json.load(f)
        assert d["session_id"] == sid


def test_buffered_then_flushed_to_parquet():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "20260506-100000-coincheck-btc_jpy-naive_v1"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        w.write_decision(_build_decision())
        w.write_fill(_build_fill())
        w.write_cancel(_build_cancel())

        # before flush: no file
        assert list((Path(tmp) / sid / "decisions").glob("*.parquet")) == []
        w.flush()
        # after flush: 1 shard each
        ds = list((Path(tmp) / sid / "decisions").glob("*.parquet"))
        fs = list((Path(tmp) / sid / "fills").glob("*.parquet"))
        cs = list((Path(tmp) / sid / "cancels").glob("*.parquet"))
        assert len(ds) == 1
        assert len(fs) == 1
        assert len(cs) == 1
        # Schema preserved
        assert pq.read_table(ds[0]).schema.equals(S.DecisionEvent.PARQUET_SCHEMA)
        assert pq.read_table(fs[0]).schema.equals(S.FillEvent.PARQUET_SCHEMA)
        assert pq.read_table(cs[0]).schema.equals(S.CancelEvent.PARQUET_SCHEMA)


def test_flush_buffer_cleared():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "s1"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        w.write_decision(_build_decision())
        assert len(w._buffers["decisions"]) == 1
        w.flush()
        assert len(w._buffers["decisions"]) == 0


def test_validation_drops_bad_event():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "s2"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        bad = _build_decision()
        # Force a missing required field via slots-aware bypass
        object.__setattr__(bad, "mid_price", None)
        w.write_decision(bad)
        # Buffer should not contain it
        assert len(w._buffers["decisions"]) == 0
        assert w._dropped["decisions"] == 1


def test_buffer_cap_triggers_flush():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "s3"
        w = EchoLogWriter(session_id=sid, base_dir=tmp, max_buffer_events=3)
        for _ in range(3):
            w.write_decision(_build_decision())
        # 3rd insert should trigger inline flush
        ds = list((Path(tmp) / sid / "decisions").glob("*.parquet"))
        assert len(ds) == 1
        assert len(w._buffers["decisions"]) == 0


def test_close_idempotent_and_updates_meta_end_time():
    with tempfile.TemporaryDirectory() as tmp:
        sid = "s4"
        w = EchoLogWriter(session_id=sid, base_dir=tmp)
        meta = _make_meta(sid)
        w.write_session_meta(meta)
        w.write_decision(_build_decision())
        asyncio.run(w.close())
        # buffer flushed
        assert list((Path(tmp) / sid / "decisions").glob("*.parquet"))
        # idempotent
        asyncio.run(w.close())
        # meta.end_wall_ns updated
        with open(Path(tmp) / sid / "meta.json") as f:
            d = json.load(f)
        assert d["end_wall_ns"] > 0


def test_flush_loop_does_periodic_flush():
    async def runner():
        with tempfile.TemporaryDirectory() as tmp:
            sid = "s5"
            w = EchoLogWriter(session_id=sid, base_dir=tmp, flush_interval_sec=0.05)
            task = asyncio.create_task(w.run_flush_loop())
            w.write_decision(_build_decision())
            await asyncio.sleep(0.15)  # 3 flush cycles
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            ds = list((Path(tmp) / sid / "decisions").glob("*.parquet"))
            assert len(ds) >= 1
    asyncio.run(runner())


def test_stats_shape():
    with tempfile.TemporaryDirectory() as tmp:
        w = EchoLogWriter(session_id="s6", base_dir=tmp)
        w.write_decision(_build_decision())
        s = w.stats()
        assert s["session_id"] == "s6"
        assert s["buffered"]["decisions"] == 1
        assert s["written"]["decisions"] == 0
        w.flush()
        s = w.stats()
        assert s["buffered"]["decisions"] == 0
        assert s["written"]["decisions"] == 1


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
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(failed)
