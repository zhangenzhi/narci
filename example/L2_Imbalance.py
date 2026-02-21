import os
import sys
import glob
import pandas as pd

# 1. 自动处理路径问题：确保可以从项目根目录导入模块
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
if root_dir not in sys.path:
    sys.path.append(root_dir)

from backtest.strategy import BaseStrategy
from backtest.backtest import BacktestEngine
from data.l2_reconstruct import L2Reconstructor

class ImbalanceStrategy(BaseStrategy):
    """
    基于 L2 盘口不平衡度 (Orderbook Imbalance) 的高频动量策略
    """
    def __init__(self, entry_threshold=0.3, exit_threshold=-0.1):
        super().__init__()
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold

    def on_tick(self, tick):
        # 引擎已将 mid_price 映射为 price
        current_price = tick['price']  
        imbalance = tick.get('imbalance', 0)

        # 策略逻辑
        if imbalance > self.entry_threshold and self.broker.position == 0:
            self.broker.buy(current_price, self.broker.cash, tick['timestamp'])
            
        elif imbalance < self.exit_threshold and self.broker.position > 0:
            self.broker.sell(current_price, self.broker.position, tick['timestamp'])

def find_latest_raw_file():
    """搜索原始数据文件"""
    search_patterns = [
        os.path.join(root_dir, "replay_buffer/realtime/l2/*_RAW_*.parquet"),
        os.path.join(root_dir, "data/realtime/l2/*_RAW_*.parquet")
    ]
    files = []
    for pattern in search_patterns:
        files.extend(glob.glob(pattern))
    
    if not files:
        return None
    return max(files, key=os.path.getctime)

def main():
    print("\n" + "="*50)
    print("🚀 Narci L2 微观特征策略回测")
    print("="*50)

    # 1. 查找数据
    raw_file = find_latest_raw_file()
    if not raw_file:
        print("❌ 未找到 L2 RAW 数据文件！")
        print("建议执行: python create_mock_l2.py 生成测试数据")
        return
    print(f"📄 数据源: {os.path.basename(raw_file)}")

    # 2. 特征重构
    print("⏳ 正在重构订单簿指标...")
    recon = L2Reconstructor(depth_limit=5)
    # 按照 100ms 采样
    df_features = recon.generate_l2_dataset(raw_file, sample_interval_ms=100)
    
    if df_features.empty:
        print("❌ 特征提取失败，请检查数据完整性（是否包含 side 3/4 的快照）。")
        return
        
    print(f"✅ 特征提取成功！生成 {len(df_features)} 个样本。")

    # 3. 数据适配
    df_features['price'] = df_features['mid_price']
    temp_path = os.path.join(root_dir, "temp_test_l2.parquet")
    df_features.to_parquet(temp_path, index=False)

    # 4. 运行回测
    strategy = ImbalanceStrategy(entry_threshold=0.3, exit_threshold=-0.1)
    # 使用极低手续费模拟高频场景 (0.01%)
    engine = BacktestEngine(data_path=temp_path, strategy=strategy, initial_cash=10000.0, fee_rate=0.0001)
    
    try:
        engine.run()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

if __name__ == "__main__":
    main()