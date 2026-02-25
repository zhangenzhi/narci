import pandas as pd
from datetime import datetime

class SimulatedBroker:
    def __init__(self, initial_cash=10000.0, maker_fee=0.0002, taker_fee=0.0004):
        """
        高频微观回测撮合引擎 (L2 Matching Engine)
        :param initial_cash: 初始资金
        :param maker_fee: 挂单手续费 (暂保留为后续限价单接口使用)
        :param taker_fee: 吃单/市价单手续费
        """
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}
        
        # 区分 Maker 和 Taker 费率，HFT 回测中这极其重要
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        
        self.current_price = {}  # Fallback L1 price
        self.current_bids = {}   # {symbol: [(price, qty), ...]}
        self.current_asks = {}   # {symbol: [(price, qty), ...]}
        self.current_timestamp = None
        
        self.trade_history = []
        self.equity_history = []

    def update_l1(self, symbol, timestamp, price):
        """更新 L1 价格 (仅作 Fallback 使用)"""
        self.current_timestamp = timestamp
        self.current_price[symbol] = price

    def update_l2(self, symbol, timestamp, bids, asks):
        """
        更新 L2 订单簿状态
        bids: list of tuples (price, qty) 降序排列
        asks: list of tuples (price, qty) 升序排列
        """
        self.current_timestamp = timestamp
        self.current_bids[symbol] = bids
        self.current_asks[symbol] = asks
        
        # 自动维护 fallback 的 mid_price
        if bids and asks:
            self.current_price[symbol] = (bids[0][0] + asks[0][0]) / 2.0

    def _execute_market_sweep(self, symbol, quantity, side):
        """
        核心撮合逻辑：穿透吃单 (Orderbook Sweeping)
        根据订单大小，逐层消耗深度，计算加权平均成交价 (VWAP)
        :param side: 'buy' (消耗 asks) 或 'sell' (消耗 bids)
        :return: (实际成交数量, 成交均价)
        """
        levels = self.current_asks.get(symbol, []) if side == 'buy' else self.current_bids.get(symbol, [])
        
        if not levels:
            return 0.0, 0.0
            
        remaining_qty = quantity
        total_cost = 0.0
        filled_qty = 0.0
        
        for price, lvl_qty in levels:
            if remaining_qty <= 0:
                break
                
            # 计算这一档能吃掉多少
            fill_amount = min(remaining_qty, lvl_qty)
            total_cost += fill_amount * price
            filled_qty += fill_amount
            remaining_qty -= fill_amount
            
        if filled_qty == 0:
            return 0.0, 0.0
            
        vwap = total_cost / filled_qty
        
        # 记录流动性耗尽警告
        if remaining_qty > 1e-8:
            print(f"⚠️ [Broker] {symbol} {side.upper()} 订单簿击穿! 目标: {quantity}, 实际成交: {filled_qty}, 剩余量被取消。")
            
        return filled_qty, vwap

    def buy(self, symbol, quantity, use_l2=True):
        """执行买入 (市价吃单 Taker)"""
        if use_l2 and symbol in self.current_asks:
            filled_qty, avg_price = self._execute_market_sweep(symbol, quantity, 'buy')
            if filled_qty == 0: return False
        else:
            # 降级到 L1 撮合（无滑点）
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None: return False

        cost = avg_price * filled_qty
        fee = cost * self.taker_fee
        total_deduction = cost + fee

        if self.cash >= total_deduction:
            self.cash -= total_deduction
            self.positions[symbol] = self.positions.get(symbol, 0.0) + filled_qty
            self._record_trade(symbol, 'BUY', filled_qty, avg_price, fee)
            return True
        else:
            # 资金不足时，按照剩余资金最大可买数量进行部分成交 (Partial Fill)
            affordable_qty = self.cash / (avg_price * (1 + self.taker_fee))
            if affordable_qty > 0.0001:  # 设置最小下单阈值
                self.buy(symbol, affordable_qty, use_l2)
            else:
                print(f"❌ [Broker] 资金不足以执行买入。余额: {self.cash:.2f}")
            return False

    def sell(self, symbol, quantity, use_l2=True):
        """执行卖出 (市价吃单 Taker)"""
        current_pos = self.positions.get(symbol, 0.0)
        
        if current_pos < quantity:
            print(f"❌ [Broker] 仓位不足以执行卖出。当前: {current_pos}, 尝试卖出: {quantity}")
            return False

        if use_l2 and symbol in self.current_bids:
            filled_qty, avg_price = self._execute_market_sweep(symbol, quantity, 'sell')
            if filled_qty == 0: return False
        else:
            avg_price = self.current_price.get(symbol)
            filled_qty = quantity
            if avg_price is None: return False

        revenue = avg_price * filled_qty
        fee = revenue * self.taker_fee
        net_revenue = revenue - fee

        self.cash += net_revenue
        self.positions[symbol] -= filled_qty
        self._record_trade(symbol, 'SELL', filled_qty, avg_price, fee)
        return True

    def close_all(self, use_l2=True):
        """市价平掉所有仓位"""
        for symbol, qty in list(self.positions.items()):
            if qty > 0:
                self.sell(symbol, qty, use_l2)

    def _record_trade(self, symbol, action, quantity, price, fee):
        """记录交易历史"""
        self.trade_history.append({
            'timestamp': self.current_timestamp,
            'symbol': symbol,
            'action': action,
            'quantity': quantity,
            'price': price,
            'fee': fee,
            'cash_balance': self.cash
        })

    def record_equity(self):
        """记录当前的资金曲线截面"""
        equity = self.cash
        for sym, qty in self.positions.items():
            if qty > 0:
                # 估值时使用当前的 mid_price 较为合理
                price = self.current_price.get(sym, 0)
                equity += qty * price
                
        self.equity_history.append({
            'timestamp': self.current_timestamp,
            'equity': equity
        })

    def get_trade_history(self):
        return pd.DataFrame(self.trade_history)

    def get_equity_history(self):
        return pd.DataFrame(self.equity_history)

    def get_state(self):
        """获取账户当前状态"""
        return {
            'cash': self.cash,
            'positions': self.positions,
            'total_equity': self.equity_history[-1]['equity'] if self.equity_history else self.initial_cash
        }