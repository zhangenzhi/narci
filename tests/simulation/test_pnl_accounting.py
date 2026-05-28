"""B0 护栏 — 锁 PnL 报告字段公式(realized + 库存 MtM 在 last_mid)。

`backtest_alpha_model` 底部 10 行 PnL 公式:

    last_top1 = broker.book.get_top1()
    last_mid  = (best_bid + best_ask) / 2 if last_top1 else 0
    realized_pnl = broker.cash + broker.inventory * last_mid

B1 抽 PnLAccountant 时这段代码会搬家;本测试断言**搬前搬后 report 字段值不变**。
"""
from __future__ import annotations

import pytest

from analytics.simulation import backtest_alpha as ba
from analytics.calibration.priors import CalibrationParams
from core.symbol_spec import SymbolSpec


def _params():
    return CalibrationParams(
        exchange="coincheck", symbol="BTC_JPY",
        cancel_latency_p50_ms=0.0, cancel_latency_p95_ms=0.0,
        cancel_latency_p99_ms=0.0,
    )

def _spec():
    return SymbolSpec(symbol="BTC_JPY", tick_size=1.0,
                      lot_size=1e-8, min_notional=0.0)


class _StubModel:
    def __init__(self, alphas):
        self._a = list(alphas); self._i = 0
    def predict(self, fb):
        if self._i < len(self._a):
            v = self._a[self._i]; self._i += 1
            return v
        return self._a[-1] if self._a else 0.0


def _seed(ts=0, bid=100.0, ask=101.0, qty=10.0):
    return [(ts,   -3, "cc", 3, bid, qty),
            (ts+1, -4, "cc", 4, ask, qty)]


def _run(monkeypatch, events, alphas, **kw):
    monkeypatch.setattr(ba, "load_alpha_model",
                        lambda p, **k: _StubModel(alphas))
    monkeypatch.setattr(ba, "_stream_days",
                        lambda days, mh, *, symbol="BTC_JPY": iter(events))
    base = dict(
        model_path="/x", days=["20260423"],
        symbol="BTC_JPY", exchange="coincheck",
        symbol_spec=_spec(), params=_params(),
        quote_size=0.001, alpha_threshold_bps=1.0,
        quote_lifetime_sec=999.0,        # 不让 TTL 触发,免污染 cancels
        warmup_seconds=0, verbose=False,
        quote_strategy="join_back",
    )
    base.update(kw)
    return ba.backtest_alpha_model(**base)


def test_pnl_zero_when_no_quotes(monkeypatch):
    """alpha 始终低于 threshold → 无下单 → 无成交 → PnL = 0,inv = 0,cash = 0。"""
    events = _seed()
    events += [(1_000, -2, "cc", 2, 100.5, 0.001)]
    r = _run(monkeypatch, events, alphas=[0.0])
    assert r["n_fills"] == 0
    assert r["n_quotes_placed"] == 0
    assert r["realized_pnl_quote"] == 0.0
    assert r["ending_inventory"] == 0.0
    assert r["ending_cash"] == 0.0


def test_realized_pnl_after_round_trip_buy_then_sell(monkeypatch):
    """BUY @ 100 穿透成交 → SELL @ 101 穿透成交。
    cash = -100*size + 101*size = +1*size,inv = 0,realized = cash + 0 = +1*size。"""
    SIZE = 0.001
    events = _seed(ts=0, bid=100.0, ask=101.0)
    # trade@1000: alpha=+5 → place BUY @ 100
    events += [(1_000, -2, "cc", 2, 100.5, 0.0001)]
    # trade@2000(qty>0=买家 maker)价 99 < 100 → Case 2 穿透 → 我方 BUY fill @ 100
    events += [(2_000, -2, "cc", 2, 99.0, 0.01)]
    # trade@3000: alpha=-5 → 撤前一笔(已成交,no-op)+ place SELL @ 101
    events += [(3_000, -2, "cc", 2, 100.5, 0.0001)]
    # trade@4000(qty<0=卖家 maker)价 102 > 101 → Case 2 穿透 → 我方 SELL fill @ 101
    events += [(4_000, -2, "cc", 2, 102.0, -0.01)]
    # 注:trade@2000/4000 也会触发 predict;alphas[1]/[3] 设 0.5 < threshold 跳过
    r = _run(monkeypatch, events, alphas=[+5.0, 0.5, -5.0, 0.5],
             quote_size=SIZE)
    assert r["n_fills"] == 2, r
    assert r["ending_inventory"] == pytest.approx(0.0, abs=1e-12)
    assert r["ending_cash"] == pytest.approx(SIZE, rel=1e-6)        # = 101S - 100S
    assert r["realized_pnl_quote"] == pytest.approx(SIZE, rel=1e-6) # cash + 0*mid


def test_mtm_inventory_priced_at_last_mid(monkeypatch):
    """只买不卖 → 剩仓 → realized = cash + inv * last_mid。

    买 @ 100 后 book 改为 bid=110/ask=111,last_mid=110.5;
    PnL = -100*SIZE + SIZE*110.5 = 10.5*SIZE。"""
    SIZE = 0.001
    events = _seed(ts=0, bid=100.0, ask=101.0)
    events += [(1_000, -2, "cc", 2, 100.5, 0.0001)]   # 触发 BUY 下单
    events += [(2_000, -2, "cc", 2,  99.0, 0.01)]     # 穿透 BUY 成交 @ 100
    # 最后一段:book 走高,锁定 last_mid = 110.5
    events += [(3_000, -3, "cc", 3, 110.0, 10.0),
               (3_001, -4, "cc", 4, 111.0, 10.0)]
    r = _run(monkeypatch, events, alphas=[+5.0, 0.5], quote_size=SIZE)
    assert r["n_fills"] == 1
    assert r["ending_inventory"] == pytest.approx(SIZE, rel=1e-6)
    assert r["ending_cash"] == pytest.approx(-100.0 * SIZE, rel=1e-6)
    assert r["realized_pnl_quote"] == pytest.approx(10.5 * SIZE, rel=1e-6)


def test_edge_per_fill_bps_formula(monkeypatch):
    """edge_per_fill_bps = realized_pnl / total_notional * 1e4。"""
    SIZE = 0.001
    events = _seed(ts=0, bid=100.0, ask=101.0)
    events += [(1_000, -2, "cc", 2, 100.5, 0.0001),
               (2_000, -2, "cc", 2,  99.0, 0.01),
               (3_000, -2, "cc", 2, 100.5, 0.0001),
               (4_000, -2, "cc", 2, 102.0, -0.01)]
    r = _run(monkeypatch, events, alphas=[+5.0, 0.5, -5.0, 0.5],
             quote_size=SIZE)
    # notional = |100*SIZE| + |101*SIZE| = 201*SIZE = 0.201
    # realized = SIZE = 0.001
    # bps = 0.001 / 0.201 * 1e4 ≈ 49.75
    expected = SIZE / (201.0 * SIZE) * 1e4
    assert r["edge_per_fill_bps"] == pytest.approx(expected, rel=1e-4)
