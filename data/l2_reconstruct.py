import pandas as pd
import numpy as np

class L2Reconstructor:
    def __init__(self, depth_limit=20):
        """
        :param depth_limit: 还原 L2 时保留的深度档位数量
        """
        self.depth_limit = depth_limit
        self.bids = {}
        self.asks = {}
        self.is_ready = False 
        
        # 用于聚合采样期间内的 L1 逐笔主动买卖成交量
        self.period_taker_buy_vol = 0.0
        self.period_taker_sell_vol = 0.0

    def apply_diff(self, side, price, quantity):
        """保留外部接口以维持兼容性，但在 process_dataframe 中已被 inline 展开以提升性能"""
        target_map = self.bids if side in [0, 3] else self.asks
        if quantity == 0:
            target_map.pop(price, None)
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

        # 【核心优化 1】: 将 DataFrame 转换为 NumPy 数组遍历，跳过 df.itertuples() 的庞大开销，提速 5~10 倍
        columns = ['timestamp', 'side', 'price', 'quantity']
        for col in columns:
            if col not in df.columns:
                return pd.DataFrame()
        
        values = df[columns].to_numpy()
        
        for i in range(len(values)):
            ts = int(values[i, 0])
            side = int(values[i, 1])
            price = values[i, 2]
            qty = values[i, 3]

            # 盘口挂单更新
            if side in [0, 1, 3, 4]:
                # 【核心优化 2】: 展开 apply_diff 逻辑，避免每行产生一次 Python 函数调用栈开销
                target_map = self.bids if side in [0, 3] else self.asks
                if qty == 0:
                    target_map.pop(price, None)
                else:
                    target_map[price] = qty
                    
                if side in [3, 4]:
                    self.is_ready = True
                    # 【致命 Bug 修复】: 快照注入阶段 (side 3/4) 时间戳完全相同，
                    # 绝对不能在这里采样！否则会记录下只注入了“半边”的残缺盘口。跳过此次循环的采样检测。
                    continue
                    
            # L1 逐笔成交量统计
            elif side == 2:
                if qty > 0:
                    self.period_taker_buy_vol += qty
                else:
                    self.period_taker_sell_vol += abs(qty)

            # 采样逻辑
            if sample_interval_ms is not None:
                if self.is_ready and ts >= next_sample_ts:
                    bids_top = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:self.depth_limit]
                    asks_top = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:self.depth_limit]
                    
                    if bids_top and asks_top:
                        b1_p, _ = bids_top[0]
                        a1_p, _ = asks_top[0]
                        
                        # 多档深度加权 (Top-5 Deep Imbalance)
                        b_q_sum = sum(q for p, q in bids_top[:5])
                        a_q_sum = sum(q for p, q in asks_top[:5])
                        imbalance = (b_q_sum - a_q_sum) / (b_q_sum + a_q_sum) if (b_q_sum + a_q_sum) > 0 else 0.0
                            
                        record = {
                            'timestamp': ts,
                            'mid_price': (b1_p + a1_p) / 2.0,
                            'imbalance': round(imbalance, 4),
                            'spread': round(a1_p - b1_p, 8),
                            'taker_buy_vol': round(self.period_taker_buy_vol, 4),
                            'taker_sell_vol': round(self.period_taker_sell_vol, 4)
                        }
                        
                        for idx in range(self.depth_limit):
                            if idx < len(bids_top):
                                record[f'b_p_{idx}'], record[f'b_q_{idx}'] = bids_top[idx]
                            if idx < len(asks_top):
                                record[f'a_p_{idx}'], record[f'a_q_{idx}'] = asks_top[idx]
                        
                        results.append(record)
                        
                        # 清空当前周期的成交统计，准备进入下一个周期
                        self.period_taker_buy_vol = 0.0
                        self.period_taker_sell_vol = 0.0
                        
                        # 【核心优化 3】: 时间漂移修复与全局绝对对齐
                        # 确保无论文件何时开始，Tick 切片永远在全球对齐的 100ms 边界上 (如 ...000, ...100, ...200)
                        # 下游 DatasetBuilder 做 GRU resample 时就不会出现坑洞
                        next_sample_ts = (ts // sample_interval_ms + 1) * sample_interval_ms
            
            # L3 逐笔成交对齐逻辑
            else:
                if side == 2 and self.is_ready:
                    bids_top = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:1]
                    asks_top = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:1]
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
        # 快照注入后确保排序规则不变：时间戳升序，side 降序 (确保 4,3 的快照最先处理)
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False])
        return self.process_dataframe(df, sample_interval_ms)