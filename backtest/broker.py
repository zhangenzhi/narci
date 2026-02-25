import pandas as pd
from datetime import datetime

class SimulatedBroker:
    def __init__(self, initial_cash=10000.0, maker_fee=0.0002, taker_fee=0.0004, leverage=10.0):
        """
        高频微观回测撮合引擎 (U本位双向合约版 + 限价单Maker机制)
        """
        self.initial_cash = initial_cash
        self.cash = initial_cash  
        
        self.positions = {}       
        self.entry_prices = {}    
        self.leverage = leverage  
        
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        
        self.current_price = {}
        self.current_bids = {}
        self.current_asks = {}
        self.current_timestamp = None
        
        # --- 新增：限价单(Maker)管理机制 ---
        self.active_orders = {}   # {order_id: order_dict}
        self.order_counter = 0
        
        self.trade_history = []
        self.equity_history = []

    def update_l1(self, symbol, timestamp, price):
        self.current_timestamp = timestamp
        self.current_price[symbol] = price

    def update_l2(self, symbol, timestamp, bids, asks):
        """更新盘口，并尝试撮合挂着的限价单"""
        self.current_timestamp = timestamp
        self.current_bids[symbol] = bids
        self.current_asks[symbol] = asks
        if bids and asks:
            self.current_price[symbol] = (bids[0][0] + asks[0][0]) / 2.0
            
        # 每次收到新盘口，检查限价单是否被成交
        self._match_limit_orders(symbol, bids, asks)

    def _match_limit_orders(self, symbol, bids, asks):
        """严苛的 Maker 撮合逻辑 (仅当市场卖单砸穿买价，或买单吃穿卖价时成交)"""
        if not bids or not asks: return
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        
        filled_order_ids = []
        for oid, order in list(self.active_orders.items()):
            if order['symbol'] != symbol: continue
            
            # 买单被成交：只有当市场的 Ask(卖单) 砸到了我的挂单价或更低
            if order['side'] == 'BUY' and best_ask <= order['price']:
                self._process_trade(symbol, 'BUY', order['qty'], order['price'], is_maker=True, signal_info=order['signal_info'])
                filled_order_ids.append(oid)
                
            # 卖单被成交：只有当市场的 Bid(买单) 吃到了我的挂单价或更高
            elif order['side'] == 'SELL' and best_bid >= order['price']:
                self._process_trade(symbol, 'SELL', order['qty'], order['price'], is_maker=True, signal_info=order['signal_info'])
                filled_order_ids.append(oid)
                
        for oid in filled_order_ids:
            if oid in self.active_orders:
                del self.active_orders[oid]

    def place_limit_order(self, symbol, side, price, qty, signal_info=None):
        """下达限价挂单 (Maker)"""
        current_pos = self.positions.get(symbol, 0.0)
        
        # 简单可用保证金校验 (仅验证开仓方向)
        if (side == 'BUY' and current_pos >= 0) or (side == 'SELL' and current_pos <= 0):
            required_margin = (qty * price) / self.leverage
            if self.get_available_margin() < required_margin:
                return None # 保证金不足，拒绝挂单
                
        self.order_counter += 1
        oid = self.order_counter
        self.active_orders[oid] = {
            'symbol': symbol,
            'side': side,
            'price': price,
            'qty': qty,
            'signal_info': signal_info
        }
        return oid

    def cancel_all_orders(self, symbol):
        """撤销该交易对的所有限价单"""
        to_delete = [oid for oid, order in self.active_orders.items() if order['symbol'] == symbol]
        for oid in to_delete:
            del self.active_orders[oid]

    def _execute_market_sweep(self, symbol, quantity, side):
        """市价单穿透吃单引擎 (Taker)"""
        levels = self.current_asks.get(symbol, []) if side == 'buy' else self.current_bids.get(symbol, [])
        if not levels: return 0.0, 0.0
            
        remaining_qty = quantity
        total_cost = 0.0
        filled_qty = 0.0
        
        for price, lvl_qty in levels:
            if remaining_qty <= 0: break
            fill_amount = min(remaining_qty, lvl_qty)
            total_cost += fill_amount * price
            filled_qty += fill_amount
            remaining_qty -= fill_amount
            
        if filled_qty == 0: return 0.0, 0.0
        return filled_qty, total_cost / filled_qty

    def get_equity(self):
        unrealized_pnl = 0.0
        for sym, qty in self.positions.items():
            if qty != 0:
                current_p = self.current_price.get(sym, self.entry_prices.get(sym, 0))
                unrealized_pnl += (current_p - self.entry_prices[sym]) * qty
        return self.cash + unrealized_pnl

    def get_available_margin(self):
        equity = self.get_equity()
        used_margin = 0.0
        for sym, qty in self.positions.items():
            if qty != 0:
                current_p = self.current_price.get(sym, self.entry_prices.get(sym, 0))
                used_margin += (abs(qty) * current_p) / self.leverage
        return equity - used_margin

    def _process_trade(self, symbol, action, executed_qty, execute_price, is_maker=False, signal_info=None):
        """处理统一的账本流转，并根据角色(Maker/Taker)收取不同手续费"""
        
        # 1. 动态费率扣除
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        fee = executed_qty * execute_price * fee_rate
        self.cash -= fee
        
        current_pos = self.positions.get(symbol, 0.0)
        current_entry = self.entry_prices.get(symbol, 0.0)
        
        realized_pnl = 0.0
        direction = 1 if action == 'BUY' else -1
        
        # 2. 异向交易 (平仓计算)
        if (current_pos > 0 and direction < 0) or (current_pos < 0 and direction > 0):
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
                self.entry_prices[symbol] = (abs(current_pos) * current_entry + executed_qty * execute_price) / abs(new_pos)
            self.positions[symbol] = new_pos

        self.trade_history.append({
            'timestamp': self.current_timestamp,
            'symbol': symbol,
            'action': action,
            'role': 'MAKER' if is_maker else 'TAKER',
            'quantity': executed_qty,
            'price': execute_price,
            'fee': fee,
            'realized_pnl': realized_pnl,
            'equity': self.get_equity(),
            'signal_info': signal_info if signal_info is not None else {}
        })

    def buy(self, symbol, quantity, use_l2=True, signal_info=None):
        """市价单 (Taker)"""
        if use_l2 and symbol in self.current_asks:
            filled_qty, avg_price = self._execute_market_sweep(symbol, quantity, 'buy')
            if filled_qty == 0: return False
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None: return False

        current_pos = self.positions.get(symbol, 0.0)
        if current_pos >= 0: 
            required_margin = (filled_qty * avg_price) / self.leverage
            avail = max(0.0, self.get_available_margin())
            if avail < required_margin:
                affordable_qty = (avail * self.leverage) / avg_price
                if affordable_qty > 0.001:
                    return self.buy(symbol, affordable_qty, use_l2, signal_info)
                return False

        self._process_trade(symbol, 'BUY', filled_qty, avg_price, is_maker=False, signal_info=signal_info)
        return True

    def sell(self, symbol, quantity, use_l2=True, signal_info=None):
        """市价单 (Taker)"""
        if use_l2 and symbol in self.current_bids:
            filled_qty, avg_price = self._execute_market_sweep(symbol, quantity, 'sell')
            if filled_qty == 0: return False
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None: return False

        current_pos = self.positions.get(symbol, 0.0)
        if current_pos <= 0:
            required_margin = (filled_qty * avg_price) / self.leverage
            avail = max(0.0, self.get_available_margin())
            if avail < required_margin:
                affordable_qty = (avail * self.leverage) / avg_price
                if affordable_qty > 0.001:
                    return self.sell(symbol, affordable_qty, use_l2, signal_info)
                return False

        self._process_trade(symbol, 'SELL', filled_qty, avg_price, is_maker=False, signal_info=signal_info)
        return True

    def close_all(self, use_l2=True, signal_info=None):
        """由于回合结束，强制市价全平撤除敞口"""
        self.cancel_all_orders(symbol=list(self.positions.keys())[0] if self.positions else None)
        for symbol, qty in list(self.positions.items()):
            if qty > 0:
                self.sell(symbol, qty, use_l2, signal_info)
            elif qty < 0:
                self.buy(symbol, abs(qty), use_l2, signal_info)

    def record_equity(self):
        self.equity_history.append({
            'timestamp': self.current_timestamp,
            'equity': self.get_equity()
        })

    def get_trade_history(self):
        return pd.DataFrame(self.trade_history)

    def get_equity_history(self):
        return pd.DataFrame(self.equity_history)

    def get_state(self):
        return {
            'cash': self.cash,
            'positions': self.positions,
            'total_equity': self.get_equity(),
            'available_margin': self.get_available_margin()
        }