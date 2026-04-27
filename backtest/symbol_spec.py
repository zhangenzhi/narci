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


def get_spec(symbol: str) -> SymbolSpec:
    """查表，未知 symbol 返回宽松默认（建议生产代码显式传入）"""
    return DEFAULT_SPECS.get(symbol.upper(), SymbolSpec(symbol.upper()))
