import os
import glob
import pandas as pd

# 导入 Narci 框架核心组件
from backtest.strategy import BaseStrategy
from backtest.backtest import BacktestEngine
from data.l2_reconstruct import L2Reconstructor

class ImbalanceStrategy(BaseStrategy):
    """
    基于 L2 盘口不平衡度 (Orderbook Imbalance) 的高频动量策略
    """
    def __init__(self, entry_threshold=0.4, exit_threshold=-0.2):
        super().__init__()
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold

    def on_tick(self, tick):
        # tick 包含了 L2Reconstructor 生成的所有特征
        current_price = tick['price']  # 这个字段我们在下面预处理时映射自 mid_price
        imbalance = tick.get('imbalance', 0)

        # 1. 进场逻辑：买盘明显厚于卖盘，且当前无仓位
        if imbalance > self.entry_threshold and self.broker.position == 0:
            # 全仓买入 (在模拟器中，cash 足够才会成交)
            self.broker.buy(current_price, self.broker.cash, tick['timestamp'])
            
        # 2. 离场逻辑：卖单墙变厚，抛压出现，且持有仓位
        elif imbalance < self.exit_threshold and self.broker.position > 0:
            # 卖出所有仓位
            self.broker.sell(current_price, self.broker.position, tick['timestamp'])

def find_latest_raw_file():
    """自动寻找最近录制的 L2 原始数据文件"""
    search_paths = [
        "./replay_buffer/realtime/l2/*_RAW_*.parquet"
    ]
    files = []
    for path in search_paths:
        files.extend(glob.glob(path))
    
    if not files:
        return None
    # 返回最新创建的文件
    return max(files, key=os.path.getctime)

def main():
    print("===" * 15)
    print("🚀 Narci L2 微观特征回测启动")
    print("===" * 15)

    # 1. 查找数据源
    raw_file = find_latest_raw_file()
    if not raw_file:
        print("❌ 未找到 L2 RAW 数据文件！")
        print("💡 请先运行: python data/l2_recoder.py 录制至少一分钟的数据。")
        return
    print(f"📄 找到最新原始数据: {raw_file}")

    # 2. 动态重构 L2 特征
    print("\n⏳ 正在进行订单簿重放与特征提取 (L2 Reconstruction)...")
    recon = L2Reconstructor(depth_limit=5)
    
    # 按照 100ms 的频率降采样并提取特征
    df_features = recon.generate_l2_dataset(raw_file, sample_interval_ms=100)
    
    if df_features.empty:
        print("❌ 特征提取失败，数据中可能缺乏初始快照。请录制更长时间。")
        return
        
    print(f"✅ 特征提取成功！生成 {len(df_features)} 个时间切片。")

    # 3. 数据适配 BacktestEngine
    # 引擎核心逻辑需要 'price' 列来计算权益，我们使用盘口中间价 (mid_price) 作为成交价近似值
    df_features['price'] = df_features['mid_price']
    
    # 将处理好的数据写入临时文件供引擎读取
    temp_data_path = "./temp_l2_backtest.parquet"
    df_features.to_parquet(temp_data_path, index=False)

    # 4. 初始化策略与引擎并运行
    print("\n📈 开始执行策略回测...")
    # 实例化我们的不平衡度策略 (进场 0.4，退场 -0.2)
    strategy = ImbalanceStrategy(entry_threshold=0.4, exit_threshold=-0.2)
    
    # 初始资金 $10000，高频策略对手续费极其敏感，这里假设挂单 (Maker) 费率为 0 (或极低，这里填 0.0001)
    engine = BacktestEngine(data_path=temp_data_path, strategy=strategy, initial_cash=10000.0, fee_rate=0.0001)
    
    try:
        engine.run()
    finally:
        # 清理临时特征文件
        if os.path.exists(temp_data_path):
            os.remove(temp_data_path)

if __name__ == "__main__":
    main()