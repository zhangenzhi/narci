"""Exchange adapter 契约测试(功能正确性)。

录制器把每条 WS 消息经 adapter 翻译成 Narci 4 列 `[ts, side, price, qty]`。
side 编码契约(`recorder/exchange/base.py` ABC 强制):
  0=bid 增量 · 1=ask 增量 · 2=aggTrade(负 qty = 卖方主动/seller maker)
  3=bid 全量快照 · 4=ask 全量快照;时间戳毫秒。

一个解析 bug = 静默产出脏数据,下游全错 —— 这些纯函数测试守住该契约。
覆盖主力 venue:Binance(spot/UM,U/u 对齐)+ Coincheck(主盘,list-of-lists 成交)。
"""
from __future__ import annotations

import time

from recorder.exchange.binance import (
    BinanceAdapter, BinanceSpotAdapter, BinanceUmFuturesAdapter,
)
from recorder.exchange.coincheck import CoincheckAdapter


# ============================ Binance ============================ #

def _binance():
    return BinanceAdapter(market_type="spot")


def test_binance_depth_to_side_0_1():
    a = _binance()
    data = {"E": 1700000000000,
            "b": [["100.5", "2.0"], ["100.0", "1.0"]],
            "a": [["101.0", "1.5"]]}
    recs = a.standardize_event("depth", data)
    assert recs == [
        [1700000000000, 0, 100.5, 2.0],
        [1700000000000, 0, 100.0, 1.0],
        [1700000000000, 1, 101.0, 1.5],
    ]


def test_binance_aggtrade_seller_maker_negative_qty():
    a = _binance()
    # m=True: buyer is maker → 主动卖 → qty 取负
    sell = a.standardize_event("trade", {"E": 1700000000000, "p": "100.5", "q": "0.3", "m": True})
    assert sell == [[1700000000000, 2, 100.5, -0.3]]
    # m=False: 主动买 → 正
    buy = a.standardize_event("trade", {"E": 1700000000000, "p": "100.5", "q": "0.3", "m": False})
    assert buy == [[1700000000000, 2, 100.5, 0.3]]


def test_binance_snapshot_to_side_3_4():
    a = _binance()
    recs = a.standardize_event("snapshot",
                               {"bids": [["100", "1"]], "asks": [["101", "2"]]},
                               now_ms=999)
    assert recs == [[999, 3, 100.0, 1.0], [999, 4, 101.0, 2.0]]


def test_binance_parse_message_routes_depth_trade():
    a = _binance()
    assert a.parse_message({"stream": "btcusdt@depth@100ms", "data": {"x": 1}}) == ("btcusdt", "depth", {"x": 1})
    assert a.parse_message({"stream": "btcusdt@aggTrade", "data": {"y": 2}}) == ("btcusdt", "trade", {"y": 2})
    assert a.parse_message({"no_stream": 1}) == (None, None, None)


def test_binance_uu_alignment_window():
    a = _binance()
    # 规则:U <= snapshot_id+1 <= u。snapshot_id=100 → 窗口锚点 101。
    assert a.try_align(100, {"U": 100, "u": 105}) is True   # 100<=101<=105
    assert a.try_align(100, {"U": 101, "u": 101}) is True   # 边界:恰好 101
    assert a.try_align(100, {"U": 102, "u": 110}) is False  # 流已 overshoot(102>101)
    assert a.try_align(100, {"U": 90, "u": 100}) is False   # 流落后(u=100<101)
    assert a.get_update_id({"u": 777}) == 777


def test_binance_needs_alignment_true():
    assert _binance().needs_alignment() is True


def test_binance_symbol_conversion():
    a = _binance()
    assert a.to_native("ETH-USDT") == "ethusdt"
    assert a.to_std("ETHUSDT") == "ETH-USDT"


def test_binance_um_dual_ws_urls():
    # UM futures:depth 与 aggTrade 在不同 WS 端点(2026-04-23 拆分)→ 两个 URL
    a = BinanceUmFuturesAdapter()
    urls = a.ws_urls(["BTCUSDT"], interval_ms=100)
    assert len(urls) == 2
    assert any("@depth@100ms" in u for u in urls)
    assert any("aggTrade" in u for u in urls)


