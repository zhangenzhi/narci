import os
import sys
import yaml
import requests
import pandas as pd
import zipfile
import shutil
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from data.validator import BinanceDataValidator


class BinanceDownloader:
    def __init__(self, config_path="configs/downloader.yaml"):
        # 修正：默认路径指正，防止找不到文件
        if not os.path.exists(config_path):
             # 尝试在当前目录下找
             if os.path.exists("downloader.yaml"):
                 config_path = "downloader.yaml"
                 
        self.config = self._load_config(config_path)
        self.base_dir = self.config.get('base_dir', './replay_buffer/parquet')
        self.retry_cfg = self.config.get('retry', {})
        
        # 初始化 session，配置全局重试策略
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
            status_forcelist=[500, 502, 503, 504, 520, 524], # 针对服务端错误重试
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_target_path(self, symbol, date_str, data_type):
        """生成目标文件路径，按类型和币种分目录存储"""
        # 结构: ./replay_buffer/parquet/aggTrades/ETHUSDT/ETHUSDT-2026-01-01.parquet
        dir_path = os.path.join(self.base_dir, data_type, symbol)
        os.makedirs(dir_path, exist_ok=True)
        filename = f"{symbol}-{date_str}.parquet"
        return os.path.join(dir_path, filename)

    def download_file(self, url, local_path):
        """流式下载文件到本地临时路径 (内存安全)"""
        timeout = self.retry_cfg.get('timeout', 30)
        try:
            with self.session.get(url, stream=True, timeout=timeout) as r:
                if r.status_code == 404:
                    return False # 文件不存在
                r.raise_for_status()
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            return True
        except Exception as e:
            # 下载中断或网络错误，清理残余文件
            if os.path.exists(local_path):
                os.remove(local_path)
            raise e

    def process_task(self, task):
        """单个下载任务的处理逻辑"""
        symbol, date_str, data_type = task
        final_path = self._get_target_path(symbol, date_str, data_type)
        
        # 1. 断点续传检查
        if os.path.exists(final_path):
            return f"⏩ [跳过] {symbol} {data_type} {date_str}: 文件已存在"

        base_url = "https://data.binance.vision"
        zip_name = f"{symbol}-{data_type}-{date_str}.zip"
        url = f"{base_url}/data/spot/daily/{data_type}/{symbol}/{zip_name}"
        
        temp_zip = final_path + ".zip"
        
        try:
            # 2. 流式下载 ZIP
            success = self.download_file(url, temp_zip)
            if not success:
                return f"⚠️ [404] {symbol} {date_str}: 官方数据尚未归档"

            # 3. 解压并处理
            with zipfile.ZipFile(temp_zip) as z:
                csv_name = z.namelist()[0]
                with z.open(csv_name) as f:
                    if data_type == 'aggTrades':
                        # L1 标准化
                        # 显式指定 dtype 防止首行被误判
                        df = pd.read_csv(f, header=None, names=[
                            'agg_trade_id', 'price', 'quantity', 'first_trade_id', 
                            'last_trade_id', 'timestamp', 'is_buyer_maker', 'is_best_match'
                        ])
                        
                        # 时间戳统一 (修复: 增加微秒 us 支持)
                        if not df.empty:
                            ts_sample = df['timestamp'].max()
                            
                            if ts_sample < 1e11:
                                unit = 's'
                            elif ts_sample < 1e14:
                                unit = 'ms'
                            else:
                                unit = 'us'

                            # 安全转换，防止溢出
                            try:
                                df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)
                            except pd.errors.OutOfBoundsDatetime:
                                return f"❌ [错误] {symbol} {date_str}: 时间戳溢出 (Sample: {ts_sample}, Unit: {unit})"

                    else:
                        # L2 深度或其他
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
            
            return f"✅ [成功] {symbol} {data_type} {date_str}"

        except Exception as e:
            return f"❌ [错误] {symbol} {date_str}: {str(e)}"
        finally:
            # 清理临时 ZIP 文件
            if os.path.exists(temp_zip):
                os.remove(temp_zip)

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

        for date_str in date_list:
            for symbol in self.config['symbols']:
                for dtype in self.config['data_types']:
                    tasks.append((symbol, date_str, dtype))
        return tasks

    def run(self):
        tasks = self.generate_tasks()
        max_workers = self.config.get('max_workers', 2)
        
        print(f"🚀 启动下载引擎 | 任务数: {len(tasks)} | 线程: {max_workers}")
        print(f"📂 存储路径: {self.base_dir}")
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {executor.submit(self.process_task, task): task for task in tasks}
            
            for future in as_completed(future_to_task):
                try:
                    result = future.result()
                    # 仅打印非跳过的信息，保持日志清爽
                    if "⏩" not in result:
                        print(result)
                except Exception as e:
                    print(f"💥 线程异常: {e}")

if __name__ == "__main__":
    # 需要先安装 pyyaml: pip install pyyaml
    downloader = BinanceDownloader()
    try:
        downloader.run()
    except KeyboardInterrupt:
        print("\n🛑 用户停止任务")