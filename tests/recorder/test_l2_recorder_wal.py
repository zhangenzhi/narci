"""Regression tests for L2Recorder WAL 改造(P1 录制止血,2026-05-26)。

锁定三个修复:
  1. 未对齐 symbol 的 trade 段照样落盘(旧 bug:save_loop 在 not
     stream_aligned 时整段跳过 → trade 在内存无限堆积且永不落盘)。
  2. 对齐成功后处理对齐事件**及其后全部**事件(旧 bug:找到首个对齐
     事件即 return,丢弃其后已到达的有效事件)。
  3. 熔断硬退出(os._exit)前先 flush micro-buffer 到段,数据交给
     recover_orphans 恢复(旧 bug:os._exit 绕过 flush,丢内存 buffer)。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

import pytest

from recorder.l2_recorder import L2Recorder, _WalBuffer
from recorder.wal import SegmentWAL, SEGMENT_SUFFIX
import pandas as pd


class _StubAdapter:
    name = "test"
    market_type = "spot"

    def __init__(self, needs_align=True):
        self._needs_align = needs_align

    def ws_url(self, symbols, interval_ms=100):
        return "wss://example.invalid/test"

    def ws_urls(self, symbols, interval_ms=100):
        return [self.ws_url(symbols, interval_ms)]

    def subscribe_messages(self, symbols):
        return []

    def uses_custom_stream(self):
        return False

    async def fetch_snapshot(self, symbol):
        return {"bids": [], "asks": []}

    def parse_snapshot(self, data):
        return 100, []

    def parse_message(self, msg):
        return None, None, None

    def standardize_event(self, event_type, data, now_ms=None):
        # 测试事件 dict 形如 {"ts","u","side","price","qty"}。
        return [[data["ts"], data["side"], data["price"], data["qty"]]]

    def needs_alignment(self):
        return self._needs_align

    def try_align(self, snapshot_update_id, event):
        # 对齐 := event 的 u 恰为 snapshot_update_id + 1。
        return event.get("u") == snapshot_update_id + 1

    def get_update_id(self, event):
        return event.get("u", 0)


def _make_recorder(symbol="btcjpy", needs_align=True):
    tmpdir = tempfile.mkdtemp(prefix="narci_wal_rec_")
    rec = L2Recorder(config_path="/nonexistent.yaml", symbol=symbol,
                     adapter=_StubAdapter(needs_align=needs_align))
    # 把落盘 + WAL 都重定向进 tmpdir,fsync 关掉加速。
    rec.save_dir = os.path.join(tmpdir, "raw")
    os.makedirs(rec.save_dir, exist_ok=True)
    rec.wal_dir = os.path.join(tmpdir, "wal")
    rec.wal_fsync = False
    rec.wals = {s: SegmentWAL(rec.wal_dir, s, fsync=False) for s in rec.symbols}
    rec.buffers = {s: _WalBuffer(rec.wals[s]) for s in rec.symbols}
    return rec, tmpdir


def test_unaligned_symbol_trades_get_flushed():
    """stream_aligned=False 时,已入 WAL 的 trade 段仍被 _compact_symbol
    合并落盘 —— 不再被对齐状态 gate。"""
    rec, tmpdir = _make_recorder()
    try:
        sym = "btcjpy"
        assert rec.stream_aligned[sym] is False  # 从未对齐
        # 模拟 record_stream 的 trade 路径:无条件入 WAL。
        rec.wals[sym].append([[1000, 2, 50.0, 0.3], [1001, 2, 50.1, -0.2]])

        raw_path = asyncio.run(rec._compact_symbol(sym))

        assert raw_path is not None, "未对齐 symbol 的 trade 应被落盘"
        assert os.path.exists(raw_path)
        df = pd.read_parquet(raw_path)
        assert list(df["timestamp"]) == [1000, 1001]
        assert set(df["side"]) == {2}
        # 段已清理
        assert rec.wals[sym].segment_paths() == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_alignment_processes_events_after_aligning_event():
    """对齐成功后处理对齐事件及其后全部事件,而非只处理对齐事件就 return。"""
    rec, tmpdir = _make_recorder(needs_align=True)
    try:
        sym = "btcjpy"
        rec.last_update_ids[sym] = 100
        rec.is_initialized[sym] = True
        rec.stream_aligned[sym] = False
        # e99 落后(u=99,会被跳过);e101 对齐(u=101=100+1);e102/e103 后续。
        e99 = {"ts": 1, "u": 99, "side": 0, "price": 10.0, "qty": 1.0}
        e101 = {"ts": 2, "u": 101, "side": 0, "price": 11.0, "qty": 1.0}
        e102 = {"ts": 3, "u": 102, "side": 1, "price": 12.0, "qty": 1.0}
        e103 = {"ts": 4, "u": 103, "side": 0, "price": 13.0, "qty": 1.0}
        rec.pre_align_buffer[sym] = [e99, e101, e102]

        asyncio.run(rec._handle_depth(sym, e103))

        assert rec.stream_aligned[sym] is True
        assert rec.pre_align_buffer[sym] == []
        # e101,e102,e103 三条都应被捕获进 WAL micro-buffer(e99 跳过)。
        assert rec.wals[sym].pending == 3, rec.wals[sym].pending
        assert rec.last_update_ids[sym] == 103
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_circuit_breaker_flushes_before_exit(monkeypatch):
    """熔断 action=exit 时,os._exit 之前先把 micro-buffer flush 成段。"""
    rec, tmpdir = _make_recorder()
    try:
        sym = "btcjpy"
        rec.wals[sym].append([[9000, 2, 1.0, 1.0]])
        assert rec.wals[sym].pending == 1
        assert rec.wals[sym].segment_paths() == []  # 还没成段

        exit_calls = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)  # 阻断后续,模拟进程退出

        monkeypatch.setattr(os, "_exit", fake_exit)
        monkeypatch.delenv("NARCI_ALIGNMENT_BREAKER_ACTION", raising=False)

        with pytest.raises(SystemExit):
            # retry/elapsed 远超阈值 → 触发熔断
            rec._maybe_trip_alignment_breaker(sym, retry_n=10_000, elapsed=10_000)

        assert exit_calls == [2], exit_calls
        # micro-buffer 已被 flush 成段(留给 recover_orphans 恢复)
        segs = rec.wals[sym].segment_paths()
        assert len(segs) == 1, segs
        assert segs[0].endswith(SEGMENT_SUFFIX)
        df = pd.read_parquet(segs[0])
        assert list(df["timestamp"]) == [9000]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
