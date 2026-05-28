"""Tests for data/wal.py — 段式 WAL(P1 录制止血,2026-05-26)。

覆盖:
  1. append/flush/harvest/discard 往返一致(顺序保留)。
  2. flush 原子写:写中途异常不留半个有效段,只留可丢弃的 .tmp。
  3. _next_seq 续接已有段编号,重启不覆盖未恢复的残留段。
  4. recover_orphans 把残留段合并成规范 RAW 并删段。
  5. 段用 .segwal 扩展名 —— 扫 *.parquet 的消费方看不到。
"""
from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

from recorder import wal as walmod
from recorder.wal import SegmentWAL, recover_orphans, write_parquet_atomic, SEGMENT_SUFFIX


def _rows(n, base_ts=1000):
    # [timestamp, side, price, quantity]
    return [[base_ts + i, i % 5, 100.0 + i, 1.0 + i] for i in range(n)]


@pytest.fixture
def wal_dir():
    d = tempfile.mkdtemp(prefix="narci_wal_")
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def test_roundtrip_preserves_order(wal_dir):
    w = SegmentWAL(wal_dir, "btcjpy")
    w.append(_rows(3, base_ts=1000))
    p1 = w.flush()
    w.append(_rows(2, base_ts=2000))
    p2 = w.flush()

    assert p1 and p2 and p1 != p2
    assert os.path.basename(p1).endswith(SEGMENT_SUFFIX)

    df, paths = w.harvest()
    assert paths == [p1, p2]
    assert list(df["timestamp"]) == [1000, 1001, 1002, 2000, 2001]
    assert list(df.columns) == ["timestamp", "side", "price", "quantity"]


def test_flush_empty_is_noop(wal_dir):
    w = SegmentWAL(wal_dir, "btcjpy")
    assert w.flush() is None
    assert w.segment_paths() == []
    df, paths = w.harvest()
    assert df is None and paths == []


def test_discard_only_removes_given_paths(wal_dir):
    w = SegmentWAL(wal_dir, "btcjpy")
    w.append(_rows(1, 1000)); p1 = w.flush()
    w.append(_rows(1, 2000)); p2 = w.flush()
    w.discard([p1])
    assert not os.path.exists(p1)
    assert os.path.exists(p2)
    assert w.segment_paths() == [p2]


def test_atomic_write_leaves_no_partial_segment(wal_dir, monkeypatch):
    """模拟 to_parquet 写到一半崩溃:目标段不应作为有效文件出现,
    最多留下可丢弃的 .tmp。"""
    w = SegmentWAL(wal_dir, "btcjpy", fsync=False)
    w.append(_rows(3))

    real_to_parquet = pd.DataFrame.to_parquet

    def boom(self, path, *a, **k):
        # 写 tmp 写一半就崩:先落一个坏 tmp 再抛
        with open(path, "wb") as f:
            f.write(b"corrupt-half-written")
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(RuntimeError):
        w.flush()

    # 没有任何有效 .segwal 段出现(os.replace 从未发生)
    segs = [f for f in os.listdir(wal_dir) if f.endswith(SEGMENT_SUFFIX)]
    assert segs == [], segs


def test_next_seq_continues_after_restart(wal_dir):
    w1 = SegmentWAL(wal_dir, "btcjpy")
    w1.append(_rows(1, 1000)); p1 = w1.flush()
    w1.append(_rows(1, 2000)); p2 = w1.flush()

    # 模拟进程重启:新 WAL 实例扫到残留段,seq 应续接而非覆盖。
    w2 = SegmentWAL(wal_dir, "btcjpy")
    w2.append(_rows(1, 3000)); p3 = w2.flush()
    assert p3 not in (p1, p2)
    assert sorted(os.listdir(wal_dir)) == [
        os.path.basename(p) for p in sorted([p1, p2, p3])
    ]


def test_recover_orphans_merges_segments_to_raw(wal_dir):
    raw_dir = tempfile.mkdtemp(prefix="narci_raw_")
    try:
        w = SegmentWAL(wal_dir, "btcjpy")
        w.append(_rows(2, 1000)); w.flush()
        w.append(_rows(2, 2000)); w.flush()
        # 第二个 symbol 也有残留
        w2 = SegmentWAL(wal_dir, "ethjpy")
        w2.append(_rows(1, 5000)); w2.flush()

        recovered = recover_orphans(wal_dir, raw_dir, fsync=False)
        assert len(recovered) == 2

        # 段已被删
        assert [f for f in os.listdir(wal_dir) if f.endswith(SEGMENT_SUFFIX)] == []
        # 产出**分区** RAW:{raw_dir}/{SYMBOL}/{YYYYMMDD}/{SYMBOL}_RAW_*.parquet
        import glob as _glob
        raws = _glob.glob(os.path.join(raw_dir, "**", "*_RAW_*.parquet"), recursive=True)
        btc = [f for f in raws if os.path.basename(f).startswith("BTCJPY_RAW_")]
        eth = [f for f in raws if os.path.basename(f).startswith("ETHJPY_RAW_")]
        assert len(btc) == 1 and len(eth) == 1
        # 验证 symbol/day 分区结构:.../BTCJPY/{8位日期}/BTCJPY_RAW_...
        parts = btc[0].split(os.sep)
        assert parts[-3] == "BTCJPY" and len(parts[-2]) == 8 and parts[-2].isdigit()
        df = pd.read_parquet(btc[0])
        assert list(df["timestamp"]) == [1000, 1001, 2000, 2001]
    finally:
        import shutil
        shutil.rmtree(raw_dir, ignore_errors=True)


def test_segments_invisible_to_parquet_scanners(wal_dir):
    """段扩展名 .segwal,任何 endswith('.parquet') 的扫描都看不到。"""
    w = SegmentWAL(wal_dir, "btcjpy")
    w.append(_rows(1)); w.flush()
    found = []
    for root, _, files in os.walk(wal_dir):
        found += [f for f in files if f.endswith(".parquet")]
    assert found == []


def test_write_parquet_atomic_roundtrip(wal_dir):
    df = pd.DataFrame(_rows(4), columns=["timestamp", "side", "price", "quantity"])
    path = os.path.join(wal_dir, "x.parquet")
    write_parquet_atomic(df, path, fsync=False)
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    back = pd.read_parquet(path)
    assert len(back) == 4
