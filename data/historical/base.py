"""
HistoricalSource 抽象基类。

历史数据来源的统一接口。每个数据源（Binance Vision、Tardis.dev、Coincheck……）
实现 download_day()，上层下载器只需遍历 source × symbol × date。

数据契约：
  - download_day() 返回本地文件路径；若失败/无数据返回 None
  - 每个 source 自己决定落盘格式（parquet / csv.gz / json），上层不关心
  - 如果 source 提供校验 (checksum / 官方对账文件)，实现 verify(); 否则返回 (True, "not supported")
"""

from abc import ABC, abstractmethod
from pathlib import Path


class HistoricalSource(ABC):
    """历史数据来源抽象基类。"""

    name: str = ""              # 例: "binance_vision", "tardis"
    supports_markets: list[str] = []   # 例: ["spot", "um_futures"]
    supports_data_types: list[str] = []  # 例: ["aggTrades", "depth"]

    @abstractmethod
    def download_day(self, symbol: str, date_str: str,
                     data_type: str, market_type: str,
                     output_dir: str | Path) -> str | None:
        """
        下载单个交易对某一天某一类型的数据。

        参数:
            symbol:      交易对（源原生符号，如 ETHUSDT）
            date_str:    YYYY-MM-DD
            data_type:   数据类型 (aggTrades / depth / bookTicker / ...)
            market_type: 市场 (spot / um_futures)
            output_dir:  输出目录

        返回本地文件路径（str），失败返回 None。
        """

    def verify(self, local_path: str, symbol: str, date_str: str,
               data_type: str, market_type: str) -> tuple[bool, str]:
        """
        对已下载的文件做一致性校验（如比对 MD5）。
        默认不支持，子类可覆盖。
        """
        return True, "verification not supported"

    def supports(self, data_type: str, market_type: str) -> bool:
        """是否支持某 (data_type, market_type) 组合。"""
        if self.supports_markets and market_type not in self.supports_markets:
            return False
        if self.supports_data_types and data_type not in self.supports_data_types:
            return False
        return True
