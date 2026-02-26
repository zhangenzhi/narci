import pandas as pd
import numpy as np
import os
import time
import yaml
import hashlib
import pyarrow.dataset as ds 
import streamlit as st

from backtest.broker import SimulatedBroker
from data.l2_reconstruct import L2Reconstructor
from data.feature_builder import FeatureBuilder

class BacktestEngine:
    """
    极速 L2 回测核心驱动引擎 (带性能探针版)
    """
    def __init__(self, data_paths, strategy, symbol='UNKNOWN', config_path='configs/broker.yaml'):
        if isinstance(data_paths, str):
            self.data_paths = [data_paths]
        else:
            self.data_paths = data_paths
            
        self.symbol = symbol.upper()
        self.strategy = strategy
        
        config = self._load_config(config_path)
        initial_cash = config.get('initial_cash', 10000.0)
        maker_fee = config.get('maker_fee', 0.0002)
        taker_fee = config.get('taker_fee', 0.0004)
        
        self.broker = SimulatedBroker(initial_cash=initial_cash, maker_fee=maker_fee, taker_fee=taker_fee)
        self.strategy.broker = self.broker

        # 新增：性能追踪字典
        self.time_stats = {
            'load_data': 0.0,
            'to_dict': 0.0,
            'broker_update': 0.0,
            'strategy': 0.0,
            'equity': 0.0
        }

    def _load_config(self, path):
        if not os.path.exists(path): return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f).get('broker', {})
        except Exception:
            return {}

    def _load_and_merge_data(self):
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
        start_total = time.time()
        
        # 1. 计时：加载与重构
        t0 = time.time()
        df = self._load_and_merge_data()
        self.time_stats['load_data'] = time.time() - t0
        
        b_p_cols = [c for c in df.columns if c.startswith('b_p_')]
        depth_limit = len(b_p_cols)
        
        print(f"📈 开始极速事件驱动回测... (合并后数据行数: {len(df):,}, 解析深度档位: {depth_limit})")
        
        # 2. 计时：转换内存字典结构
        t0 = time.time()
        records = df.to_dict('records')
        self.time_stats['to_dict'] = time.time() - t0
        
        # 3. 核心循环计时
        for i, tick in enumerate(records):
            ts = tick['timestamp']
            
            # --- 撮合引擎提取盘口耗时 ---
            t_start = time.time()
            bids, asks = [], []
            for d in range(depth_limit):
                bp = tick.get(f'b_p_{d}')
                if bp is None or pd.isna(bp): break
                bids.append((bp, tick.get(f'b_q_{d}')))
                asks.append((tick.get(f'a_p_{d}'), tick.get(f'a_q_{d}')))
                
            self.broker.update_l2(self.symbol, ts, bids, asks)
            self.time_stats['broker_update'] += (time.time() - t_start)
            
            # --- 策略逻辑计算耗时 ---
            t_start = time.time()
            self.strategy.on_tick(tick)
            self.time_stats['strategy'] += (time.time() - t_start)
            
            # --- 资金快照耗时 ---
            if i % 1000 == 0:
                t_start = time.time()
                self.broker.record_equity()
                self.time_stats['equity'] += (time.time() - t_start)
                
        self.broker.record_equity()
        self.strategy.on_finish()
        
        elapsed = time.time() - start_total
        print(f"✅ 回测计算完成！耗时: {elapsed:.2f} 秒 (处理速度: {len(df)/elapsed:,.0f} ticks/秒)")
        
        # --- 打印性能拆解报告 ---
        print("\n" + "="*20 + " ⏱️ 引擎耗时分析 (Time Profiling) " + "="*20)
        print(f"  ├─ 数据加载与重构 (JIT): {self.time_stats['load_data']:.3f} 秒")
        print(f"  ├─ 结构转换 (to_dict):   {self.time_stats['to_dict']:.3f} 秒")
        print(f"  ├─ Broker 解析并撮合:    {self.time_stats['broker_update']:.3f} 秒")
        print(f"  ├─ 策略运算 (Strategy):  {self.time_stats['strategy']:.3f} 秒")
        print(f"  └─ 资金快照 (Equity):    {self.time_stats['equity']:.3f} 秒")
        print("="*65)
        
        self.print_results()

    def print_results(self):
        pass


