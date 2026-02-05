import pandas as pd
import numpy as np
import os
from collections import OrderedDict

class L2Reconstructor:
    def __init__(self, depth_limit=20):
        """
        :param depth_limit: 还原 L2 时保留的深度档位数量
        """
        self.depth_limit = depth_limit
        # 使用字典存储订单簿：Price -> Quantity
        self.bids = {}
        self.asks = {}
        self.is_ready = False  # 标记快照是否已加载完成

    def apply_diff(self, side, price, quantity):
        """应用增量更新或初始化快照"""
        # side 0: Bid Diff, 1: Ask Diff, 3: Snapshot Bid, 4: Snapshot Ask
        target_map = self.bids if side in [0, 3] else self.asks
        
        if quantity == 0:
            if price in target_map:
                del target_map[price]
        else:
            target_map[price] = quantity
            
        # 只要遇到快照或累积了一定量的 Diff，我们就认为可以开始尝试重建
        if side in [3, 4]:
            self.is_ready = True

    def get_snapshot(self):
        """获取当前排好序的 Top N 深度视图"""
        if not self.bids or not self.asks:
            return [], []

        # Bids 降序排列 (买一最高)
        sorted_bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:self.depth_limit]
        # Asks 升序排列 (卖一最低)
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0], reverse=False)[:self.depth_limit]
        
        return sorted_bids, sorted_asks

    def _check_data_integrity(self, df):
        """检查数据完整性并打印诊断信息"""
        stats = df['side'].value_counts().to_dict()
        print(f"--- 数据质量诊断 ---")
        print(f"总记录数: {len(df)}")
        print(f"买盘增量(0): {stats.get(0, 0)} | 卖盘增量(1): {stats.get(1, 0)}")
        print(f"成交记录(2): {stats.get(2, 0)}")
        print(f"初始买盘快照(3): {stats.get(3.0, 0)} | 初始卖盘快照(4): {stats.get(4.0, 0)}")
        
        if stats.get(3, 0) == 0 and stats.get(4, 0) == 0:
            print("⚠️ 警告: 未在文件中发现快照数据(side 3/4)。正在通过增量流动态重建盘口...")
            self.is_ready = True 
        print("-" * 20)

    def reconstruct(self, file_path):
        """
        L3 重建：成交对齐
        """
        print(f"正在读取并排序原始数据: {file_path}")
        df = pd.read_parquet(file_path)
        self._check_data_integrity(df)
        
        # 确保同一毫秒内：快照(3,4) > 增量(0,1) > 成交(2)
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False])
        
        reconstructed_data = []
        
        for _, row in df.iterrows():
            ts = row['timestamp']
            side = int(row['side'])
            price = row['price']
            qty = row['quantity']

            if side in [0, 1, 3, 4]:
                self.apply_diff(side, price, qty)
            
            elif side == 2 and self.is_ready:
                bids_top, asks_top = self.get_snapshot()
                if not bids_top or not asks_top: continue
                
                reconstructed_data.append({
                    'timestamp': ts,
                    'price': price,
                    'quantity': qty,
                    'bid1': bids_top[0][0],
                    'ask1': asks_top[0][0],
                    'spread': round(asks_top[0][0] - bids_top[0][0], 8)
                })
                
        return pd.DataFrame(reconstructed_data)

    def generate_l2_dataset(self, file_path, sample_interval_ms=1000):
        """
        L2 重建：定时快照并计算盘口指标
        """
        df = pd.read_parquet(file_path)
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False])
        
        reconstructed_l2 = []
        next_sample_ts = 0
        self.bids, self.asks = {}, {} 
        self._check_data_integrity(df)

        for _, row in df.iterrows():
            ts = row['timestamp']
            side = int(row['side'])
            
            if side in [0, 1, 3, 4]:
                self.apply_diff(side, row['price'], row['quantity'])

            if self.is_ready and ts >= next_sample_ts:
                bids_top, asks_top = self.get_snapshot()
                if bids_top and asks_top:
                    b1_p, b1_q = bids_top[0]
                    a1_p, a1_q = asks_top[0]
                    
                    # 计算盘口不平衡度 (Imbalance)
                    # (买一量 - 卖一量) / (买一量 + 卖一量)
                    # 结果在 [-1, 1] 之间，正数代表买盘更厚
                    imbalance = (b1_q - a1_q) / (b1_q + a1_q)
                    
                    record = {
                        'timestamp': ts,
                        'mid_price': (b1_p + a1_p) / 2,
                        'imbalance': round(imbalance, 4),
                        'spread': round(a1_p - b1_p, 8)
                    }
                    
                    # 展开深度档位
                    for i in range(self.depth_limit):
                        if i < len(bids_top):
                            record[f'b_p_{i}'] = bids_top[i][0]
                            record[f'b_q_{i}'] = bids_top[i][1]
                        if i < len(asks_top):
                            record[f'a_p_{i}'] = asks_top[i][0]
                            record[f'a_q_{i}'] = asks_top[i][1]
                    
                    reconstructed_l2.append(record)
                    if next_sample_ts == 0:
                        next_sample_ts = ts + sample_interval_ms
                    else:
                        next_sample_ts += sample_interval_ms
        
        return pd.DataFrame(reconstructed_l2)

if __name__ == "__main__":
    # 使用你最新的录制文件
    raw_file = "./data/realtime/l2/ethusdt_RAW_20260129_1807.parquet" 
    
    if os.path.exists(raw_file):
        recon = L2Reconstructor(depth_limit=5)
        
        print("\n>>> 开始执行 L3 重建 (成交与盘口对齐) <<<")
        trade_df = recon.reconstruct(raw_file)
        if not trade_df.empty:
            print(trade_df.head())
        
        print("\n>>> 开始执行 L2 重建 (增加 Imbalance 指标) <<<")
        l2_df = recon.generate_l2_dataset(raw_file, sample_interval_ms=1000)
        if not l2_df.empty:
            # 仅展示核心指标
            cols_to_show = ['timestamp', 'mid_price', 'imbalance', 'spread', 'b_p_0', 'a_p_0']
            print(l2_df[cols_to_show].head())
            
            # 保存为特征工程后的数据集
            # output_path = raw_file.replace("RAW", "L2_FEATURES")
            # l2_df.to_parquet(output_path, index=False)
            # print(f"\n特征数据集已保存至: {output_path}")
    else:
        print(f"找不到文件: {raw_file}")