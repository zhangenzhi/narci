"""B0 护栏 — 锁 backtest_alpha_model 的策略外层分支。

B1 拆 god function 时,把 strategy / accountant / cold_stream / reporter 抽出去会
重写这块代码;本测试在重构前后保持输出一致 = 重构无回归的最强证据。

覆盖分支:
  - warmup 边界:warmup 内 predict 计数但不下单
  - threshold:|alpha| < threshold → 不下单
  - 报价下达:|alpha| >= threshold → 下单
  - 重报价:alpha 变号 → 撤前一笔(reason=STRATEGY_REPRICE)再下新
  - TTL:超 quote_lifetime_sec → 撤(reason=QUOTE_TTL_EXPIRED)
  - 会话结束:dangling 报价 → SESSION_END 撤
  - 入参守卫:未知 quote_strategy / venue_symbol 立即报错

策略:monkeypatch _stream_days(注入合成事件流)+ load_alpha_model(注入受控
alpha 序列的 StubModel),无需 lustre 冷层。
"""
from __future__ import annotations

import pytest

from analytics.simulation import backtest_alpha as ba
from analytics.calibration.priors import CalibrationParams
from core.symbol_spec import SymbolSpec


# ============================ fixtures ============================ #

def _params():
    """最小 CalibrationParams:撤单延迟设 0,便于断言撤单立即可见。"""
    return CalibrationParams(
        exchange="coincheck", symbol="BTC_JPY",
        cancel_latency_p50_ms=0.0, cancel_latency_p95_ms=0.0,
        cancel_latency_p99_ms=0.0,
    )


def _spec():
    return SymbolSpec(symbol="BTC_JPY", tick_size=1.0,
                      lot_size=1e-8, min_notional=0.0)


class _StubModel:
    """按调用顺序吐 alpha(bps)。用尽后保持最后一个值。"""
    def __init__(self, alphas):
        self._a = list(alphas)
        self._i = 0
        self.calls = 0

    def predict(self, fb):
        self.calls += 1
        if self._i < len(self._a):
            v = self._a[self._i]
            self._i += 1
            return v
        return self._a[-1] if self._a else 0.0


def _seed(ts_ms=0, bid=100.0, ask=101.0, qty=10.0):
    """seed 双边 snapshot(side 3=bid_snap, 4=ask_snap)→ book ready。"""
    return [
        (ts_ms,     -3, "cc", 3, bid, qty),
        (ts_ms + 1, -4, "cc", 4, ask, qty),
    ]


def _cc_trade(ts_ms, price, qty):
    """CC trade:qty>0 = 买家 maker(打 bid),<0 = 卖家 maker(打 ask)。"""
    return (ts_ms, -2, "cc", 2, price, qty)


def _run(monkeypatch, *, events, alphas, **kwargs):
    monkeypatch.setattr(ba, "load_alpha_model",
                        lambda path, **kw: _StubModel(alphas))
    monkeypatch.setattr(ba, "_stream_days",
                        lambda days, max_hours, *, symbol="BTC_JPY": iter(events))
    base = dict(
        model_path="/stubbed", days=["20260423"],
        symbol="BTC_JPY", exchange="coincheck",
        symbol_spec=_spec(), params=_params(),
        quote_size=0.001, alpha_threshold_bps=1.0,
        quote_lifetime_sec=5.0, lookback_seconds=300,
        warmup_seconds=0, verbose=False,
        quote_strategy="join_back",
    )
    base.update(kwargs)
    return ba.backtest_alpha_model(**base)


# ============================ branch locks ============================ #

def test_warmup_skips_quotes_but_counts_predictions(monkeypatch):
    """warmup 期内调了 predict → 计数 ↑,但 continue 跳过下单。"""
    events = _seed(ts_ms=0)
    # warmup_end_ts = 0 + 10*1000 = 10000;两笔 trade 都在 warmup 内
    events += [_cc_trade(2_000, 100.5, 0.0001),
               _cc_trade(5_000, 100.5, 0.0001)]
    r = _run(monkeypatch, events=events, alphas=[5.0, 5.0],
             warmup_seconds=10)
    assert r["n_predictions"] == 2
    assert r["n_predictions_during_warmup"] == 2
    assert r["n_quotes_placed"] == 0


def test_below_threshold_skips_quote(monkeypatch):
    events = _seed()
    events += [_cc_trade(1_000, 100.5, 0.0001)]
    r = _run(monkeypatch, events=events, alphas=[0.5],
             alpha_threshold_bps=1.0)
    assert r["n_predictions"] == 1
    assert r["n_quotes_placed"] == 0


