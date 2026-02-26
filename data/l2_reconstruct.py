import pandas as pd
import numpy as np
import os

class L2Reconstructor:
    def __init__(self, depth_limit=20):
        """
        :param depth_limit: 还原 L2 时保留的深度档位数量
        """
        self.depth_limit = depth_limit
        self.bids = {}
        self.asks = {}
        self.is_ready = False 
        
        # 新增：用于聚合采样期间内的 L1 逐笔主动买卖成交量
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

    def apply_diff(self, side, price, quantity):
        """应用增量更新或初始化快照"""
        target_map = self.bids if side in [0, 3] else self.asks
        
        if quantity == 0:
            if price in target_map:
                del target_map[price]
        else:
            target_map[price] = quantity
            
        if side in [3, 4]:
            self.is_ready = True

    def get_snapshot(self):
        """获取当前排好序的 Top N 深度视图"""
        if not self.bids or not self.asks:
            return [], []

        sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:self.depth_limit]
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:self.depth_limit]
        
        return sorted_bids, sorted_asks

    def process_dataframe(self, df, sample_interval_ms=None):
        """
        核心性能优化方法：直接处理传入的 DataFrame，避免反复读盘。
        """
        results = []
        next_sample_ts = 0
        
        # 重置状态
        self.bids, self.asks = {}, {}
        self.is_ready = False
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

        for row in df.itertuples():
            ts = row.timestamp
            side = int(row.side)
            price = row.price
            qty = row.quantity

            # 盘口挂单更新
            if side in [0, 1, 3, 4]:
                self.apply_diff(side, price, qty)
                
            # --- 【核心优化 3 数据源】: L1 逐笔成交量统计 ---
            elif side == 2:
                # 在 recorder 中，Taker Sell 数量被标记为负数
                if qty > 0:
                    self.period_taker_buy_vol += qty
                else:
                    self.period_taker_sell_vol += abs(qty)

            # 采样逻辑
            if sample_interval_ms is not None:
                if self.is_ready and ts >= next_sample_ts:
                    bids_top, asks_top = self.get_snapshot()
                    if bids_top and asks_top:
                        b1_p, b1_q = bids_top[0]
                        a1_p, a1_q = asks_top[0]
                        
                        # --- 【核心优化 1】: 多档深度加权 (Top-5 Deep Imbalance) ---
                        b_q_sum = sum(q for p, q in bids_top[:5])
                        a_q_sum = sum(q for p, q in asks_top[:5])
                        if (b_q_sum + a_q_sum) > 0:
                            imbalance = (b_q_sum - a_q_sum) / (b_q_sum + a_q_sum)
                        else:
                            imbalance = 0.0
                            
                        record = {
                            'timestamp': ts,
                            'mid_price': (b1_p + a1_p) / 2,
                            'imbalance': round(imbalance, 4),
                            'spread': round(a1_p - b1_p, 8),
                            'taker_buy_vol': round(self.period_taker_buy_vol, 4),   # 注入切片期内主动买
                            'taker_sell_vol': round(self.period_taker_sell_vol, 4)  # 注入切片期内主动卖
                        }
                        
                        for i in range(self.depth_limit):
                            if i < len(bids_top):
                                record[f'b_p_{i}'], record[f'b_q_{i}'] = bids_top[i]
                            if i < len(asks_top):
                                record[f'a_p_{i}'], record[f'a_q_{i}'] = asks_top[i]
                        
                        results.append(record)
                        
                        # 清空当前周期的成交统计，准备进入下一个周期
                        self.period_taker_buy_vol = 0.0
                        self.period_taker_sell_vol = 0.0
                        next_sample_ts = ts + sample_interval_ms if next_sample_ts == 0 else next_sample_ts + sample_interval_ms
            
            # L3 逐笔成交对齐逻辑
            else:
                if side == 2 and self.is_ready:
                    bids_top, asks_top = self.get_snapshot()
                    if bids_top and asks_top:
                        results.append({
                            'timestamp': ts,
                            'price': price,
                            'quantity': qty,
                            'bid1': bids_top[0][0],
                            'ask1': asks_top[0][0],
                            'spread': round(asks_top[0][0] - bids_top[0][0], 8)
                        })
                        
        return pd.DataFrame(results)

    def generate_l2_dataset(self, file_path, sample_interval_ms=1000):
        df = pd.read_parquet(file_path)
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False])
        return self.process_dataframe(df, sample_interval_ms)