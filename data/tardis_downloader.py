"""
Tardis.dev L2 depth data downloader.

Downloads orderbook depth snapshots and incremental updates from Tardis.dev,
then converts them to Narci's standard 4-column RAW parquet format:
  [timestamp(ms), side, price, quantity]

Side encoding (matches Narci Recorder):
  0 = bid incremental update (qty=0 means cancel)
  1 = ask incremental update
  2 = aggTrade (qty<0 for seller-maker)
  3 = bid snapshot
  4 = ask snapshot

Tardis.dev free tier: 10GB/month.
API docs: https://docs.tardis.dev/api/http-streaming
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)


class TardisDownloader:
    """
    Downloads L2 depth data from Tardis.dev and converts to Narci RAW format.

    Usage:
        dl = TardisDownloader(output_dir="./replay_buffer/parquet/um_futures/l2")
        dl.download_day("ETHUSDT", "2025-09-01", exchange="binance-futures")
    """

    BASE_URL = "https://api.tardis.dev/v1/data-feeds"

    # Tardis exchange identifiers
    EXCHANGE_MAP = {
        "um_futures": "binance-futures",
        "spot": "binance",
    }

    def __init__(self, output_dir: str = "./replay_buffer/tardis",
                 api_key: str = None):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = api_key or os.environ.get("TARDIS_API_KEY", "")
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def _build_url(self, exchange: str, symbol: str, date_str: str,
                   channels: list[str]) -> str:
        """Build Tardis streaming API URL for a single day."""
        sym_lower = symbol.lower()
        # Tardis uses ISO date format
        from_dt = f"{date_str}T00:00:00.000Z"
        # next day
        to_date = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1))
        to_dt = to_date.strftime("%Y-%m-%dT00:00:00.000Z")

        filters = json.dumps([
            {"channel": ch, "symbols": [sym_lower]}
            for ch in channels
        ])

        return (
            f"{self.BASE_URL}/{exchange}"
            f"?from={from_dt}&to={to_dt}"
            f"&filters={filters}"
        )

    def download_day(self, symbol: str, date_str: str,
                     market_type: str = "um_futures") -> str | None:
        """
        Download one day of L2 depth data from Tardis.dev and convert to
        Narci RAW parquet format.

        Returns the output file path, or None on failure.
        """
        exchange = self.EXCHANGE_MAP.get(market_type, "binance-futures")
        symbol_upper = symbol.upper()

        out_dir = self.output_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        # Output filename matches Narci convention: SYMBOL_RAW_YYYYMMDD_000000.parquet
        date_compact = date_str.replace("-", "")
        out_file = out_dir / f"{symbol_upper}_TARDIS_{date_compact}.parquet"

        if out_file.exists():
            logger.info(f"[skip] {out_file.name} already exists")
            return str(out_file)

        # Use depthSnapshot (full OB) + depth (incremental updates)
        channels = ["depthSnapshot", "depth"]
        url = self._build_url(exchange, symbol_upper, date_str, channels)

        logger.info(f"Downloading {symbol_upper} {date_str} from Tardis.dev ({exchange})...")

        try:
            resp = self.session.get(url, stream=True, timeout=30)
            if resp.status_code == 401:
                logger.error("Tardis API authentication failed. Set TARDIS_API_KEY env var.")
                return None
            if resp.status_code == 429:
                logger.error("Tardis API rate limit exceeded. Free tier: 10GB/month.")
                return None
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Tardis API request failed: {e}")
            return None

        records = []
        line_count = 0

        for line in resp.iter_lines():
            if not line:
                continue
            line_count += 1

            try:
                # Tardis NDJSON format: ISO_TIMESTAMP\t{JSON_PAYLOAD}
                decoded = line.decode("utf-8")
                tab_idx = decoded.index("\t")
                payload = json.loads(decoded[tab_idx + 1:])
            except (ValueError, json.JSONDecodeError) as e:
                if line_count <= 3:
                    logger.warning(f"Skipping malformed line {line_count}: {e}")
                continue

            stream = payload.get("stream", "")
            data = payload.get("data", {})

            if "depthSnapshot" in stream:
                # Full orderbook snapshot
                ts = data.get("E", 0)  # Event timestamp in ms
                for price_str, qty_str in data.get("bids", []):
                    records.append([ts, 3, float(price_str), float(qty_str)])
                for price_str, qty_str in data.get("asks", []):
                    records.append([ts, 4, float(price_str), float(qty_str)])

            elif "depth" in stream and "depthSnapshot" not in stream:
                # Incremental depth update
                ts = data.get("E", 0)
                for price_str, qty_str in data.get("b", []):
                    records.append([ts, 0, float(price_str), float(qty_str)])
                for price_str, qty_str in data.get("a", []):
                    records.append([ts, 1, float(price_str), float(qty_str)])

        if not records:
            logger.warning(f"No depth records found for {symbol_upper} {date_str}")
            return None

        df = pd.DataFrame(records, columns=["timestamp", "side", "price", "quantity"])
        df = df.sort_values("timestamp").reset_index(drop=True)

        df.to_parquet(str(out_file), engine="pyarrow", compression="snappy", index=False)
        logger.info(f"Saved {len(df)} records -> {out_file.name}")

        return str(out_file)

    def download_range(self, symbol: str, start_date: str, end_date: str,
                       market_type: str = "um_futures",
                       delay_sec: float = 1.0) -> list[str]:
        """Download a date range of L2 depth data."""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        results = []
        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            path = self.download_day(symbol, date_str, market_type)
            if path:
                results.append(path)
            current += timedelta(days=1)
            time.sleep(delay_sec)  # Respect rate limits

        return results
