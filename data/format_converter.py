"""
Format converter: merges Tardis L2 depth data + Binance Vision aggTrades
into Narci's standard 4-column RAW parquet format.

Input sources:
  1. Tardis depth files: SYMBOL_TARDIS_YYYYMMDD.parquet
     Already in [timestamp, side, price, quantity] format (sides 0,1,3,4)
  2. Binance Vision aggTrades: SYMBOL-YYYY-MM-DD.parquet
     Columns: agg_trade_id, price, quantity, first_trade_id, last_trade_id,
              timestamp, is_buyer_maker, is_best_match

Output:
  Narci RAW file: SYMBOL_RAW_YYYYMMDD_000000.parquet
  Columns: [timestamp(ms), side, price, quantity]
  Side encoding:
    0 = bid update, 1 = ask update, 2 = aggTrade,
    3 = bid snapshot, 4 = ask snapshot
"""

import os
import re
import logging
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


class FormatConverter:
    """
    Merges Tardis depth data and Binance Vision aggTrades into unified
    Narci RAW parquet files that can be processed by nyx's L2Reconstructor.
    """

    def __init__(self,
                 tardis_dir: str = "./replay_buffer/tardis",
                 aggtrades_dir: str = "./replay_buffer/parquet",
                 output_dir: str = "./replay_buffer/merged/um_futures/l2"):
        self.tardis_dir = Path(tardis_dir)
        self.aggtrades_dir = Path(aggtrades_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _find_tardis_file(self, symbol: str, date_str: str) -> Path | None:
        """Find Tardis depth file for a given symbol and date."""
        date_compact = date_str.replace("-", "")
        fname = f"{symbol.upper()}_TARDIS_{date_compact}.parquet"
        path = self.tardis_dir / fname
        return path if path.exists() else None

    def _find_aggtrades_file(self, symbol: str, date_str: str,
                             market_type: str = "um_futures") -> Path | None:
        """Find Binance Vision aggTrades parquet file."""
        fname = f"{symbol.upper()}-{date_str}.parquet"
        path = self.aggtrades_dir / market_type / "aggTrades" / symbol.upper() / fname
        return path if path.exists() else None

    def _convert_aggtrades(self, path: Path) -> pd.DataFrame:
        """
        Convert Binance Vision aggTrades parquet to Narci's 4-column format.

        aggTrades columns: agg_trade_id, price, quantity, first_trade_id,
                          last_trade_id, timestamp, is_buyer_maker, is_best_match
        """
        df = pd.read_parquet(str(path))
        if df.empty:
            return pd.DataFrame(columns=["timestamp", "side", "price", "quantity"])

        # Ensure timestamp is in milliseconds (integer)
        if pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            ts = df["timestamp"].astype("int64") // 1_000_000  # ns -> ms
        else:
            ts_sample = df["timestamp"].max()
            if ts_sample < 1e11:
                ts = (df["timestamp"] * 1000).astype("int64")  # s -> ms
            elif ts_sample < 1e14:
                ts = df["timestamp"].astype("int64")  # already ms
            else:
                ts = (df["timestamp"] // 1000).astype("int64")  # us -> ms

        # Narci convention: side=2, qty negative for seller-maker
        qty = df["quantity"].astype(float).copy()
        if "is_buyer_maker" in df.columns:
            mask = df["is_buyer_maker"].astype(bool)
            qty.loc[mask] = -qty.loc[mask]

        result = pd.DataFrame({
            "timestamp": ts,
            "side": 2,
            "price": df["price"].astype(float),
            "quantity": qty,
        })
        return result

    def merge_day(self, symbol: str, date_str: str,
                  market_type: str = "um_futures") -> str | None:
        """
        Merge Tardis depth + Binance aggTrades for one day into a single
        Narci RAW parquet file.

        Returns output file path, or None if no data available.
        """
        symbol_upper = symbol.upper()
        date_compact = date_str.replace("-", "")
        out_file = self.output_dir / f"{symbol_upper}_RAW_{date_compact}_000000.parquet"

        if out_file.exists():
            logger.info(f"[skip] {out_file.name} already exists")
            return str(out_file)

        frames = []

        # 1. Tardis depth data
        tardis_path = self._find_tardis_file(symbol, date_str)
        if tardis_path:
            df_depth = pd.read_parquet(str(tardis_path))
            if not df_depth.empty:
                frames.append(df_depth)
                logger.info(f"  Depth: {len(df_depth)} records from {tardis_path.name}")
        else:
            logger.warning(f"  No Tardis depth file for {symbol_upper} {date_str}")

        # 2. Binance Vision aggTrades
        agg_path = self._find_aggtrades_file(symbol, date_str, market_type)
        if agg_path:
            df_trades = self._convert_aggtrades(agg_path)
            if not df_trades.empty:
                frames.append(df_trades)
                logger.info(f"  Trades: {len(df_trades)} records from {agg_path.name}")
        else:
            logger.warning(f"  No aggTrades file for {symbol_upper} {date_str}")

        if not frames:
            logger.warning(f"No data for {symbol_upper} {date_str}, skipping.")
            return None

        # Merge and sort by timestamp
        df = pd.concat(frames, ignore_index=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Ensure correct dtypes
        df["timestamp"] = df["timestamp"].astype("int64")
        df["side"] = df["side"].astype("int32")
        df["price"] = df["price"].astype("float64")
        df["quantity"] = df["quantity"].astype("float64")

        df.to_parquet(str(out_file), engine="pyarrow", compression="snappy", index=False)
        logger.info(f"Merged {len(df)} records -> {out_file.name}")

        return str(out_file)

    def merge_range(self, symbol: str, start_date: str, end_date: str,
                    market_type: str = "um_futures") -> list[str]:
        """Merge a date range."""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        results = []
        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            path = self.merge_day(symbol, date_str, market_type)
            if path:
                results.append(path)
            current += timedelta(days=1)

        return results

    def scan_and_merge_all(self, market_type: str = "um_futures") -> list[str]:
        """
        Auto-discover all Tardis files, find matching aggTrades,
        and merge everything.
        """
        pattern = re.compile(r"([A-Z0-9]+)_TARDIS_(\d{8})\.parquet")
        results = []

        for f in sorted(self.tardis_dir.glob("*_TARDIS_*.parquet")):
            m = pattern.match(f.name)
            if not m:
                continue
            symbol = m.group(1)
            date_compact = m.group(2)
            date_str = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:8]}"

            logger.info(f"Processing {symbol} {date_str}...")
            path = self.merge_day(symbol, date_str, market_type)
            if path:
                results.append(path)

        return results
