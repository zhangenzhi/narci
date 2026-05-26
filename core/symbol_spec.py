"""
交易对元数据：tick_size / lot_size / min_notional。

实盘交易所会以这三个值为基础 reject 不合规订单。回测要做到 paper-trading parity
就必须同样校验，否则回测出来的 fill 数据在试盘上会大量 reject。

每个交易所每个 symbol 的精度规则不同，使用前请用真实交易所 exchangeInfo
（Binance）或对应 API 校准。下面 DEFAULT_SPECS 只是粗略默认值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SymbolSpec:
    symbol: str = "UNKNOWN"
    tick_size: float = 0.01     # 最小价位
    lot_size: float = 1e-8      # 最小数量
    min_notional: float = 0.0   # 最小订单金额（0 = 无约束）
    fp_tol: float = 1e-7

    def round_price(self, price: float, side: str) -> float:
        """BUY 向下取整 (避免出价更高)；SELL 向上取整 (避免接受更低)。"""
        if side.upper() == "BUY":
            return math.floor(price / self.tick_size) * self.tick_size
        return math.ceil(price / self.tick_size) * self.tick_size

    def round_qty(self, qty: float) -> float:
        return math.floor(qty / self.lot_size) * self.lot_size

    def validate(self, price: float, qty: float) -> str | None:
        """合规则返回 None；否则返回 reject 原因字符串。"""
        if qty <= 0:
            return f"qty {qty} <= 0"
        if price <= 0:
            return f"price {price} <= 0"
        n_p = price / self.tick_size
        if abs(n_p - round(n_p)) > self.fp_tol:
            return (f"price {price} not aligned to tick_size {self.tick_size} "
                    f"(suggest {round(n_p) * self.tick_size})")
        n_q = qty / self.lot_size
        if abs(n_q - round(n_q)) > self.fp_tol:
            return (f"qty {qty} not aligned to lot_size {self.lot_size} "
                    f"(suggest {round(n_q) * self.lot_size})")
        if self.min_notional > 0 and price * qty < self.min_notional - self.fp_tol:
            return f"notional {price * qty} < min_notional {self.min_notional}"
        return None


# 粗略默认（生产应从交易所 API 实时拉 exchangeInfo 校准）
# 默认表保留 CC + Binance 的形状不变,避免破坏现有调用方;bitbank 走单独的
# BITBANK_SPECS 表,通过 get_spec(symbol, venue="bitbank") 查询。
DEFAULT_SPECS: dict[str, SymbolSpec] = {
    # Coincheck spot
    "BTC_JPY": SymbolSpec("BTC_JPY", tick_size=1.0, lot_size=0.001, min_notional=500.0),
    "ETH_JPY": SymbolSpec("ETH_JPY", tick_size=1.0, lot_size=0.001, min_notional=500.0),
    "XRP_JPY": SymbolSpec("XRP_JPY", tick_size=0.001, lot_size=1.0, min_notional=500.0),
    # Binance Japan / Binance.com JPY pairs
    "BTCJPY":  SymbolSpec("BTCJPY",  tick_size=1.0, lot_size=0.00001, min_notional=500.0),
    "ETHJPY":  SymbolSpec("ETHJPY",  tick_size=0.01, lot_size=0.0001, min_notional=500.0),
    "XRPJPY":  SymbolSpec("XRPJPY",  tick_size=0.001, lot_size=1.0, min_notional=500.0),
    # Binance UM Futures USDT
    "BTCUSDT": SymbolSpec("BTCUSDT", tick_size=0.1, lot_size=0.001, min_notional=5.0),
    "ETHUSDT": SymbolSpec("ETHUSDT", tick_size=0.01, lot_size=0.001, min_notional=5.0),
}


# bitbank /v1/spot/pairs (实测 2026-05-15):price_digits → tick = 10^-digits;
# amount_digits → lot = 10^-digits;min_notional 用 bitbank 通用 200 JPY 下限。
# bitbank BTC/ETH 走 8 位精度,远细于 CC 的 0.001 — 跟 CC 数据混用前要注意 lot 不一致。
BITBANK_SPECS: dict[str, SymbolSpec] = {
    "BTC_JPY":  SymbolSpec("BTC_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=200.0),
    "ETH_JPY":  SymbolSpec("ETH_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=200.0),
    "XRP_JPY":  SymbolSpec("XRP_JPY",  tick_size=0.001,  lot_size=1e-4, min_notional=200.0),
    "SOL_JPY":  SymbolSpec("SOL_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=200.0),
    "DOGE_JPY": SymbolSpec("DOGE_JPY", tick_size=0.001,  lot_size=1e-4, min_notional=200.0),
}

# bitFlyer spot:tick 1 JPY (BTC/ETH/BCH/MONA/LSK),0.001 (XRP),lot 1e-8
BITFLYER_SPOT_SPECS: dict[str, SymbolSpec] = {
    "BTC_JPY":  SymbolSpec("BTC_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=500.0),
    "ETH_JPY":  SymbolSpec("ETH_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=500.0),
    "XRP_JPY":  SymbolSpec("XRP_JPY",  tick_size=0.001,  lot_size=1e-4, min_notional=500.0),
    "BCH_JPY":  SymbolSpec("BCH_JPY",  tick_size=1.0,    lot_size=1e-8, min_notional=500.0),
    "ETH_BTC":  SymbolSpec("ETH_BTC",  tick_size=1e-5,   lot_size=1e-8, min_notional=0.0),
}

# bitFlyer Lightning FX:只有 FX_BTC_JPY,tick 1 JPY,lot 0.01 BTC
BITFLYER_FX_SPECS: dict[str, SymbolSpec] = {
    "FX_BTC_JPY": SymbolSpec("FX_BTC_JPY", tick_size=1.0, lot_size=0.01, min_notional=1.0),
}

# GMO 現物:single-asset symbol,tick / lot 跟 pair 不同
# 来源:https://api.coin.z.com/docs/#tag/Public/operation/getMarkets
GMO_SPOT_SPECS: dict[str, SymbolSpec] = {
    "BTC":  SymbolSpec("BTC",  tick_size=1.0,    lot_size=1e-4, min_notional=0.0),
    "ETH":  SymbolSpec("ETH",  tick_size=1.0,    lot_size=1e-2, min_notional=0.0),
    "BCH":  SymbolSpec("BCH",  tick_size=1.0,    lot_size=1e-2, min_notional=0.0),
    "LTC":  SymbolSpec("LTC",  tick_size=1.0,    lot_size=1e-1, min_notional=0.0),
    "XRP":  SymbolSpec("XRP",  tick_size=0.001,  lot_size=1.0,  min_notional=0.0),
}

# GMO レバレッジ:pair symbol,tick 1 JPY (BTC/ETH/BCH/LTC),0.001 (XRP),lot 0.01
GMO_LEVERAGE_SPECS: dict[str, SymbolSpec] = {
    "BTC_JPY":  SymbolSpec("BTC_JPY",  tick_size=1.0,    lot_size=0.01, min_notional=0.0),
    "ETH_JPY":  SymbolSpec("ETH_JPY",  tick_size=1.0,    lot_size=0.1,  min_notional=0.0),
    "BCH_JPY":  SymbolSpec("BCH_JPY",  tick_size=1.0,    lot_size=0.1,  min_notional=0.0),
    "LTC_JPY":  SymbolSpec("LTC_JPY",  tick_size=1.0,    lot_size=1.0,  min_notional=0.0),
    "XRP_JPY":  SymbolSpec("XRP_JPY",  tick_size=0.001,  lot_size=10.0, min_notional=0.0),
    "SOL_JPY":  SymbolSpec("SOL_JPY",  tick_size=1.0,    lot_size=0.1,  min_notional=0.0),
}


_VENUE_TABLES: dict[str, dict[str, SymbolSpec]] = {
    "bitbank":       BITBANK_SPECS,
    "bitflyer_spot": BITFLYER_SPOT_SPECS,
    "bitflyer_fx":   BITFLYER_FX_SPECS,
    "gmo_spot":      GMO_SPOT_SPECS,
    "gmo_leverage":  GMO_LEVERAGE_SPECS,
    # CC / Binance 仍用 DEFAULT_SPECS,这里不重复登记
}


def get_spec(symbol: str, venue: str | None = None) -> SymbolSpec:
    """查表,未知 symbol 返回宽松默认。

    venue=None 走默认表 (向后兼容);传入 venue 时先查 venue 专属表,
    miss 才回退默认表。这样 bitbank 的 lot 不会污染 CC 的查询。
    """
    sym = symbol.upper()
    if venue:
        table = _VENUE_TABLES.get(venue.lower())
        if table and sym in table:
            return table[sym]
    return DEFAULT_SPECS.get(sym, SymbolSpec(sym))
