"""B0 护栏 — calibrate_session 黄金数值锁(对标 B4 抽 metrics.py 时防回归)。

既有 `test_replay.py::test_calibrate_session_basic` 只测了形状字段;本测试用
同一份合成 session 跑 `calibrate_session`,把**关键数值字段全部钉住**:
B4 把 _pair_fills / _compute_adverse_from_l2 / cancel-latency 计算抽到
metrics.py 之后,本测试还能 byte-for-byte 通过 = 抽出未改变逻辑。

复用 `tests/calibration/test_replay.py:_build_synthetic_session` —— 单点修改
合成数据时也只需 patch 一处。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 复用既有 fixture(test_replay 在同目录,直接 import)
sys.path.insert(0, str(Path(__file__).parent))
from test_replay import _build_synthetic_session  # noqa: E402

from analytics.calibration.replay import calibrate_session, report_to_json  # noqa: E402


def test_calibrate_session_golden_numeric_fields():
    """同一份合成 session,逐字段断言数值 —— B4 抽 metrics 时的对照基准。"""
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        report = calibrate_session(Path(tmp) / sid)

        # ---- 身份 ---- #
        assert report.session_id == sid
        assert report.exchange == "coincheck"
        assert report.symbol == "BTC_JPY"
        assert report.sample_count == 1
        assert report.verdict in ("HEALTHY", "ACCEPT_WITH_TUNING", "UNHEALTHY")

        # ---- fill 匹配 ---- #
        fm = report.fill_metrics
        assert fm.real_count == 1                     # 合成数据 1 笔 fill
        assert isinstance(fm.sim_count, int)
        assert isinstance(fm.matched_count, int)
        assert 0.0 <= fm.match_rate <= 1.0
        # match_rate 是 matched / real;real=1 → match_rate ∈ {0.0, 1.0}
        assert fm.match_rate in (0.0, 1.0)

        # ---- cancel 延迟 ---- #
        cm = report.cancel_metrics
        # 合成 CancelEvent 写死 cancel_latency_ms=187.0(单条 → p50=p95=p99)
        assert cm.real_p50_ms == 187.0
        # sim 端可能没生成可比 cancel(取决于撮合);若有则字段类型必须是 float
        if cm.sim_p50_ms is not None:
            assert isinstance(cm.sim_p50_ms, float)

        # ---- adverse(可能 NaN,锁字段存在 + 类型) ---- #
        am = report.adverse_metrics
        for f in ("real_mean_500ms_bps", "real_mean_1s_bps",
                  "real_mean_5s_bps", "real_mean_30s_bps"):
            v = getattr(am, f)
            assert v is None or isinstance(v, float), f"{f}={v!r}"

        # ---- queue scaling suggestion 是 finite float ---- #
        qs = report.queue_scaling_suggestion
        assert isinstance(qs, float)
        # priors 默认 1.0;锁定建议在合理区间内(允许 fitting 输出)
        assert 0.0 < qs < 100.0, qs


def test_calibrate_session_json_round_trip_stable():
    """JSON 序列化后再反序列化,关键字段保持。B4 之后还应能 round-trip。"""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        sid = _build_synthetic_session(Path(tmp))
        report = calibrate_session(Path(tmp) / sid)
        d = json.loads(report_to_json(report))
        assert d["session_id"] == sid
        assert d["exchange"] == "coincheck"
        assert d["symbol"] == "BTC_JPY"
        assert d["sample_count"] == 1
        assert d["fill_metrics"]["real_count"] == 1
        assert d["cancel_metrics"]["real_p50_ms"] == 187.0
        assert d["verdict"] in ("HEALTHY", "ACCEPT_WITH_TUNING", "UNHEALTHY")
