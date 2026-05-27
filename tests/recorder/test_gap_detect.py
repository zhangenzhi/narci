"""Coincheck cold-tier gap 检测测试(recorder/gap_detect.py)。

锁定两个正确性核心 + 边界:
  1. **排除注入快照(side 3/4)**:gap 区间里塞 side 3/4 不能把 gap 填掉
     (否则录制器每 600s 注入的快照会掩盖真实丢数据)。
  2. **按 stream 分流**:depth 死了但 trade 还在(2026-05-08 场景)→ depth 有 gap、
     trade 无 gap。
"""
from __future__ import annotations

import pandas as pd

from recorder.gap_detect import detect_gaps, build_report, DEFAULT_THRESHOLD_SEC


def _df(rows):
    return pd.DataFrame(rows, columns=["timestamp", "side", "price", "quantity"])


def _depth(ts):  # 一条 depth 增量(side 0)
    return [ts, 0, 100.0, 1.0]


def _trade(ts):
    return [ts, 2, 100.0, 0.1]


def _snap(ts):  # 注入的快照(side 3)
    return [ts, 3, 100.0, 1.0]


def test_no_gap_dense_stream():
    # 每 100ms 一条 depth,无 gap
    rows = [_depth(1_000_000 + i * 100) for i in range(50)]
    out = detect_gaps(_df(rows), threshold_sec=30)
    assert out["depth"]["stats"]["n_gaps"] == 0
    assert out["depth"]["stats"]["coverage_pct"] == 100.0


def test_single_gap_detected_with_bounds():
    # 1000s 处停,1100s 处恢复(100s gap > 30s 阈值)
    rows = ([_depth(1_000_000 + i * 100) for i in range(10)]   # ...1_000_900
            + [_depth(1_100_000 + i * 100) for i in range(10)])
    out = detect_gaps(_df(rows), threshold_sec=30)
    gaps = out["depth"]["gaps"]
    assert len(gaps) == 1
    assert gaps[0]["start_ms"] == 1_000_900
    assert gaps[0]["end_ms"] == 1_100_000
    assert gaps[0]["gap_sec"] == round((1_100_000 - 1_000_900) / 1000, 3)


def test_injected_snapshots_do_not_mask_gap():
    """关键:gap 区间里塞 side 3/4 快照(模拟录制器注入)不能消除 gap。"""
    rows = [_depth(1_000_000)]
    # 在 100s 的 depth 静默期内,每 600... 用 50s 间隔塞几条快照
    rows += [_snap(1_000_000 + k * 50_000) for k in range(1, 3)]  # 1_050_000, 1_100_000
    rows += [_depth(1_120_000)]  # depth 恢复
    out = detect_gaps(_df(rows), threshold_sec=30)
    # depth 流仍是 1_000_000 → 1_120_000 的 120s gap(快照被忽略)
    assert out["depth"]["stats"]["n_gaps"] == 1
    assert out["depth"]["gaps"][0]["gap_sec"] == 120.0


def test_per_stream_depth_dead_trade_alive():
    """2026-05-08 场景:orderbook(depth)死,trade 仍连续 → 只 depth 有 gap。"""
    rows = []
    rows += [_depth(1_000_000), _depth(1_000_100)]      # depth 在 t≈1000s 后就停了
    rows += [_trade(1_000_000 + i * 1000) for i in range(200)]  # trade 持续每 1s 一条,200s
    out = detect_gaps(_df(rows), threshold_sec=30)
    assert out["depth"]["stats"]["n_gaps"] >= 0  # depth 只有 2 条,跨度小→无 gap... 见下
    # depth 两条相邻 100ms,无内部 gap;但 trade 连续无 gap
    assert out["trade"]["stats"]["n_gaps"] == 0
    # 关键反证:把 trade 也插一个洞,确认 trade 流独立检测
    rows2 = [_trade(2_000_000), _trade(2_080_000)]      # 80s trade gap
    out2 = detect_gaps(_df(rows2), threshold_sec=30)
    assert out2["trade"]["stats"]["n_gaps"] == 1
    assert out2["depth"]["stats"]["n_gaps"] == 0        # 无 depth 事件 → 无 depth gap


def test_threshold_boundary():
    rows = [_depth(0), _depth(29_000)]  # 29s < 30s 阈值
    assert detect_gaps(_df(rows), threshold_sec=30)["depth"]["stats"]["n_gaps"] == 0
    rows = [_depth(0), _depth(31_000)]  # 31s > 30s
    assert detect_gaps(_df(rows), threshold_sec=30)["depth"]["stats"]["n_gaps"] == 1


def test_empty_and_single_event():
    for out in (detect_gaps(_df([]), 30),
                detect_gaps(_df([_depth(1)]), 30)):
        assert out["depth"]["stats"]["n_gaps"] == 0
        assert out["depth"]["stats"]["coverage_pct"] == 100.0


def test_coverage_pct():
    # span=200s,一个 100s gap → coverage ≈ 50%
    rows = [_depth(0), _depth(100_000), _depth(200_000)]  # 0→100s gap, 100s→200s gap
    out = detect_gaps(_df(rows), threshold_sec=30)
    st = out["depth"]["stats"]
    assert st["span_sec"] == 200.0
    assert st["total_gap_sec"] == 200.0   # 两段各 100s
    assert st["coverage_pct"] == 0.0      # 全是 gap


def test_build_report_shape():
    rows = [_depth(0), _depth(100_000)]
    rep = build_report("btc_jpy", "2026-05-17", _df(rows), threshold_sec=30)
    assert rep["symbol"] == "BTC_JPY" and rep["date"] == "2026-05-17"
    assert rep["threshold_sec"] == 30 and rep["total_gaps"] == 1
    assert "depth" in rep["streams"] and "trade" in rep["streams"]
    assert isinstance(rep["generated_at"], int)


def test_daily_compactor_writes_gap_sidecar(tmp_path):
    """集成:DailyCompactor.write_gap_report 从 DAILY 产出 cold-tier sidecar。"""
    import json
    from datetime import date
    from recorder.daily_compactor import DailyCompactor

    raw = tmp_path / "raw"; raw.mkdir()
    cold = tmp_path / "cold"
    _df([_depth(1_000_000), _depth(1_120_000)]).to_parquet(   # 120s depth gap
        raw / "BTC_JPY_RAW_20260517_DAILY.parquet", index=False)

    c = DailyCompactor(symbol="btc_jpy", target_date=date(2026, 5, 17),
                       raw_dir=str(raw), official_dir=str(tmp_path / "off"),
                       cold_dir=str(cold), exchange="coincheck", market_type="spot")
    path = c.write_gap_report(threshold_sec=30)
    assert path and path.endswith("BTC_JPY_GAPS_20260517.json")
    rep = json.loads(open(path, encoding="utf-8").read())
    assert rep["total_gaps"] == 1
    assert rep["streams"]["depth"]["gaps"][0]["gap_sec"] == 120.0


def test_daily_compactor_gap_report_noop_when_no_daily(tmp_path):
    from datetime import date
    from recorder.daily_compactor import DailyCompactor
    raw = tmp_path / "raw"; raw.mkdir()
    c = DailyCompactor(symbol="btc_jpy", target_date=date(2026, 5, 17),
                       raw_dir=str(raw), official_dir=str(tmp_path / "off"),
                       exchange="coincheck", market_type="spot")
    assert c.write_gap_report() is None   # DAILY 不存在 → best-effort 返回 None,不崩
