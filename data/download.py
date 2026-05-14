"""
历史数据批量下载器（交易所无关）。

通过 HistoricalSource 抽象层委托给具体数据源。默认使用 Binance Vision。
配置可指定 source: binance_vision / tardis 等。
"""

import os
import sys
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from data.historical import get_source, HistoricalSource


class HistoricalDownloader:
    def __init__(self, config_path: str = "configs/downloader.yaml"):
        if not os.path.exists(config_path) and os.path.exists("downloader.yaml"):
            config_path = "downloader.yaml"
        self.config = self._load_config(config_path)
        self.base_dir = self.config.get("base_dir", "./replay_buffer/parquet")
        self.retry_cfg = self.config.get("retry", {})

        source_name = self.config.get("source", "binance_vision")
        source_kwargs = {}
        if source_name == "binance_vision":
            source_kwargs["retry_cfg"] = self.retry_cfg
        elif source_name == "tardis":
            source_kwargs["exchange"] = self.config.get("exchange", "binance")
        self.source: HistoricalSource = get_source(source_name, **source_kwargs)

        self.verify_checksum = self.config.get("verify_checksum", False)

    @staticmethod
    def _load_config(path: str) -> dict:
        if not os.path.exists(path):
            print(f"❌ 配置文件 {path} 不存在")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f).get("downloader", {})

    def _resolve_market_types(self) -> list[str]:
        mt = self.config.get("market_type", "spot")
        if mt == "both":
            return ["spot", "um_futures"]
        if isinstance(mt, list):
            return mt
        return [mt]

    def _symbols_for_market(self, market: str) -> list[str]:
        # symbols can be a flat list (applied to every market) or a dict keyed
        # by market_type — the latter lets us route JPY pairs to spot only,
        # since Binance Vision has no um_futures JPY archives.
        sym = self.config.get("symbols", [])
        if isinstance(sym, dict):
            return list(sym.get(market, []))
        return list(sym)

    def generate_tasks(self) -> list[tuple]:
        dr = self.config.get("date_range")
        if not dr:
            print("❌ 配置错误: date_range 缺失")
            return []

        start = datetime.strptime(dr["start_date"], "%Y-%m-%d")
        end_str = dr["end_date"]
        end = datetime.now() - timedelta(days=1) if end_str == "auto" \
            else datetime.strptime(end_str, "%Y-%m-%d")

        dates = [(start + timedelta(days=d)).strftime("%Y-%m-%d")
                 for d in range((end - start).days + 1)]

        tasks = []
        for market in self._resolve_market_types():
            symbols = self._symbols_for_market(market)
            for date_str in dates:
                for symbol in symbols:
                    for dtype in self.config["data_types"]:
                        tasks.append((symbol, date_str, dtype, market))
        return tasks

    def process_task(self, task: tuple) -> str:
        symbol, date_str, data_type, market_type = task

        if not self.source.supports(data_type, market_type):
            return f"⏩ [跳过] {self.source.name} 不支持 {market_type}/{data_type}"

        try:
            path = self.source.download_day(
                symbol, date_str, data_type, market_type, self.base_dir)
            if path is None:
                return f"⚠️ [404] {market_type}/{symbol} {data_type} {date_str}"
            return f"✅ [{market_type}] {symbol} {data_type} {date_str}"
        except Exception as e:
            return f"❌ [错误] {market_type}/{symbol} {date_str}: {e}"

    def run(self):
        tasks = self.generate_tasks()
        max_workers = self.config.get("max_workers", 2)

        print(f"🚀 启动下载引擎 | 任务数: {len(tasks)} | 线程: {max_workers}")
        print(f"📂 存储路径: {self.base_dir}")
        print(f"🔌 数据源: {self.source.name}")
        print(f"🏪 市场: {self._resolve_market_types()}")
        print(f"💰 交易对: {self.config.get('symbols')}")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.process_task, t): t for t in tasks}
            for fut in as_completed(futures):
                try:
                    result = fut.result()
                    if "⏩" not in result:
                        print(result)
                except Exception as e:
                    print(f"💥 线程异常: {e}")


# 向后兼容别名
BinanceDownloader = HistoricalDownloader


if __name__ == "__main__":
    dl = HistoricalDownloader()
    try:
        dl.run()
    except KeyboardInterrupt:
        print("\n🛑 用户停止任务")
