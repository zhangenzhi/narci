import sys
import os
import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from backtest.strategy import BaseStrategy
except ImportError:
    class BaseStrategy:
        def __init__(self): self.broker = None
        def on_tick(self, tick): pass
        def on_finish(self): pass

from backtest.backtest import BacktestEngine

class L2ImbalanceStrategy(BaseStrategy):
    """
    高频做市商/微观策略 (Maker 版)：
    动态跟踪盘口，在极端不平衡时挂 Limit 单拦截利润，避免 Taker 巨额滑点与手续费。
    """
    def __init__(self, symbol='BTCUSDT', imbalance_threshold=0.3, trade_qty=0.1):
        super().__init__()
        self.symbol = symbol.upper()
        self.imbalance_threshold = imbalance_threshold
        self.trade_qty = trade_qty

    def on_tick(self, tick):
        imbalance = tick.get('imbalance', 0.0)
        
        # 提取盘口最优价
        b_p_0 = tick.get('b_p_0') # 买一价
        a_p_0 = tick.get('a_p_0') # 卖一价
        
        if pd.isna(b_p_0) or pd.isna(a_p_0):
            return

        current_pos = self.broker.positions.get(self.symbol, 0.0)
        sig_info = {'imbalance': round(imbalance, 4)}
        
        # 获取当前我方活跃挂单
        pending_orders = [o for o in self.broker.active_orders.values() if o['symbol'] == self.symbol]
        
        # --- 1. 做多追踪逻辑 ---
        if imbalance > self.imbalance_threshold:
            if current_pos <= 0:
                # 价格追踪(Price Tracking)：如果之前的挂单没在当前的买一价(Bid 1)上，撤单重挂，保持在队伍最前列
                for order in pending_orders:
                    if order['price'] != b_p_0:
                        self.broker.cancel_all_orders(self.symbol)
                        pending_orders = []
                
                # 若没有挂单，下达买入限价单 (Maker) 挂在买一价
                if not pending_orders:
                    target_qty = self.trade_qty if current_pos == 0 else abs(current_pos)
                    self.broker.place_limit_order(self.symbol, 'BUY', b_p_0, target_qty, sig_info)

        # --- 2. 做空追踪逻辑 ---
        elif imbalance < -self.imbalance_threshold:
            if current_pos >= 0:
                for order in pending_orders:
                    if order['price'] != a_p_0:
                        self.broker.cancel_all_orders(self.symbol)
                        pending_orders = []
                        
                # 挂在卖一价阻击
                if not pending_orders:
                    target_qty = self.trade_qty if current_pos == 0 else abs(current_pos)
                    self.broker.place_limit_order(self.symbol, 'SELL', a_p_0, target_qty, sig_info)

        # --- 3. 信号消退逻辑 ---
        else:
            # 如果盘口不平衡消失了，我们立刻撤掉还没有成交的入场挂单，防止接飞刀
            if current_pos == 0 and pending_orders:
                self.broker.cancel_all_orders(self.symbol)
                
            # 【可选进阶】：如果已有仓位但信号反转了，挂出平仓止盈单，这里暂时保持简单

    def on_finish(self):
        """回测结束：撤掉所有未成交的挂单，并将手头上的仓位市价平仓清算"""
        self.broker.cancel_all_orders(self.symbol)
        self.broker.close_all(use_l2=True)

if __name__ == "__main__":
    pass