class JitBacktestEngine(BacktestEngine):
    """
    具备 JIT 实时盘口重构、多线程解析与 Feature 落盘能力的进阶回测引擎
    """
    def __init__(self, data_paths, strategy, symbol, is_raw, init_cash, m_fee, t_fee, lev, c_dir, config_path="configs/broker.yaml"):
        super().__init__(data_paths=data_paths, strategy=strategy, symbol=symbol, config_path=config_path)
        self.is_raw = is_raw
        self.cache_dir = c_dir
        
        # 覆盖基础配置参数
        self.broker.initial_cash = float(init_cash)
        self.broker.cash = float(init_cash)
        self.broker.maker_fee = float(m_fee)
        self.broker.taker_fee = float(t_fee)
        self.broker.leverage = float(lev)
        self.broker.positions = {}
        self.broker.entry_prices = {}
        self.broker.active_orders = {}

    def _load_and_merge_data(self):
        if self.is_raw:
            file_names = "".join(sorted([os.path.basename(p) for p in self.data_paths]))
            hash_str = hashlib.md5(file_names.encode('utf-8')).hexdigest()[:8]
            cache_filename = f"{self.symbol}_100ms_merged_{hash_str}.parquet"
            cache_file_path = os.path.join(self.cache_dir, cache_filename)

            if os.path.exists(cache_file_path):
                print(f"\n⚡ 命中本地特征缓存落盘: {cache_file_path}")
                st.toast(f"⚡ 命中特征缓存，即将开始光速回测！", icon="⚡")
                return pd.read_parquet(cache_file_path)
            
            print(f"\n⏳ 未命中本地特征缓存，触发引擎全量重构机制 (耗时较长，请耐心等待)...")
            st.toast(f"⏳ 首次组合，正在执行多线程读取与底层深度重构...", icon="⏳")
            
            t_read = time.time()
            dataset = ds.dataset(self.data_paths, format="parquet")
            table = dataset.to_table(columns=['timestamp', 'side', 'price', 'quantity'])
            df = table.to_pandas()
            print(f"  ├─ 并发读取 {len(self.data_paths)} 个文件耗时: {time.time() - t_read:.2f} 秒")
            
            t_sort = time.time()
            df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False]).reset_index(drop=True)
            print(f"  ├─ {len(df):,} 行原始数据全局内存排序耗时: {time.time() - t_sort:.2f} 秒")
            
            t_recon = time.time()
            recon = L2Reconstructor(depth_limit=10)
            reconstructed_df = recon.process_dataframe(df, sample_interval_ms=100)
            print(f"  ├─ L2 盘口微观特征重构 (100ms切片) 耗时: {time.time() - t_recon:.2f} 秒")
            
            # 🚀 接入 FeatureBuilder：在此处执行离线向量化高级衍生特征计算
            t_feat = time.time()
            fb = FeatureBuilder()
            reconstructed_df = fb.build_offline(reconstructed_df)
            print(f"  ├─ 高级特征向量化衍生 (Alpha Features) 耗时: {time.time() - t_feat:.2f} 秒")
            
            del df
            del table
            
            if not reconstructed_df.empty:
                os.makedirs(self.cache_dir, exist_ok=True)
                reconstructed_df.to_parquet(cache_file_path, engine='pyarrow', compression='snappy')
                print(f"💾 深度重构完毕！已保存至特征缓存目录: {cache_file_path}")
                st.toast(f"💾 特征已成功落盘！以后加载相同文件只需 1 秒。", icon="💾")
                
            return reconstructed_df
        
        return super()._load_and_merge_data()