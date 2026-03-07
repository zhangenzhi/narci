import os
import sys
import yaml
import requests
import pandas as pd
import zipfile
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from data.validator import BinanceDataValidator


class BinanceDownloader:
    def __init__(self, config_path="configs/downloader.yaml"):
        if not os.path.exists(config_path):
             if os.path.exists("downloader.yaml"):
                 config_path = "downloader.yaml"

        self.config = self._load_config(config_path)
        self.base_dir = self.config.get('base_dir', './replay_buffer/parquet')
        self.retry_cfg = self.config.get('retry', {})

        self.session = self._setup_session()
        self.validator = BinanceDataValidator() if BinanceDataValidator else None

    def _load_config(self, path):
        if not os.path.exists(path):
            print(f"❌ 配置文件 {path} 不存在，请先创建。")
            sys.exit(1)
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f).get('downloader', {})

    def _setup_session(self):
        """配置带有指数退避重试策略的 Requests Session"""
        session = requests.Session()
        retries = self.retry_cfg.get('max_retries', 3)
        backoff = self.retry_cfg.get('backoff_factor', 1)

        retry_strategy = Retry(
            total=retries,
            backoff_factor=backoff,
            status_forcelist=[500, 502, 503, 504, 520, 524],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_target_path(self, symbol, date_str, data_type, market_type):
        """生成目标文件路径，按市场/类型/币种分目录存储"""
        # 结构: ./replay_buffer/parquet/{market_type}/aggTrades/ETHUSDT/ETHUSDT-2026-01-01.parquet
        dir_path = os.path.join(self.base_dir, market_type, data_type, symbol)
        os.makedirs(dir_path, exist_ok=True)
        filename = f"{symbol}-{date_str}.parquet"
        return os.path.join(dir_path, filename)

    def _build_url(self, symbol, date_str, data_type, market_type):
        """根据市场类型构建 Binance Vision 下载 URL"""
        base_url = "https://data.binance.vision"
        zip_name = f"{symbol}-{data_type}-{date_str}.zip"

        if market_type == "um_futures":
            return f"{base_url}/data/futures/um/daily/{data_type}/{symbol}/{zip_name}"
        else:
            return f"{base_url}/data/spot/daily/{data_type}/{symbol}/{zip_name}"

    def download_file(self, url, local_path):
        """流式下载文件到本地临时路径 (内存安全)"""
        timeout = self.retry_cfg.get('timeout', 30)
        try:
            with self.session.get(url, stream=True, timeout=timeout) as r:
                if r.status_code == 404:
                    return False
                r.raise_for_status()
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            if os.path.exists(local_path):
                os.remove(local_path)
            raise e

    def process_task(self, task):
        """单个下载任务的处理逻辑"""
        symbol, date_str, data_type, market_type = task
        final_path = self._get_target_path(symbol, date_str, data_type, market_type)

        # 1. 断点续传检查
        if os.path.exists(final_path):
            return f"⏩ [跳过] {market_type}/{symbol} {data_type} {date_str}"

        url = self._build_url(symbol, date_str, data_type, market_type)
        temp_zip = final_path + ".zip"

        try:
            # 2. 流式下载 ZIP
            success = self.download_file(url, temp_zip)
            if not success:
                return f"⚠️ [404] {market_type}/{symbol} {date_str}: 官方数据尚未归档"

            # 3. 解压并处理
            with zipfile.ZipFile(temp_zip) as z:
                csv_name = z.namelist()[0]
                with z.open(csv_name) as f:
                    if data_type == 'aggTrades':
                        df = pd.read_csv(f, header=None, names=[
                            'agg_trade_id', 'price', 'quantity', 'first_trade_id',
                            'last_trade_id', 'timestamp', 'is_buyer_maker', 'is_best_match'
                        ])

                        if not df.empty:
                            ts_sample = df['timestamp'].max()
                            if ts_sample < 1e11:
                                unit = 's'
                            elif ts_sample < 1e14:
                                unit = 'ms'
                            else:
                                unit = 'us'

                            try:
                                df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)
                            except pd.errors.OutOfBoundsDatetime:
                                return f"❌ [错误] {market_type}/{symbol} {date_str}: 时间戳溢出"
                    else:
                        df = pd.read_csv(f)
                        if 'timestamp' in df.columns and not df.empty:
                            ts_sample = df['timestamp'].max()
                            if ts_sample < 1e11:
                                unit = 's'
                            elif ts_sample < 1e14:
                                unit = 'ms'
                            else:
                                unit = 'us'
                            df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)

            # 4. (可选) 数据校验
            if self.validator and data_type == 'aggTrades':
                report = self.validator.validate_dataframe(df, date_str)
                if not report["is_valid"]:
                    raise ValueError(f"校验失败: {report['errors']}")

            # 5. 转换为 Parquet 并清理临时文件
            df.to_parquet(final_path, engine='pyarrow', compression='snappy', index=False)

            return f"✅ [{market_type}] {symbol} {data_type} {date_str}"

        except Exception as e:
            return f"❌ [错误] {market_type}/{symbol} {date_str}: {str(e)}"
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)

    def _resolve_market_types(self):
        """解析配置中的 market_type，返回市场类型列表"""
        mt = self.config.get('market_type', 'spot')
        if mt == 'both':
            return ['spot', 'um_futures']
        if isinstance(mt, list):
            return mt
        return [mt]

    def generate_tasks(self):
        """生成任务队列"""
        tasks = []
        if not self.config.get('date_range'):
            print("❌ 配置错误: date_range 缺失")
            return []

        start_str = self.config['date_range']['start_date']
        end_str = self.config['date_range']['end_date']

        try:
            start = datetime.strptime(start_str, "%Y-%m-%d")
            if end_str == "auto":
                end = datetime.now() - timedelta(days=1)
            else:
                end = datetime.strptime(end_str, "%Y-%m-%d")

            date_list = [
                (start + timedelta(days=x)).strftime("%Y-%m-%d")
                for x in range((end - start).days + 1)
            ]
        except ValueError as e:
            print(f"❌ 日期格式错误: {e}")
            return []

        market_types = self._resolve_market_types()

        for market_type in market_types:
            for date_str in date_list:
                for symbol in self.config['symbols']:
                    for dtype in self.config['data_types']:
                        tasks.append((symbol, date_str, dtype, market_type))
        return tasks

    def run(self):
        tasks = self.generate_tasks()
        max_workers = self.config.get('max_workers', 2)
        market_types = self._resolve_market_types()

        print(f"🚀 启动下载引擎 | 任务数: {len(tasks)} | 线程: {max_workers}")
        print(f"📂 存储路径: {self.base_dir}")
        print(f"🏪 市场: {market_types}")
        print(f"💰 交易对: {self.config['symbols']}")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {executor.submit(self.process_task, task): task for task in tasks}

            for future in as_completed(future_to_task):
                try:
                    result = future.result()
                    if "⏩" not in result:
                        print(result)
                except Exception as e:
                    print(f"💥 线程异常: {e}")

if __name__ == "__main__":
    downloader = BinanceDownloader()
    try:
        downloader.run()
    except KeyboardInterrupt:
        print("\n🛑 用户停止任务")
