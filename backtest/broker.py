"""
回测撮合引擎。

类层次：
  BaseBroker (公共行情/挂单/账本接口)
   ├── SpotBroker       现货（Coincheck / Binance 现货）
   └── SimulatedBroker  U 本位双向合约（Binance UM Futures）

BaseBroker 本身不能直接用；子类实现 `_execute()` 决定一次成交如何影响 cash/position/realized_pnl。
"""

import pandas as pd


# ======================================================================
# BaseBroker: 纯通用部分（行情快照、限价单管理、市价吃单、账本查询）
# ======================================================================

class BaseBroker:
    def __init__(self, initial_cash: float = 1_000_000.0,
                 maker_fee: float = 0.0, taker_fee: float = 0.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash

        self.positions: dict[str, float] = {}

        self.maker_fee = maker_fee
        self.taker_fee = taker_fee

        self.current_price: dict[str, float] = {}
        self.current_bids: dict[str, list] = {}
        self.current_asks: dict[str, list] = {}
        self.current_timestamp = None

        self.active_orders: dict[int, dict] = {}
        self.order_counter = 0

        self.trade_history: list[dict] = []
        self.equity_history: list[dict] = []

        # 事件驱动引擎接管时由 engine 注入；非空时 place_limit_order 会同步注册到此 orderbook，
        # 而 update_l2 不再触发本地的 _match_limit_orders（fills 由 engine 通过 apply_fill 推回来）。
        self.orderbook = None

    # --- 行情更新 ---
    def update_l1(self, symbol, timestamp, price):
        self.current_timestamp = timestamp
        self.current_price[symbol] = price

    def update_l2(self, symbol, timestamp, bids, asks):
        self.current_timestamp = timestamp
        self.current_bids[symbol] = bids
        self.current_asks[symbol] = asks
        if bids and asks:
            self.current_price[symbol] = (bids[0][0] + asks[0][0]) / 2.0
        # event-driven 模式下 orderbook 接管限价单撮合，不再走 best-price-cross 简化模型
        if self.orderbook is None:
            self._match_limit_orders(symbol, bids, asks)

    # --- 限价单 (Maker) ---
    def _match_limit_orders(self, symbol, bids, asks):
        if not bids or not asks:
            return
        best_bid, best_ask = bids[0][0], asks[0][0]
        filled = []
        for oid, order in list(self.active_orders.items()):
            if order["symbol"] != symbol:
                continue
            if order["side"] == "BUY" and best_ask <= order["price"]:
                self._execute(symbol, "BUY", order["qty"], order["price"],
                              is_maker=True, signal_info=order["signal_info"])
                filled.append(oid)
            elif order["side"] == "SELL" and best_bid >= order["price"]:
                self._execute(symbol, "SELL", order["qty"], order["price"],
                              is_maker=True, signal_info=order["signal_info"])
                filled.append(oid)
        for oid in filled:
            self.active_orders.pop(oid, None)

    def cancel_all_orders(self, symbol=None):
        if symbol is None:
            self.active_orders.clear()
            return
        for oid in [o for o, v in self.active_orders.items() if v["symbol"] == symbol]:
            del self.active_orders[oid]

    # --- 市价吃单 ---
    def _sweep(self, symbol, quantity, side):
        """side: 'buy' -> sweep asks; 'sell' -> sweep bids"""
        levels = self.current_asks.get(symbol, []) if side == "buy" \
            else self.current_bids.get(symbol, [])
        if not levels:
            return 0.0, 0.0
        remaining, cost, filled = quantity, 0.0, 0.0
        for price, lvl_qty in levels:
            if remaining <= 0:
                break
            fill = min(remaining, lvl_qty)
            cost += fill * price
            filled += fill
            remaining -= fill
        if filled == 0:
            return 0.0, 0.0
        return filled, cost / filled

    # --- 查询 ---
    def record_equity(self):
        self.equity_history.append({
            "timestamp": self.current_timestamp,
            "equity": self.get_equity(),
        })

    def get_trade_history(self):
        return pd.DataFrame(self.trade_history)

    def get_equity_history(self):
        return pd.DataFrame(self.equity_history)

    # --- 子类必须实现 ---
    def _execute(self, symbol, action, qty, price, is_maker, signal_info):
        raise NotImplementedError

    def get_equity(self):
        raise NotImplementedError


# ======================================================================
# SpotBroker: 现货（无杠杆、无做空）
# ======================================================================

class SpotBroker(BaseBroker):
    """持仓单位 = 基础币数量（≥0）；cash = 计价币。适用 Coincheck / Binance Spot。"""

    def __init__(self, initial_cash: float = 1_000_000.0,
                 maker_fee: float = 0.0, taker_fee: float = 0.0):
        super().__init__(initial_cash, maker_fee, taker_fee)
        self.avg_costs: dict[str, float] = {}

    # --- 限价单 ---
    def place_limit_order(self, symbol, side, price, qty, signal_info=None):
        side = side.upper()
        if side == "BUY":
            if self.cash < price * qty:
                return None
        elif side == "SELL":
            if self.positions.get(symbol, 0.0) < qty:
                return None
        else:
            return None
        self.order_counter += 1
        oid = self.order_counter
        self.active_orders[oid] = {
            "symbol": symbol, "side": side, "price": price,
            "qty": qty, "remaining": qty, "signal_info": signal_info,
        }
        # 同步到事件驱动 orderbook（如已注入）。orderbook 拒绝（aggressive cross / 满单）则同步回滚。
        if self.orderbook is not None:
            from .orderbook import ORDER_BUY, ORDER_SELL
            ob_side = ORDER_BUY if side == "BUY" else ORDER_SELL
            if not self.orderbook.place_limit_order(oid, ob_side, price, qty):
                self.active_orders.pop(oid, None)
                return None
        return oid

    def apply_fill(self, oid: int, fill_qty: float, fill_price: float, is_maker: bool = True):
        """
        由 EventBacktestEngine 在 orderbook 报告成交时调用。
        支持部分成交：active_orders[oid] 的 remaining 减到 0 才弹出。
        """
        order = self.active_orders.get(oid)
        if order is None:
            return
        order["remaining"] -= fill_qty
        self._execute(order["symbol"], order["side"], fill_qty, fill_price,
                      is_maker=is_maker, signal_info=order["signal_info"])
        if order["remaining"] <= 1e-10:
            self.active_orders.pop(oid, None)

    # --- 市价单 ---
    def buy(self, symbol, quantity, use_l2=True, signal_info=None):
        if use_l2 and symbol in self.current_asks:
            filled_qty, avg_price = self._sweep(symbol, quantity, "buy")
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None:
                return False
        if filled_qty == 0:
            return False
        cost = filled_qty * avg_price * (1 + self.taker_fee)
        if self.cash < cost:
            affordable = (self.cash / (avg_price * (1 + self.taker_fee))) * 0.999
            if affordable < 1e-8:
                return False
            filled_qty = affordable
        self._execute(symbol, "BUY", filled_qty, avg_price,
                      is_maker=False, signal_info=signal_info)
        return True

    def sell(self, symbol, quantity, use_l2=True, signal_info=None):
        held = self.positions.get(symbol, 0.0)
        if held <= 0:
            return False
        quantity = min(quantity, held)
        if use_l2 and symbol in self.current_bids:
            filled_qty, avg_price = self._sweep(symbol, quantity, "sell")
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None:
                return False
        if filled_qty == 0:
            return False
        self._execute(symbol, "SELL", filled_qty, avg_price,
                      is_maker=False, signal_info=signal_info)
        return True

    def close_all(self, use_l2=True, signal_info=None):
        self.cancel_all_orders()
        for symbol, qty in list(self.positions.items()):
            if qty > 0:
                self.sell(symbol, qty, use_l2, signal_info)

    # --- 账本 ---
    def _execute(self, symbol, action, qty, price, is_maker, signal_info):
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        fee = qty * price * fee_rate
        pos = self.positions.get(symbol, 0.0)
        avg = self.avg_costs.get(symbol, 0.0)
        realized_pnl = 0.0

        if action == "BUY":
            self.cash -= qty * price + fee
            new_pos = pos + qty
            self.avg_costs[symbol] = (pos * avg + qty * price) / new_pos if new_pos > 0 else 0.0
            self.positions[symbol] = new_pos
        else:  # SELL
            self.cash += qty * price - fee
            realized_pnl = (price - avg) * qty
            new_pos = pos - qty
            if new_pos < 1e-10:
                new_pos = 0.0
                self.avg_costs[symbol] = 0.0
            self.positions[symbol] = new_pos

        self.trade_history.append({
            "timestamp": self.current_timestamp,
            "symbol": symbol, "action": action,
            "role": "MAKER" if is_maker else "TAKER",
            "quantity": qty, "price": price, "fee": fee,
            "realized_pnl": realized_pnl,
            "equity": self.get_equity(),
            "signal_info": signal_info if signal_info is not None else {},
        })

    def get_equity(self):
        eq = self.cash
        for sym, qty in self.positions.items():
            if qty > 0:
                eq += qty * self.current_price.get(sym, self.avg_costs.get(sym, 0.0))
        return eq

    def get_available_cash(self):
        return self.cash

    def get_state(self):
        return {
            "cash": self.cash,
            "positions": self.positions,
            "total_equity": self.get_equity(),
            "avg_costs": self.avg_costs,
        }


# ======================================================================
# SimulatedBroker: U 本位双向合约（带杠杆、可做空）
# ======================================================================

class SimulatedBroker(BaseBroker):
    def __init__(self, initial_cash: float = 10000.0,
                 maker_fee: float = 0.0002, taker_fee: float = 0.0004,
                 leverage: float = 10.0):
        super().__init__(initial_cash, maker_fee, taker_fee)
        self.entry_prices: dict[str, float] = {}
        self.leverage = leverage

    # --- 限价单 ---
    def place_limit_order(self, symbol, side, price, qty, signal_info=None):
        current_pos = self.positions.get(symbol, 0.0)
        if (side == "BUY" and current_pos >= 0) or (side == "SELL" and current_pos <= 0):
            required_margin = (qty * price) / self.leverage
            if self.get_available_margin() < required_margin:
                return None
        self.order_counter += 1
        oid = self.order_counter
        self.active_orders[oid] = {
            "symbol": symbol, "side": side, "price": price,
            "qty": qty, "signal_info": signal_info,
        }
        return oid

    # --- 市价单 ---
    def buy(self, symbol, quantity, use_l2=True, signal_info=None):
        if use_l2 and symbol in self.current_asks:
            filled_qty, avg_price = self._sweep(symbol, quantity, "buy")
            if filled_qty == 0:
                return False
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None:
                return False

        current_pos = self.positions.get(symbol, 0.0)
        if current_pos >= 0:
            required_margin = (filled_qty * avg_price) / self.leverage
            avail = max(0.0, self.get_available_margin())
            if avail < required_margin:
                affordable = (avail * self.leverage) / avg_price
                if affordable > 0.001:
                    return self.buy(symbol, affordable, use_l2, signal_info)
                return False

        self._execute(symbol, "BUY", filled_qty, avg_price,
                      is_maker=False, signal_info=signal_info)
        return True

    def sell(self, symbol, quantity, use_l2=True, signal_info=None):
        if use_l2 and symbol in self.current_bids:
            filled_qty, avg_price = self._sweep(symbol, quantity, "sell")
            if filled_qty == 0:
                return False
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None:
                return False

        current_pos = self.positions.get(symbol, 0.0)
        if current_pos <= 0:
            required_margin = (filled_qty * avg_price) / self.leverage
            avail = max(0.0, self.get_available_margin())
            if avail < required_margin:
                affordable = (avail * self.leverage) / avg_price
                if affordable > 0.001:
                    return self.sell(symbol, affordable, use_l2, signal_info)
                return False

        self._execute(symbol, "SELL", filled_qty, avg_price,
                      is_maker=False, signal_info=signal_info)
        return True

    def close_all(self, use_l2=True, signal_info=None):
        if self.positions:
            self.cancel_all_orders(symbol=list(self.positions.keys())[0])
        for symbol, qty in list(self.positions.items()):
            if qty > 0:
                self.sell(symbol, qty, use_l2, signal_info)
            elif qty < 0:
                self.buy(symbol, abs(qty), use_l2, signal_info)

    # --- 账本（双向持仓、异向平仓释放 PnL） ---
    def _execute(self, symbol, action, executed_qty, execute_price,
                 is_maker, signal_info):
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        fee = executed_qty * execute_price * fee_rate
        self.cash -= fee

        current_pos = self.positions.get(symbol, 0.0)
        current_entry = self.entry_prices.get(symbol, 0.0)
        realized_pnl = 0.0
        direction = 1 if action == "BUY" else -1

        if (current_pos > 0 and direction < 0) or (current_pos < 0 and direction > 0):
            # 异向：平仓
            close_qty = min(abs(current_pos), executed_qty)
            pos_dir = 1 if current_pos > 0 else -1
            realized_pnl = (execute_price - current_entry) * close_qty * pos_dir
            self.cash += realized_pnl
            remaining_qty = executed_qty - close_qty
            if remaining_qty > 1e-8:
                self.positions[symbol] = remaining_qty * direction
                self.entry_prices[symbol] = execute_price
            else:
                new_pos = current_pos + (executed_qty * direction)
                if abs(new_pos) < 1e-8:
                    self.positions[symbol] = 0.0
                    self.entry_prices[symbol] = 0.0
                else:
                    self.positions[symbol] = new_pos
        else:
            # 同向加仓
            new_pos = current_pos + (executed_qty * direction)
            if abs(new_pos) > 1e-8:
                self.entry_prices[symbol] = (abs(current_pos) * current_entry
                                             + executed_qty * execute_price) / abs(new_pos)
            self.positions[symbol] = new_pos

        self.trade_history.append({
            "timestamp": self.current_timestamp,
            "symbol": symbol, "action": action,
            "role": "MAKER" if is_maker else "TAKER",
            "quantity": executed_qty, "price": execute_price,
            "fee": fee, "realized_pnl": realized_pnl,
            "equity": self.get_equity(),
            "signal_info": signal_info if signal_info is not None else {},
        })

    def get_equity(self):
        unrealized = 0.0
        for sym, qty in self.positions.items():
            if qty != 0:
                p = self.current_price.get(sym, self.entry_prices.get(sym, 0))
                unrealized += (p - self.entry_prices[sym]) * qty
        return self.cash + unrealized

    def get_available_margin(self):
        equity = self.get_equity()
        used = 0.0
        for sym, qty in self.positions.items():
            if qty != 0:
                p = self.current_price.get(sym, self.entry_prices.get(sym, 0))
                used += (abs(qty) * p) / self.leverage
        return equity - used

    def get_state(self):
        return {
            "cash": self.cash,
            "positions": self.positions,
            "total_equity": self.get_equity(),
            "available_margin": self.get_available_margin(),
        }
