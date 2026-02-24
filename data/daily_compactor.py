import os
import glob
import time
import zipfile
import urllib.request
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.dataset as ds
from datetime import datetime, timedelta
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DailyCompactor")

class DailyCompactor:
    def __init__(self, symbol, target_date, raw_dir, official_dir):
        """
        :param symbol: 交易对，如 ETHUSDT
        :param target_date: 需要聚合的日期 (datetime.date 对象)
        :param raw_dir: L2 1分钟小文件的存储目录
        :param official_dir: 用于存放币安官方下载数据的目录
        """
        self.symbol = symbol.upper()
        self.target_date = target_date
        self.date_str = target_date.strftime("%Y%m%d")
        self.date_str_dash = target_date.strftime("%Y-%m-%d")
        
        self.raw_dir = raw_dir
        self.official_dir = official_dir
        self.daily_file_path = os.path.join(self.raw_dir, f"{self.symbol}_RAW_{self.date_str}_DAILY.parquet")
        
        os.makedirs(self.official_dir, exist_ok=True)

    def compact_small_files(self):
        """将一天内的所有 1min 小文件合并为 1 个 DAILY 大文件，并保持小文件不删除"""
        logger.info(f"[{self.symbol}] 开始聚合 {self.date_str} 的 1min 级 L2 原始数据...")
        
        pattern = os.path.join(self.raw_dir, f"{self.symbol}_RAW_{self.date_str}_*.parquet")
        files = glob.glob(pattern)
        
        # 过滤掉可能已经存在的 DAILY 文件
        files = [f for f in files if "DAILY" not in f]
        
        if not files:
            logger.warning(f"[{self.symbol}] 未找到 {self.date_str} 的任何录制文件。")
            return False
            
        logger.info(f"[{self.symbol}] 找到 {len(files)} 个 1min 切片文件，开始使用 PyArrow 引擎合并...")
        
        # 使用 PyArrow Dataset 极速加载并合并
        dataset = ds.dataset(files, format="parquet")
        combined_table = dataset.to_table()
        df = combined_table.to_pandas()
        
        # 严格按时间戳和优先级排序
        df = df.sort_values(by=['timestamp', 'side'], ascending=[True, False]).reset_index(drop=True)
        
        # 写入大文件
        df.to_parquet(self.daily_file_path, index=False)
        logger.info(f"✅ 聚合完成！共处理 {len(df):,} 行数据。文件已保存至: {self.daily_file_path}")
        
        # 注意：此处遵守要求，不对原始 1min 小文件进行 os.remove() 删除
        return True

    def download_official_agg_trades(self):
        """从 Binance Vision 下载官方 AggTrades (L1) 历史数据用于校验"""
        base_url = "https://data.binance.vision/data/futures/um/daily/aggTrades"
        zip_filename = f"{self.symbol}-aggTrades-{self.date_str_dash}.zip"
        download_url = f"{base_url}/{self.symbol}/{zip_filename}"
        
        zip_path = os.path.join(self.official_dir, zip_filename)
        csv_path = zip_path.replace(".zip", ".csv")
        
        if os.path.exists(csv_path):
            logger.info(f"官方 L1 数据已存在: {csv_path}")
            return csv_path
            
        logger.info(f"正在从币安官方下载 {self.date_str_dash} 的 AggTrades 校验数据...")
        try:
            urllib.request.urlretrieve(download_url, zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(self.official_dir)
            os.remove(zip_path) # 删除压缩包
            logger.info(f"✅ 官方数据下载并解压成功。")
            return csv_path
        except Exception as e:
            logger.error(f"❌ 官方数据下载失败 (可能由于币安尚未生成该日的归档): {e}")
            return None

    def validate_data(self, official_csv_path):
        """读取聚合后的 DAILY 文件，提取 L1 还原数据，与官方数据进行交叉比对"""
        if not os.path.exists(self.daily_file_path):
            logger.error("找不到聚合后的 DAILY 文件，无法进行校验。")
            return
            
        logger.info(f"🔍 开始交叉验证 {self.date_str} 的微观数据一致性...")
        
        # 1. 解析本地聚合 L2 文件中的 L1 数据
        df_l2 = pd.read_parquet(self.daily_file_path)
        df_local_trades = df_l2[df_l2['side'] == 2].copy()
        df_local_trades['quantity'] = df_local_trades['quantity'].abs()
        local_count = len(df_local_trades)
        local_volume = df_local_trades['quantity'].sum()
        
        # 2. 读取官方数据
        # Binance CSV 格式: agg_trade_id, price, quantity, first_trade_id, last_trade_id, transact_time, is_buyer_maker
        df_official = pd.read_csv(official_csv_path, header=0, names=[
            'agg_trade_id', 'price', 'quantity', 'first_trade_id', 'last_trade_id', 'transact_time', 'is_buyer_maker'
        ])
        official_count = len(df_official)
        official_volume = df_official['quantity'].sum()
        
        # 3. 计算差异
        count_diff = local_count - official_count
        vol_diff = local_volume - official_volume
        vol_diff_pct = (vol_diff / official_volume * 100) if official_volume > 0 else 0
        
        logger.info("-" * 40)
        logger.info(f"📊 【{self.symbol} - {self.date_str_dash}】 L1 数据交叉校验报告")
        logger.info(f"▶ 官方数据: {official_count:,} 笔 | 总量: {official_volume:.4f}")
        logger.info(f"▶ 本地录制: {local_count:,} 笔 | 总量: {local_volume:.4f}")
        logger.info(f"▶ 笔数差异: {count_diff:,} 笔")
        logger.info(f"▶ 体积差异: {vol_diff:.4f} ({vol_diff_pct:.4f}%)")
        
        if count_diff == 0 and abs(vol_diff) < 1e-5:
            logger.info("🎉 完美匹配！本地录制流无任何掉包/漏单。")
        else:
            logger.warning("⚠️ 存在差异！网络波动或 WebSocket 重连可能导致了部分数据丢失。")
        logger.info("-" * 40)

    def run(self):
        """执行完整工作流"""
        success = self.compact_small_files()
        if success:
            official_csv = self.download_official_agg_trades()
            if official_csv:
                self.validate_data(official_csv)
            else:
                logger.warning("跳过校验：未获取到官方对比文件。")