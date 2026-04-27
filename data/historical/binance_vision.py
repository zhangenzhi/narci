"""
Binance Vision 历史数据源。

从 https://data.binance.vision 下载 ZIP 归档，解压转换为 parquet。
支持现货和 U 本位合约。核心校验通过官方 .CHECKSUM 文件比对 MD5。
"""

import hashlib
import io
import logging
import os
import zipfile
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import HistoricalSource

logger = logging.getLogger(__name__)


class BinanceVisionSource(HistoricalSource):
    name = "binance_vision"
    supports_markets = ["spot", "um_futures"]
    supports_data_types = ["aggTrades", "trades", "klines", "bookTicker"]

    BASE_URL = "https://data.binance.vision"

    def __init__(self, retry_cfg: dict | None = None):
        retry_cfg = retry_cfg or {}
        self.timeout = retry_cfg.get("timeout", 30)
        self.session = requests.Session()
        retry = Retry(
            total=retry_cfg.get("max_retries", 3),
            backoff_factor=retry_cfg.get("backoff_factor", 1),
            status_forcelist=[500, 502, 503, 504, 520, 524],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ #
    # URL 构造
    # ------------------------------------------------------------------ #

    def _build_url(self, symbol: str, date_str: str,
                   data_type: str, market_type: str) -> str:
        zip_name = f"{symbol}-{data_type}-{date_str}.zip"
        if market_type == "um_futures":
            return f"{self.BASE_URL}/data/futures/um/daily/{data_type}/{symbol}/{zip_name}"
        return f"{self.BASE_URL}/data/spot/daily/{data_type}/{symbol}/{zip_name}"

    def _checksum_url(self, symbol: str, date_str: str,
                      data_type: str, market_type: str) -> str:
        return self._build_url(symbol, date_str, data_type, market_type) + ".CHECKSUM"

    # ------------------------------------------------------------------ #
    # 下载与转换
    # ------------------------------------------------------------------ #

    def download_day(self, symbol: str, date_str: str,
                     data_type: str, market_type: str,
                     output_dir: str | Path) -> str | None:
        if not self.supports(data_type, market_type):
            return None

        out_dir = Path(output_dir) / market_type / data_type / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / f"{symbol}-{date_str}.parquet"

        if final_path.exists():
            return str(final_path)

        # Offline mode: refuse outbound HTTP. Set BINANCE_VISION_OFFLINE=1 on
        # machines that must not reach data.binance.vision directly. The donor
        # machine downloads + pushes parquets to Drive; this machine reads them
        # locally only after rclone sync. Warn + return None so bulk runs (e.g.
        # `compact --symbol ALL`) keep going on the next date.
        if os.environ.get("BINANCE_VISION_OFFLINE", "").strip():
            logger.warning(
                f"BINANCE_VISION_OFFLINE=1, local parquet missing: {final_path}. "
                f"Skipping cross-validation; donor must push first."
            )
            return None

        url = self._build_url(symbol, date_str, data_type, market_type)
        temp_zip = str(final_path) + ".zip"

        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as r:
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                with open(temp_zip, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            with zipfile.ZipFile(temp_zip) as z:
                csv_name = z.namelist()[0]
                with z.open(csv_name) as f:
                    df = self._parse_csv(f, data_type)

            df.to_parquet(str(final_path), engine="pyarrow",
                          compression="snappy", index=False)
            return str(final_path)
        except Exception as e:
            if os.path.exists(str(final_path)):
                os.remove(str(final_path))
            raise e
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)

    def _parse_csv(self, f, data_type: str) -> pd.DataFrame:
        # Binance Vision aggTrade CSVs gained a header row in 2025; old ones don't.
        # Detect by checking whether the first cell is numeric, and remap columns by position.
        data = f.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        if not data.strip():
            return pd.DataFrame()

        if data_type == "aggTrades":
            cols = [
                "agg_trade_id", "price", "quantity", "first_trade_id",
                "last_trade_id", "timestamp", "is_buyer_maker", "is_best_match",
            ]
            first_line = data.split("\n", 1)[0]
            ncols = len(first_line.split(","))
            first_cell = first_line.split(",", 1)[0]
            has_header = not first_cell.lstrip("-").isdigit()
            df = pd.read_csv(
                io.StringIO(data),
                header=0 if has_header else None,
                names=None if has_header else cols[:ncols],
            )
            df.columns = cols[: df.shape[1]]
        else:
            df = pd.read_csv(io.StringIO(data))

        if "timestamp" in df.columns and not df.empty:
            ts_sample = df["timestamp"].max()
            unit = "s" if ts_sample < 1e11 else ("ms" if ts_sample < 1e14 else "us")
            try:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit=unit)
            except pd.errors.OutOfBoundsDatetime:
                pass
        return df

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    @staticmethod
    def _md5(path: str) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                h.update(chunk)
        return h.hexdigest()

    def verify(self, local_path: str, symbol: str, date_str: str,
               data_type: str, market_type: str) -> tuple[bool, str]:
        """比对 Binance 官方 .CHECKSUM 文件。注意：校验目标是 ZIP 而非解压后 parquet。"""
        url = self._checksum_url(symbol, date_str, data_type, market_type)
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return False, f"checksum fetch failed: HTTP {resp.status_code}"
            expected = resp.text.split()[0].strip()
            actual = self._md5(local_path)
            if expected == actual:
                return True, "MD5 ok"
            return False, f"MD5 mismatch: expected {expected}, got {actual}"
        except Exception as e:
            return False, f"verify error: {e}"
