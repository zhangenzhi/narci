import os
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import zipfile
import sys
import json

# 导入校验工具
try:
    from data.validator import BinanceDataValidator
except ImportError:
    print("Warning: validator.py not found. Validation will be skipped.")
    BinanceDataValidator = None

class BinanceDownloader:
    def __init__(self, symbol="ETHUSDT", data_type="aggTrades", base_url="https://data.binance.vision"):
        """
        :param symbol: 交易对
        :param data_type: 数据类型 ('aggTrades' 为 L1, 'depth' 或 'orderBook' 为 L2)
        :param base_url: 币安历史数据基础 URL
        """
        self.symbol = symbol
        self.data_type = data_type
        self.base_url = base_url
        self.save_dir = f"./data/parquet/{data_type}"
        self.validator = BinanceDataValidator() if BinanceDataValidator else None
        os.makedirs(self.save_dir, exist_ok=True)

    def process_and_save(self, date_str):
        """
        下载并处理数据。支持 L1 (aggTrades) 和 L2 (depth)
        """
        url = f"{self.base_url}/data/spot/daily/{self.data_type}/{self.symbol}/{self.symbol}-{self.data_type}-{date_str}.zip"
        target_path = os.path.join(self.save_dir, f"{self.symbol}-{date_str}.parquet")
        
        if os.path.exists(target_path):
            return f"Skip {date_str}: File already exists."

        try:
            # 1. Download
            response = requests.get(url, timeout=60)
            if response.status_code == 404:
                return f"Error {date_str}: Data not found (404). Check if {self.data_type} exists for this date."
            response.raise_for_status()

            # 2. Extract and Process
            with zipfile.ZipFile(BytesIO(response.content)) as z:
                csv_filename = z.namelist()[0]
                with z.open(csv_filename) as f:
                    if self.data_type == 'aggTrades':
                        # L1 数据处理
                        df = pd.read_csv(f, header=None, names=[
                            'agg_trade_id', 'price', 'quantity', 'first_trade_id', 
                            'last_trade_id', 'timestamp', 'is_buyer_maker', 'is_best_match'
                        ])
                        unit = self._detect_timestamp_unit(df['timestamp'].iloc[0])
                        df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)
                    
                    elif self.data_type in ['depth', 'orderBook']:
                        # L2 深度数据处理
                        # 币安 L2 CSV 通常包含: timestamp, side, price, quantity
                        # 或者是一个包含了 bids/asks 快照的结构
                        df = pd.read_csv(f) 
                        if 'timestamp' in df.columns:
                            unit = self._detect_timestamp_unit(df['timestamp'].iloc[0])
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)
                    else:
                        df = pd.read_csv(f)

            # 3. Validation (Only for supported types)
            if self.validator and self.data_type == 'aggTrades':
                report = self.validator.validate_dataframe(df, date_str)
                if not report["is_valid"]:
                    return f"❌ {date_str} Validation Failed: {report['errors']}"

            # 4. Save to Parquet
            temp_path = f"{target_path}.tmp"
            df.to_parquet(temp_path, engine='pyarrow', compression='snappy', index=False)
            os.rename(temp_path, target_path)
            
            return f"✅ {date_str} {self.data_type} Downloaded & Processed."

        except Exception as e:
            return f"💥 {date_str} Error: {str(e)}"

    def _detect_timestamp_unit(self, ts):
        """自动识别时间戳单位"""
        if ts > 1e17: return 'ns'
        if ts > 1e14: return 'us'
        return 'ms'

    def run_parallel(self, date_list, max_workers=2):
        """
        L2 数据体积巨大，建议降低线程数以防止内存溢出
        """
        print(f"🚀 Starting {self.data_type} download tasks...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = list(executor.map(self.process_and_save, date_list))
        
        print("\n" + "="*30 + " SUMMARY " + "="*30)
        for res in results:
            print(res)

if __name__ == "__main__":
    # 使用示例
    dates = ["2026-01-20", "2026-01-21"]
    
    # 下载 L1 数据 (成交数据)
    # l1_downloader = BinanceDownloader(data_type="aggTrades")
    # l1_downloader.run_parallel(dates)
    
    # 下载 L2 数据 (深度数据 - 如果 Binance 提供了该日期的 depth)
    l2_downloader = BinanceDownloader(data_type="depth")
    l2_downloader.run_parallel(dates, max_workers=2)