def test_binance_spot_single_combined_ws_url():
    a = BinanceSpotAdapter()
    urls = a.ws_urls(["BTCUSDT"])
    assert len(urls) == 1
    assert "@depth" in urls[0] and "aggTrade" in urls[0]


# ============================ Coincheck ============================ #

def test_coincheck_parse_message_orderbook():
    a = CoincheckAdapter()
    msg = ["btc_jpy", {"bids": [["100", "1"]], "asks": [], "last_update_at": "x"}]
    sym, kind, data = a.parse_message(msg)
    assert sym == "btc_jpy" and kind == "depth"
    assert data["bids"] == [["100", "1"]]


def test_coincheck_parse_message_trades_list_of_lists():
    a = CoincheckAdapter()
    # 一帧可含多笔成交;field[2]=pair
    msg = [["1700000000", "id1", "btc_jpy", "100.5", "0.3", "buy", "f1", "f2", None],
           ["1700000001", "id2", "btc_jpy", "100.6", "0.2", "sell", "f3", "f4", None]]
    sym, kind, data = a.parse_message(msg)
    assert sym == "btc_jpy" and kind == "trade"
    assert data == {"trades": msg}


def test_coincheck_parse_message_garbage():
    a = CoincheckAdapter()
    assert a.parse_message([]) == (None, None, None)
    assert a.parse_message("not-a-list") == (None, None, None)
    assert a.parse_message(["pair_only"]) == (None, None, None)


def test_coincheck_trade_sell_negative_and_ts_seconds_to_ms():
    a = CoincheckAdapter()
    data = {"trades": [
        ["1700000000", "id1", "btc_jpy", "100.5", "0.3", "buy"],
        ["1700000001", "id2", "btc_jpy", "100.6", "0.2", "sell"],
    ]}
    recs = a.standardize_event("trade", data)
    assert recs == [
        [1700000000000, 2, 100.5, 0.3],    # buy → 正
        [1700000001000, 2, 100.6, -0.2],   # sell(卖方主动)→ 负
    ]


def test_coincheck_trade_skips_malformed_rows():
    a = CoincheckAdapter()
    data = {"trades": [
        ["1700000000", "id1", "btc_jpy", "100.5", "0.3", "buy"],
        ["bad"],                                  # 太短 → 跳过
        ["x", "id", "btc_jpy", "notnum", "0.1", "buy"],  # rate 非数 → 跳过
    ]}
    recs = a.standardize_event("trade", data)
    assert recs == [[1700000000000, 2, 100.5, 0.3]]


def test_coincheck_depth_keeps_zero_qty_as_delete():
    a = CoincheckAdapter()
    recs = a.standardize_event("depth",
                               {"bids": [["100", "1"]], "asks": [["101", "0"]]},
                               now_ms=42)
    assert recs == [[42, 0, 100.0, 1.0], [42, 1, 101.0, 0.0]]  # qty=0 = 撤单,保留


def test_coincheck_snapshot_to_side_3_4():
    a = CoincheckAdapter()
    recs = a.standardize_event("snapshot",
                               {"bids": [["100", "1"]], "asks": [["101", "2"]]},
                               now_ms=42)
    assert recs == [[42, 3, 100.0, 1.0], [42, 4, 101.0, 2.0]]


def test_coincheck_no_alignment_and_symbols():
    a = CoincheckAdapter()
    assert a.needs_alignment() is False
    assert a.to_native("ETH-JPY") == "eth_jpy"
    assert a.to_std("eth_jpy") == "ETH-JPY"


# ====================== 跨 adapter:side 编码一致 ====================== #

def test_side_encoding_consistent_across_venues():
    """两 venue 的快照 side 语义必须一致(3=bid / 4=ask)。

    注:depth 增量 payload 各 venue 键名不同(Binance `b`/`a` vs Coincheck
    `bids`/`asks`),不可共用;0/1 增量编码已在各自 venue 的测试中覆盖。
    snapshot 两者都用 `bids`/`asks`,故在此跨 venue 验证 3/4 一致。"""
    for a in (BinanceSpotAdapter(), CoincheckAdapter()):
        s = a.standardize_event("snapshot", {"bids": [["1", "1"]], "asks": [["2", "1"]]}, now_ms=1)
        assert sorted(r[1] for r in s) == [3, 4]
