import pandas as pd
import numpy as np
import os

from backtest.broker import SimulatedBroker

class BacktestEngine:
    """
    回测核心驱动引擎
    """
    def __init__(self, data_path, strategy, initial_cash=10000.0, fee_rate=0.001):
        self.data_path = data_path
        self.strategy = strategy
        self.broker = SimulatedBroker(initial_cash, fee_rate)
        self.strategy.broker = self.broker

    def run(self):
        print(f"🚀 正在加载数据: {self.data_path}")
        df = pd.read_parquet(self.data_path)
        df = df.sort_values('timestamp')
        
        print(f"📈 开始回测... (总数据行数: {len(df)})")
        
        # 核心事件循环
        for _, tick in df.iterrows():
            # 1. 更新账户权益
            self.broker.update_equity(tick['price'])
            
            # 2. 调用策略逻辑
            self.strategy.on_tick(tick)
            
            # 3. 记录每日/每时刻状态 (可选，为性能分析准备)
            # self.broker.history.append({'time': tick['timestamp'], 'equity': self.broker.equity})
            
        self.strategy.on_finish()
        self.print_results()

    def print_results(self):
        """
        输出回测报告
        """
        trades_df = pd.DataFrame(self.broker.trades)
        final_equity = self.broker.equity
        total_return = (final_equity - 10000.0) / 10000.0 * 100
        
        print("\n" + "="*20 + " 回测报告 " + "="*20)
        print(f"最终权益: ${final_equity:.2f}")
        print(f"累计收益: {total_return:.2f}%")
        print(f"总交易次数: {len(trades_df)}")
        if not trades_df.empty:
            buy_trades = trades_df[trades_df['side'] == 'BUY']
            sell_trades = trades_df[trades_df['side'] == 'SELL']
            print(f"买入平均价: ${buy_trades['price'].mean():.2f}")
            print(f"卖出平均价: ${sell_trades['price'].mean():.2f}")
        print("="*50)


if __name__ == "__main__":
    # 使用你下载好的其中一个文件进行回测
    data_file = "./data/parquet/ETHUSDT-2026-01-20.parquet"
    from backtest.strategy import MyTrendStrategy
    
    if os.path.exists(data_file):
        # 初始化策略
        my_strat = MyTrendStrategy(window_size=500)
        
        # 初始化引擎并运行
        engine = BacktestEngine(data_file, my_strat)
        engine.run()
    else:
        print(f"未找到数据文件: {data_file}")