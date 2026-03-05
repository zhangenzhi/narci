import os
import time
import requests
import pandas as pd
import numpy as np
import gzip
import io
from datetime import datetime, timedelta
from pathlib import Path
import logging
from tqdm import tqdm

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class CryptoDataPipeline:
    def __init__(self, base_dir: str = "./crypto_data"):
        """
        初始化数据管道
        :param base_dir: 本地数据存储的根目录
        """
        self.base_dir = Path(base_dir)
        self.raw_dir = self.base_dir / "raw"
        self.processed_dir = self.base_dir / "processed"
        
        # 创建所需目录
        for d in [self.raw_dir, self.processed_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        self.session = requests.Session()
        
    def _get_s3_url(self, data_type: str, exchange: str, symbol: str, date_str: str) -> str:
        """从 CryptoChassis API 获取特定日期的 S3 下载链接"""
        url = f"https://api.cryptochassis.com/v1/{data_type}/{exchange}/{symbol}?startTime={date_str}"
        try:
            res = self.session.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if 'urls' in data and len(data['urls']) > 0:
                    return data['urls'][0]['url']
        except Exception as e:
            logger.error(f"获取 {symbol} {data_type} {date_str} URL失败: {e}")
        return None

    def download_raw_data(self, data_type: str, exchange: str, symbol: str, date_str: str) -> Path:
        """下载单日原始 csv.gz 数据并保存到本地"""
        target_dir = self.raw_dir / data_type / symbol
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{symbol}_{data_type}_{date_str}.csv.gz"
        
        # 如果文件已存在，跳过下载（断点续传）
        if file_path.exists() and file_path.stat().st_size > 1024:
            return file_path
            
        s3_url = self._get_s3_url(data_type, exchange, symbol, date_str)
        if not s3_url:
            return None
            
        try:
            # 流式下载大文件
            response = self.session.get(s3_url, stream=True, timeout=30)
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            # 礼貌延迟，防止被封 IP
            time.sleep(1)
            return file_path
        except Exception as e:
            logger.error(f"下载失败 {file_path.name}: {e}")
            if file_path.exists():
                file_path.unlink() # 删除损坏的文件
            return None

    def process_daily_data(self, symbol: str, date_str: str, depth_limit: int = 5):
        """
        核心逻辑：读取当天的 Depth 和 Trade，聚合为 1 秒级的宽表，保存为 Parquet
        """
        depth_file = self.raw_dir / "market-depth" / symbol / f"{symbol}_market-depth_{date_str}.csv.gz"
        trade_file = self.raw_dir / "trade" / symbol / f"{symbol}_trade_{date_str}.csv.gz"
        
        out_dir = self.processed_dir / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{symbol}_features_{date_str}.parquet"
        
        # 如果已经处理过，直接跳过
        if out_file.exists():
            return
            
        if not depth_file.exists() or not trade_file.exists():
            logger.warning(f"缺少 {symbol} 在 {date_str} 的原始文件，跳过处理。")
            return

        try:
            # 1. 处理 Trades (聚合成 1s 级别的 taker_buy_vol / taker_sell_vol)
            # 字段通常为: time_seconds, price, size, is_buyer_maker
            df_trade = pd.read_csv(trade_file, compression='gzip', names=['time_seconds', 'price', 'size', 'is_buyer_maker'], header=0)
            
            # CryptoChassis 中 is_buyer_maker=1 代表主动卖出(Taker Sell), 0 代表主动买入(Taker Buy)
            df_trade['is_taker_buy'] = df_trade['is_buyer_maker'] == 0
            df_trade['is_taker_sell'] = df_trade['is_buyer_maker'] == 1
            
            df_trade['taker_buy_vol'] = np.where(df_trade['is_taker_buy'], df_trade['size'], 0.0)
            df_trade['taker_sell_vol'] = np.where(df_trade['is_taker_sell'], df_trade['size'], 0.0)
            
            # 向下取整到秒
            df_trade['time_sec_floor'] = np.floor(df_trade['time_seconds']).astype(int)
            
            # 按秒聚合
            trade_1s = df_trade.groupby('time_sec_floor').agg({
                'taker_buy_vol': 'sum',
                'taker_sell_vol': 'sum',
                'size': 'sum'  # total volume
            }).reset_index()

            # 2. 处理 Depth (解析字符串，取整到秒)
            df_depth = pd.read_csv(depth_file, compression='gzip', names=['time_seconds', 'bids', 'asks'], header=0)
            df_depth['time_sec_floor'] = np.floor(df_depth['time_seconds']).astype(int)
            
            # 去重：如果同一秒有多条快照，只保留最后一条
            df_depth = df_depth.drop_duplicates(subset=['time_sec_floor'], keep='last')
            
            # 字符串解析逻辑
            parsed_data = {'time_sec_floor': df_depth['time_sec_floor'].values}
            for i in range(depth_limit):
                parsed_data[f'b_p_{i}'] = []
                parsed_data[f'b_q_{i}'] = []
                parsed_data[f'a_p_{i}'] = []
                parsed_data[f'a_q_{i}'] = []
                
            for _, row in df_depth.iterrows():
                bids_str = str(row['bids']).split('|')
                asks_str = str(row['asks']).split('|')
                for i in range(depth_limit):
                    # Bids
                    if i < len(bids_str) and '_' in bids_str[i]:
                        p, q = bids_str[i].split('_')
                        parsed_data[f'b_p_{i}'].append(float(p))
                        parsed_data[f'b_q_{i}'].append(float(q))
                    else:
                        parsed_data[f'b_p_{i}'].append(np.nan)
                        parsed_data[f'b_q_{i}'].append(np.nan)
                    # Asks
                    if i < len(asks_str) and '_' in asks_str[i]:
                        p, q = asks_str[i].split('_')
                        parsed_data[f'a_p_{i}'].append(float(p))
                        parsed_data[f'a_q_{i}'].append(float(q))
                    else:
                        parsed_data[f'a_p_{i}'].append(np.nan)
                        parsed_data[f'a_q_{i}'].append(np.nan)
                        
            depth_1s = pd.DataFrame(parsed_data)

            # 3. 对齐合并 (Merge)
            df_final = pd.merge(depth_1s, trade_1s, on='time_sec_floor', how='left')
            
            # 填补没有成交的秒数
            df_final['taker_buy_vol'] = df_final['taker_buy_vol'].fillna(0.0)
            df_final['taker_sell_vol'] = df_final['taker_sell_vol'].fillna(0.0)
            
            # 基础预计算
            df_final['mid_price'] = (df_final['b_p_0'] + df_final['a_p_0']) / 2.0
            df_final['spread'] = df_final['a_p_0'] - df_final['b_p_0']
            df_final['timestamp'] = pd.to_datetime(df_final['time_sec_floor'], unit='s')
            df_final.set_index('timestamp', inplace=True)
            df_final.drop(columns=['time_sec_floor'], inplace=True)

            # 保存为高效的 Parquet 格式
            df_final.to_parquet(out_file, engine='pyarrow', compression='snappy')
            logger.info(f"已成功处理并保存: {out_file.name}")
            
        except Exception as e:
            logger.error(f"处理数据失败 {symbol} {date_str}: {e}")

    def run_pipeline(self, symbols: list, start_date: str, end_date: str):
        """执行完整的一年数据拉取与处理管道"""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        
        # 生成日期列表
        date_list = [(start + timedelta(days=x)).strftime("%Y-%m-%d") 
                     for x in range((end - start).days + 1)]
                     
        logger.info(f"开始执行数据管道，共计 {len(date_list)} 天，{len(symbols)} 个交易对。")

        for symbol in symbols:
            logger.info(f"========== 正在处理 {symbol} ==========")
            for date_str in tqdm(date_list, desc=f"{symbol} 进度"):
                # 1. 下载当天的 trade
                self.download_raw_data("trade", "binance", symbol, date_str)
                # 2. 下载当天的 depth
                self.download_raw_data("market-depth", "binance", symbol, date_str)
                # 3. 解析、对齐、落盘为 Parquet
                self.process_daily_data(symbol, date_str, depth_limit=5)


if __name__ == "__main__":
    # 实例化数据管道
    pipeline = CryptoDataPipeline(base_dir="./quant_data")
    
    # 定义目标：包含主流 USDT 交易对和部分代表性 JPY 交易对
    target_symbols = [
        # 主流 USDT 交易对
        "btc-usdt", "eth-usdt", "sol-usdt", "bnb-usdt", 
        "xrp-usdt", "doge-usdt", "ada-usdt", "avax-usdt", "link-usdt",
        # 日元 (JPY) 交易对
        "btc-jpy", "eth-jpy", "xrp-jpy", "sol-jpy", "doge-jpy"
    ]
    
    # 设定过去一年的时间范围 (2025-03-04 到 2026-03-04)
    # 建议一开始测试时，先只跑 3 天验证逻辑 (例如 '2026-03-01' 到 '2026-03-03')
    START_DATE = "2025-03-04"
    END_DATE = "2026-03-04"
    
    # 启动管道
    pipeline.run_pipeline(symbols=target_symbols, start_date=START_DATE, end_date=END_DATE)
    
    print("全部执行完毕！你的数据现在可以完美对接 FeatureEngineerV2 了。")
    print("读取示例: df = pd.read_parquet('./quant_data/processed/btc-usdt/btc-usdt_features_2025-03-04.parquet')")