def test_above_threshold_places_quote(monkeypatch):
    events = _seed()
    events += [_cc_trade(1_000, 100.5, 0.0001)]
    r = _run(monkeypatch, events=events, alphas=[5.0],
             alpha_threshold_bps=1.0)
    assert r["n_quotes_placed"] == 1
    # 至少一条 decisions 是 PLACE
    place_kinds = [d.get("event_type", d.get("kind", ""))
                   for d in r["decisions"]]
    assert any("PLACE" in str(k) or "place" in str(k).lower()
               for k in place_kinds), f"无 PLACE: {place_kinds}"


def test_reprice_cancels_live_quote_with_strategy_reprice_reason(monkeypatch):
    """live_oid 不空 + 新一次 |alpha| >= threshold → 撤前一笔(STRATEGY_REPRICE)+ 下新。

    覆盖一切 reprice 情形(同向 / 变号),只要前一笔还活着。变号路径在 CC 现货下
    需要先 fill 才能 SELL(spot-only 库存约束),这里用同向 reprice 简化。

    撤单先进 pending_cancels,**下一个** apply_market_event 才 emit;末尾加 trailing。"""
    events = _seed()
    events += [_cc_trade(1_000, 100.5, 0.0001),    # alpha=+5 → place BUY #1
               _cc_trade(2_000, 100.5, 0.0001),    # alpha=+5 → cancel #1 + place BUY #2
               (3_000, 0, "cc", 0, 100.0, 5.0)]    # trailing 冲洗 pending_cancels
    r = _run(monkeypatch, events=events, alphas=[5.0, 5.0])
    assert r["n_quotes_placed"] == 2
    reasons = [c.get("cancel_reason", "") for c in r["cancels"]]
    assert "STRATEGY_REPRICE" in reasons, reasons


def test_quote_ttl_expiry_cancels_with_ttl_reason(monkeypatch):
    """下单后任何事件 ts >= place_ts + lifetime → TTL 撤(同样需要 trailing event)。"""
    events = _seed()
    events += [_cc_trade(1_000, 100.5, 0.0001),
               # 过 TTL 的事件,触发 broker.cancel(QUOTE_TTL_EXPIRED)
               (10_000, 0, "cc", 0, 100.0, 5.0),
               # trailing:让 _process_pending_cancels 在下一次 apply_market_event 跑
               (10_001, 0, "cc", 0, 100.0, 5.0)]
    r = _run(monkeypatch, events=events, alphas=[5.0],
             quote_lifetime_sec=2.0)
    reasons = [c.get("cancel_reason", "") for c in r["cancels"]]
    assert "QUOTE_TTL_EXPIRED" in reasons, reasons


def test_session_end_cancels_dangling_quote(monkeypatch):
    """会话结束时 dangling 报价 → broker.cancel(SESSION_END)。

    SESSION_END 是循环**之后**调的,没有后续 apply_market_event 处理 pending,
    因此当前实现下不会 emit 到 cancels 流(已知行为)。这里用 spy 锁
    「策略外层确实调了 broker.cancel(reason='SESSION_END')」—— B1 的
    Strategy.on_session_end 必须保留这个调用。
    """
    from analytics.simulation.maker_broker import MakerSimBroker
    calls = []
    original = MakerSimBroker.cancel

    def spy(self, ts, cid, reason="STRATEGY_REPRICE"):
        calls.append({"ts": ts, "cid": cid, "reason": reason})
        return original(self, ts, cid, reason)

    monkeypatch.setattr(MakerSimBroker, "cancel", spy)
    events = _seed()
    events += [_cc_trade(1_000, 100.5, 0.0001)]
    r = _run(monkeypatch, events=events, alphas=[5.0],
             quote_lifetime_sec=999.0)
    assert r["n_quotes_placed"] == 1
    assert any(c["reason"] == "SESSION_END" for c in calls), calls


# ============================ input guards ============================ #

def test_unknown_quote_strategy_raises():
    """white-list 不在表上立即报错(发生在 load_alpha_model 之前)。"""
    with pytest.raises(ValueError, match="quote_strategy"):
        ba.backtest_alpha_model(
            model_path="/x", days=["20260423"],
            symbol_spec=_spec(), params=_params(),
            quote_strategy="not_a_real_strategy",
        )


def test_unknown_venue_symbol_raises():
    """venue_symbol 不在 VENUE_SOURCES_BY_SYMBOL → 立即 ValueError(早于 model 加载)。"""
    with pytest.raises(ValueError, match="venue_symbol"):
        ba.backtest_alpha_model(
            model_path="/x", days=["20260423"],
            symbol_spec=_spec(), params=_params(),
            venue_symbol="UNKNOWN_PAIR",
        )
