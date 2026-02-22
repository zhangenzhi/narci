import pandas as pd
import numpy as np
import os
import glob
from backtest.broker import SimulatedBroker

class BacktestEngine:
    """
    回测核心驱动引擎 (增强版：支持多文件加载)
    """
    def __init__(self, data_paths, strategy, initial_cash=10000.0, fee_rate=0.001):
        """
        :param data_paths: 可以是单个文件路径字符串，也可以是文件路径列表
        """
        if isinstance(data_paths, str):
            self.data_paths = [data_paths]
        else:
            self.data_paths = data_paths
            
        self.strategy = strategy
        self.broker = SimulatedBroker(initial_cash, fee_rate)
        self.strategy.broker = self.broker

    def _load_and_merge_data(self):
        """加载并合并多个 parquet 文件"""
        dfs = []
        for p in sorted(self.data_paths):
            if os.path.exists(p):
                dfs.append(pd.read_parquet(p))
        
        if not dfs:
            raise ValueError("未找到任何有效的数据文件进行回测")
            
        full_df = pd.concat(dfs, ignore_index=True)
        # 确保全局有序
        return full_df.sort_values('timestamp')

    def run(self):
        print(f"🚀 正在准备数据，总计文件数: {len(self.data_paths)}")
        df = self._load_and_merge_data()
        
        print(f"📈 开始回测... (合并后数据行数: {len(df)})")
        
        # 核心事件循环
        for _, tick in df.iterrows():
            # 1. 更新账户权益 (以当前价格折算)
            self.broker.update_equity(tick['price'])
            
            # 2. 调用策略逻辑
            self.strategy.on_tick(tick)
            
        self.strategy.on_finish()
        self.print_results()

    def print_results(self):
        trades_df = pd.DataFrame(self.broker.trades)
        final_equity = self.broker.equity
        total_return = (final_equity - self.broker.history[0]['equity'] if self.broker.history else (final_equity - 10000.0)) / 10000.0 * 100
        
        print("\n" + "="*20 + " 回测报告 " + "="*20)
        print(f"最终权益: ${final_equity:.2f}")
        print(f"累计收益: {total_return:.2f}%")
        print(f"总交易次数: {len(trades_df)}")
        print("="*50)