import pandas as pd
import numpy as np
import os
import time
import hashlib
import yaml

class FeatureBuilder:
    """
    特征工程构建器 (Feature Builder)
    负责将基础的 L2 截面数据转化为量化模型可用的高级特征库 (Feature Store)。
    """
    def __init__(self, config_path=None, ema_span=10, vol_window=20):
        self.ema_span = ema_span
        self.vol_window = vol_window
        
        # 尝试从 backtest.yaml 动态加载特征配置
        if config_path is None:
            config_path = os.path.join(os.getcwd(), "configs", "backtest.yaml")
            
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                    if cfg and 'backtest' in cfg and 'features' in cfg['backtest']:
                        f_cfg = cfg['backtest']['features']
                        self.ema_span = f_cfg.get('ema_span', self.ema_span)
                        self.vol_window = f_cfg.get('vol_window', self.vol_window)
            except Exception as e:
                print(f"⚠️ 读取配置文件 {config_path} 失败: {e}")
        
        self._ema_imbalance = 0.0
        self._ema_alpha = 2.0 / (self.ema_span + 1)
        self._price_history = [0.0] * self.vol_window
        self._tick_count = 0
        
    def build_offline(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        离线向量化提取，生成用于 Feature Store 的宽表
        """
        if df.empty:
            return df
            
        df = df.copy()
        
        if 'taker_buy_vol' in df.columns and 'taker_sell_vol' in df.columns:
            df['momentum_bullish'] = (df['taker_buy_vol'] > df['taker_sell_vol']) & (df['taker_buy_vol'] > 0)
            df['momentum_bearish'] = (df['taker_sell_vol'] > df['taker_buy_vol']) & (df['taker_sell_vol'] > 0)
            
        if 'imbalance' in df.columns:
            df['ema_imbalance'] = df['imbalance'].ewm(span=self.ema_span, adjust=False).mean()
            df['ema_imbalance'] = df['ema_imbalance'].round(4)
            
        if 'mid_price' in df.columns:
            df['volatility_2s'] = df['mid_price'].diff(self.vol_window).abs().fillna(0.0)
            
        return df
        
    def build_online(self, tick: dict) -> dict:
        """实盘流式提取"""
        raw_imb = tick.get('imbalance', 0.0)
        mid_price = tick.get('mid_price', 0.0)
        buy_vol = tick.get('taker_buy_vol', 0.0)
        sell_vol = tick.get('taker_sell_vol', 0.0)
        
        if self._ema_imbalance == 0.0:
            self._ema_imbalance = raw_imb
        else:
            self._ema_imbalance = self._ema_alpha * raw_imb + (1 - self._ema_alpha) * self._ema_imbalance
            
        self._price_history[self._tick_count % self.vol_window] = mid_price
        old_price = self._price_history[(self._tick_count + 1) % self.vol_window]
        self._tick_count += 1
        vol_2s = abs(mid_price - old_price) if old_price > 0 else 0.0
        
        tick['ema_imbalance'] = round(self._ema_imbalance, 4)
        tick['volatility_2s'] = round(vol_2s, 4)
        tick['momentum_bullish'] = buy_vol > sell_vol and buy_vol > 0
        tick['momentum_bearish'] = sell_vol > buy_vol and sell_vol > 0
        
        return tick

    def reset_online_state(self):
        self._ema_imbalance = 0.0
        self._price_history = [0.0] * self.vol_window
        self._tick_count = 0

    def build_from_raw_files(self, file_paths: list) -> pd.DataFrame:
        """
        端到端流水线：多线程提取 -> L2 重构 -> Alpha 特征衍生
        """
        import time
        import pyarrow.dataset as ds
        from data.l2_reconstruct import L2Reconstructor
        
        dataset = ds.dataset(file_paths, format="parquet")
        table = dataset.to_table(columns=['timestamp', 'side', 'price', 'quantity'])
        df = table.to_pandas()
        print(f"  ├─ 📥 原始数据加载完成 (共 {len(df):,} 行)，正在执行全局内存排序...")
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False]).reset_index(drop=True)
        
        print(f"  ├─ 🧊 正在执行底层 L2 盘口微观结构重构 (100ms 采样步长)...")
        recon = L2Reconstructor(depth_limit=10)
        reconstructed_df = recon.process_dataframe(df, sample_interval_ms=100)
        
        if not reconstructed_df.empty:
            print(f"  ├─ 🧠 正在执行特征库 (Feature Store) 的高级向量化衍生...")
            reconstructed_df = self.build_offline(reconstructed_df)
            print(f"  ├─ ✨ 特征衍生完成，共生成 {len(reconstructed_df):,} 帧有效切片")
        else:
            print("  ├─ ⚠️ 重构后数据为空，跳过特征衍生。")
            
        return reconstructed_df

    def build_and_store_from_raw(self, raw_files: list, base_dir: str, market_type: str, symbol: str) -> str:
        """
        全量落盘管线，包含 Feature Store 路径与哈希映射
        """
        feature_dir = os.path.join(base_dir, market_type, "features")
        os.makedirs(feature_dir, exist_ok=True)
        
        file_names = "".join(sorted([os.path.basename(p) for p in raw_files]))
        hash_str = hashlib.md5(file_names.encode('utf-8')).hexdigest()[:8]
        symbol_prefix = symbol if symbol != "ALL" else "MIXED"
        feature_filename = f"{symbol_prefix}_100ms_merged_features_{hash_str}.parquet"
        feature_file_path = os.path.join(feature_dir, feature_filename)
        
        if os.path.exists(feature_file_path):
            print(f"⚡ 命中已有特征库！无需重复构建。\n📍 路径: {feature_file_path}")
            return feature_file_path
            
        print(f"⏳ 开始并发读取 {len(raw_files)} 个 {symbol} 的 RAW 碎片文件...")
        t_start = time.time()
        
        reconstructed_df = self.build_from_raw_files(raw_files)
            
        if not reconstructed_df.empty:
            reconstructed_df.to_parquet(feature_file_path, engine='pyarrow', compression='snappy')
            elapsed = time.time() - t_start
            print(f"✅ 特征库构建成功！总耗时: {elapsed:.2f} 秒")
            print(f"💾 特征已固化至: {feature_file_path}")
            
        return feature_file_path