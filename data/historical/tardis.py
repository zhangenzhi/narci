"""
Tardis.dev 历史数据源。

提供 Binance 现货/合约、Coincheck 等多家交易所的完整 L2 depth 流（snapshot + diff）
和 aggTrades。这是唯一对 Coincheck / Binance 合约历史 L2 可用的公开源。

直接输出 Narci 4 列 RAW 格式（timestamp, side, price, quantity）。
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .base import HistoricalSource


class TardisSource(HistoricalSource):
    name = "tardis"
    supports_markets = ["spot", "um_futures"]
    supports_data_types = ["depth", "aggTrades"]

    BASE_URL = "https://api.tardis.dev/v1/data-feeds"

    # 标准 market_type -> Tardis 交易所标识
    EXCHANGE_MAP = {
        ("binance", "spot"):       "binance",
        ("binance", "um_futures"): "binance-futures",
        ("coincheck", "spot"):     "coincheck",
    }

    def __init__(self, exchange: str = "binance", api_key: str | None = None):
        self.exchange = exchange
        self.api_key = api_key or os.environ.get("TARDIS_API_KEY", "")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _tardis_exchange(self, market_type: str) -> str:
        key = (self.exchange, market_type)
        if key not in self.EXCHANGE_MAP:
            raise ValueError(f"Tardis 不支持 {self.exchange}/{market_type}")
        return self.EXCHANGE_MAP[key]

    def _build_url(self, exchange: str, symbol: str, date_str: str,
                   channels: list[str]) -> str:
        from_dt = f"{date_str}T00:00:00.000Z"
        to = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        to_dt = to.strftime("%Y-%m-%dT00:00:00.000Z")
        filters = json.dumps([
            {"channel": ch, "symbols": [symbol.lower()]} for ch in channels
        ])
        return f"{self.BASE_URL}/{exchange}?from={from_dt}&to={to_dt}&filters={filters}"

    def download_day(self, symbol: str, date_str: str,
                     data_type: str, market_type: str,
                     output_dir: str | Path) -> str | None:
        if not self.supports(data_type, market_type):
            return None

        tardis_exchange = self._tardis_exchange(market_type)
        out_dir = Path(output_dir) / market_type / data_type / symbol.upper()
        out_dir.mkdir(parents=True, exist_ok=True)

        date_compact = date_str.replace("-", "")
        out_file = out_dir / f"{symbol.upper()}_{date_compact}.parquet"
        if out_file.exists():
            return str(out_file)

        channels = {
            "depth":     ["depthSnapshot", "depth"],
            "aggTrades": ["aggTrade"],
        }[data_type]

        url = self._build_url(tardis_exchange, symbol, date_str, channels)

        try:
            resp = self.session.get(url, stream=True, timeout=30)
            if resp.status_code in (401, 403):
                return None
            resp.raise_for_status()
        except requests.RequestException:
            return None

        records = []
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                decoded = line.decode("utf-8")
                tab = decoded.index("\t")
                payload = json.loads(decoded[tab + 1:])
            except (ValueError, json.JSONDecodeError):
                continue

            stream = payload.get("stream", "")
            data = payload.get("data", {})
            ts = data.get("E", 0)

            if data_type == "depth":
                if "depthSnapshot" in stream:
                    for p, q in data.get("bids", []):
                        records.append([ts, 3, float(p), float(q)])
                    for p, q in data.get("asks", []):
                        records.append([ts, 4, float(p), float(q)])
                elif "depth" in stream:
                    for p, q in data.get("b", []):
                        records.append([ts, 0, float(p), float(q)])
                    for p, q in data.get("a", []):
                        records.append([ts, 1, float(p), float(q)])
            elif data_type == "aggTrades":
                p = float(data.get("p", 0))
                q = float(data.get("q", 0))
                if data.get("m"):
                    q = -q
                records.append([ts, 2, p, q])

        if not records:
            return None

        df = pd.DataFrame(records, columns=["timestamp", "side", "price", "quantity"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df.to_parquet(str(out_file), engine="pyarrow", compression="snappy", index=False)
        return str(out_file)
