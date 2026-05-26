"""Tests for data/sampling.py + L2Reconstructor 采样器接入(P3,2026-05-26)。

锁定:
  1. FixedGridSampler 的全局对齐网格算术 + "仅 emit 后推进"语义。
  2. process_dataframe 经 sampler 重构后**行为保持**:
     - sample_interval_ms=100 与显式 FixedGridSampler(100) 输出一致;
     - emit 的时间戳落在全局 100ms 网格上。
  3. EventSampler 在触发 side 上 emit。
  4. make_sampler 工厂(含 event_at_simulated_maker_fill 的 NotImplementedError 预留)。
"""
from __future__ import annotations

import pandas as pd
import pytest

from data.sampling import (
    Sampler, FixedGridSampler, EventSampler, make_sampler,
)
from data.l2_reconstruct import L2Reconstructor


# ----------------------------- FixedGridSampler ----------------------------- #

def test_fixed_grid_global_alignment_and_advance_only_on_emit():
    s = FixedGridSampler(100)
    # 初始 next_ts=0 → 第一个事件即可 emit
    assert s.should_emit(1037) is True
    s.notify_emitted(1037)
    # 推进到全局对齐边界:(1037//100+1)*100 = 1100
    assert s.should_emit(1050) is False     # 同一格内不再 emit
    assert s.should_emit(1099) is False
    assert s.should_emit(1100) is True      # 跨过边界
    s.notify_emitted(1100)
    assert s.should_emit(1150) is False      # next_ts=1200
    assert s.should_emit(1200) is True


def test_fixed_grid_no_advance_without_emit():
    """未调用 notify_emitted(对应一侧空、未实际 emit)则网格不推进。"""
    s = FixedGridSampler(100)
    assert s.should_emit(1000) is True
    # 不 notify(模拟 book 一侧空、跳过 emit)
    assert s.should_emit(1050) is True       # 仍可 emit,网格未推进
    s.notify_emitted(1050)
    assert s.should_emit(1080) is False


def test_fixed_grid_reset():
    s = FixedGridSampler(100)
    s.notify_emitted(5000)
    assert s.should_emit(1000) is False
    s.reset()
    assert s.should_emit(1000) is True


def test_fixed_grid_mode_tag():
    assert FixedGridSampler(1000).mode_tag == "1s_grid"   # 对齐 SAMPLING_MODES
    assert FixedGridSampler(100).mode_tag == "100ms_grid"
    assert FixedGridSampler(500).mode_tag == "500ms_grid"


def test_fixed_grid_rejects_nonpositive():
    with pytest.raises(ValueError):
        FixedGridSampler(0)
    with pytest.raises(ValueError):
        FixedGridSampler(-100)


# ----------------------------- EventSampler -------------------------------- #

def test_event_sampler_triggers_on_sides():
    s = EventSampler("event_at_cc_trade", frozenset({2}))
    assert s.should_emit(1000, side=2) is True
    assert s.should_emit(1000, side=0) is False
    assert s.should_emit(1000, side=None) is False
    assert s.mode_tag == "event_at_cc_trade"
    # 事件型无网格状态:notify/reset 不改变行为
    s.notify_emitted(1000); s.reset()
    assert s.should_emit(1000, side=2) is True


# ----------------------------- make_sampler -------------------------------- #

def test_make_sampler_factory():
    assert isinstance(make_sampler("1s_grid"), FixedGridSampler)
    assert make_sampler("1s_grid").interval_ms == 1000
    assert make_sampler("250ms_grid").interval_ms == 250
    assert isinstance(make_sampler("event_at_book_update"), EventSampler)
    assert make_sampler("event_at_cc_trade").should_emit(1, side=2) is True
    with pytest.raises(NotImplementedError):
        make_sampler("event_at_simulated_maker_fill")
    with pytest.raises(ValueError):
        make_sampler("nonsense_mode")


# --------------------- process_dataframe 行为保持 -------------------------- #

def _synthetic_df():
    # snapshot(side 3/4)播种 book + is_ready;随后跨多个 100ms 格的更新。
    rows = [
        (1000, 3, 100.0, 1.0),   # bid 快照
        (1000, 4, 101.0, 1.0),   # ask 快照
        (1000, 0,  99.0, 2.0),   # 非快照事件 → 触发 ts=1000 采样
        (1050, 0,  99.5, 2.0),   # 同格,不采
        (1100, 1, 101.5, 2.0),   # 跨格 → 采 1100
        (1150, 0,  99.6, 1.0),   # 同格,不采
        (1200, 1, 101.6, 1.0),   # 跨格 → 采 1200
    ]
    return pd.DataFrame(rows, columns=["timestamp", "side", "price", "quantity"])


def test_process_dataframe_emits_on_global_grid():
    df = _synthetic_df()
    out = L2Reconstructor(depth_limit=10).process_dataframe(df, sample_interval_ms=100)
    assert list(out["timestamp"]) == [1000, 1100, 1200]
    # 全部落在 100ms 全局网格上
    assert all(ts % 100 == 0 for ts in out["timestamp"])


def test_interval_arg_equals_explicit_sampler():
    """sample_interval_ms=100 与显式 FixedGridSampler(100) 必须逐行一致
    (证明抽取后行为未变)。"""
    df = _synthetic_df()
    a = L2Reconstructor(depth_limit=10).process_dataframe(df, sample_interval_ms=100)
    b = L2Reconstructor(depth_limit=10).process_dataframe(df, sampler=FixedGridSampler(100))
    pd.testing.assert_frame_equal(a, b)


def test_no_sampler_falls_back_to_l3_trade_align():
    """两者都 None → 走逐笔成交对齐输出(L3,仅 side==2 成交),列含
    price/bid1/ask1。"""
    rows = [
        (1000, 3, 100.0, 1.0),   # bid 快照 → is_ready
        (1000, 4, 101.0, 1.0),   # ask 快照
        (1010, 2, 100.5, 0.3),   # 成交 → L3 输出一行
        (1020, 2, 100.6, -0.2),  # 卖方主动成交(负量)
    ]
    df = pd.DataFrame(rows, columns=["timestamp", "side", "price", "quantity"])
    out = L2Reconstructor(depth_limit=10).process_dataframe(df)
    assert list(out["timestamp"]) == [1010, 1020]
    assert {"price", "bid1", "ask1"}.issubset(out.columns)
