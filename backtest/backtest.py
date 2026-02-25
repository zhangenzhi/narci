import pandas as pd
import numpy as np
import os
import time
from backtest.broker import SimulatedBroker

class BacktestEngine:
    """
    极速 L2 回测核心驱动引擎 (增强版)
    - 针对 L2 高频数据优化了遍历性能 (放弃 iterrows)
    - 动态解析多档订单簿并送入高频撮合引擎
    """
    def __init__(self, data_paths, strategy, symbol='UNKNOWN', initial_cash=10000.0, maker_fee=0.0002, taker_fee=0.0004):
        """
        :param data_paths: 数据文件路径或列表
        :param strategy: 策略实例
        :param symbol: 回测的主交易对 (必须指定，以便 Broker 识别)
        """
        if isinstance(data_paths, str):
            self.data_paths = [data_paths]
        else:
            self.data_paths = data_paths
            
        self.symbol = symbol.upper()
        self.strategy = strategy
        # 对接全新的 L2 SimulatedBroker
        self.broker = SimulatedBroker(initial_cash=initial_cash, maker_fee=maker_fee, taker_fee=taker_fee)
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
        return full_df.sort_values('timestamp')

    def run(self):
        print(f"🚀 正在准备数据，总计文件数: {len(self.data_paths)}")
        df = self._load_and_merge_data()
        
        # 预先侦测包含的深度档位数量 (b_p_0, b_p_1...)
        b_p_cols = [c for c in df.columns if c.startswith('b_p_')]
        depth_limit = len(b_p_cols)
        
        print(f"📈 开始极速事件驱动回测... (合并后数据行数: {len(df):,}, 解析深度档位: {depth_limit})")
        start_time = time.time()
        
        # 【性能优化核心 1】：放弃 iterrows()。
        # 使用 to_dict('records') 或 itertuples，在 Python 级别循环，性能可提升百倍。
        records = df.to_dict('records')
        
        for i, tick in enumerate(records):
            ts = tick['timestamp']
            
            # 【实盘适配核心】：解析 L2 订单簿，送入新版 Broker
            bids = []
            asks = []
            for d in range(depth_limit):
                bp = tick.get(f'b_p_{d}')
                if bp is None or pd.isna(bp): break
                bq = tick.get(f'b_q_{d}')
                ap = tick.get(f'a_p_{d}')
                aq = tick.get(f'a_q_{d}')
                bids.append((bp, bq))
                asks.append((ap, aq))
                
            self.broker.update_l2(self.symbol, ts, bids, asks)
            
            # 调用策略逻辑
            self.strategy.on_tick(tick)
            
            # 【性能优化核心 2】：降频采样记录资金曲线
            # 没必要每 100ms 记录一次权益，每 1000 次 tick (约100秒) 记录一次足够画出平滑曲线，极大节省内存
            if i % 1000 == 0:
                self.broker.record_equity()
                
        # 循环结束，强制记录最后一次净值并清仓
        self.broker.record_equity()
        self.strategy.on_finish()
        
        elapsed = time.time() - start_time
        print(f"✅ 回测计算完成！耗时: {elapsed:.2f} 秒 (处理速度: {len(df)/elapsed:,.0f} ticks/秒)")
        
        self.print_results()

    def print_results(self):
        trade_history = self.broker.get_trade_history()
        equity_history = self.broker.get_equity_history()
        state = self.broker.get_state()
        
        final_equity = state['total_equity']
        initial_equity = self.broker.initial_cash
        total_return = (final_equity - initial_equity) / initial_equity * 100
        
        print("\n" + "="*20 + " L2 回测报告 " + "="*20)
        print(f"📌 测试标的: {self.symbol}")
        print(f"💰 初始资金: ${initial_equity:,.2f}")
        print(f"🏦 最终权益: ${final_equity:,.2f}")
        print(f"📈 累计收益: {total_return:.2f}%")
        print(f"🤝 总交易次数: {len(trade_history)}")
        
        if not trade_history.empty:
            total_fees = trade_history['fee'].sum()
            win_rate = "暂未统计(需要闭环逻辑)"
            print(f"💸 累计支付手续费: ${total_fees:,.2f}")
            print("\n【最近 5 笔交易记录】")
            print(trade_history[['timestamp', 'action', 'quantity', 'price', 'fee']].tail(5))
            
        print("="*53)