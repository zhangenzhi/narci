import os
import sys
import glob
import numpy as np

# 1. 自动处理路径问题：确保可以从项目根目录导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
if root_dir not in sys.path:
    sys.path.append(root_dir)
from backtest.strategy import BaseStrategy

class MyTrendStrategy(BaseStrategy):
    """
    一个简单的双均线或价格突破策略示例
    """
    def __init__(self, window_size=100):
        super().__init__()
        self.window_size = window_size
        self.prices = []

    def on_tick(self, tick):
        self.prices.append(tick['price'])
        if len(self.prices) > self.window_size:
            self.prices.pop(0)
            
            avg_price = np.mean(self.prices)
            current_price = tick['price']
            
            # 简单的策略逻辑：当前价格高于均线且无仓位时买入
            if current_price > avg_price * 1.001 and self.broker.position == 0:
                self.broker.buy(current_price, self.broker.cash, tick['timestamp'])
            
            # 当前价格低于均线且有仓位时卖出
            elif current_price < avg_price * 0.999 and self.broker.position > 0:
                self.broker.sell(current_price, self.broker.position, tick['timestamp'])