"""
日级小文件聚合 + 可选交叉校验（交易所无关）。

工作流:
  1. compact_small_files: 把一天内所有 RAW parquet 碎片合并为一个 DAILY 大文件
  2. validate_data (可选): 通过 HistoricalSource 拉官方对比数据做 L1 对账
  3. archive_to_cold: 复制到冷数据目录（供 rclone 同步）
  4. cleanup_old_fragments: 删除超龄小文件

相比原实现：
  - 不再硬编码 Binance Vision URL
  - 通过 HistoricalSource 注入交叉校验源；Coincheck 等无官方归档的交易所自动跳过
"""

import glob
import logging
import os
import shutil
from datetime import datetime, timedelta

import pandas as pd
import pyarrow.dataset as ds

from data.historical import HistoricalSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DailyCompactor")


class DailyCompactor:
    def __init__(self, symbol: str, target_date,
                 raw_dir: str, official_dir: str,
                 cold_dir: str | None = None,
                 retain_days: int = 7,
                 source: HistoricalSource | None = None,
                 market_type: str = "um_futures",
                 exchange: str | None = None):
        """
        :param symbol: 交易对，如 ETHUSDT / eth_jpy
        :param target_date: datetime.date
        :param raw_dir: RAW 1min 碎片目录
        :param official_dir: 拉取的官方对比文件落脚点
        :param cold_dir: 冷数据归档根目录（None 则不归档）
        :param retain_days: 聚合后保留最近 N 天碎片
        :param source: 交叉校验用的 HistoricalSource（None 则跳过校验）
        :param market_type: "spot" | "um_futures"，供 source 构造 URL 用
        :param exchange: "binance" | "binance_jp" | "coincheck"。若提供，
            cold tier 写入 `{cold_dir}/{exchange}/{market_type}/` 子目录
            （和 backfill_vision_trades.py 的 layout 对齐，避免不同源同符号
            collision，例如 binance.com 全球 BTCJPY 跟 binance.jp BTCJPY）。
            若为 None 则写 flat `cold_dir/`（向后兼容旧 layout）。
        """
        self.symbol = symbol.upper()
        self.target_date = target_date
        self.date_str = target_date.strftime("%Y%m%d")
        self.date_str_dash = target_date.strftime("%Y-%m-%d")

        self.raw_dir = raw_dir
        self.official_dir = official_dir
        self.retain_days = retain_days
        self.source = source
        self.market_type = market_type
        self.exchange = exchange

        # cold tier 子目录路径：cold/{exchange}/{market_type}/
        if cold_dir and exchange:
            self.cold_dir = os.path.join(cold_dir, exchange, market_type)
        else:
            self.cold_dir = cold_dir

        self.daily_file_path = os.path.join(
            self.raw_dir, f"{self.symbol}_RAW_{self.date_str}_DAILY.parquet"
        )

        os.makedirs(self.official_dir, exist_ok=True)
        if self.cold_dir:
            os.makedirs(self.cold_dir, exist_ok=True)

    def compact_small_files(self) -> bool:
        logger.info(f"[{self.symbol}] 聚合 {self.date_str} 的 1min 碎片...")
        pattern = os.path.join(self.raw_dir, f"{self.symbol}_RAW_{self.date_str}_*.parquet")
        files = [f for f in glob.glob(pattern) if "DAILY" not in f]
        if not files:
            logger.warning(f"[{self.symbol}] 未找到 {self.date_str} 的任何录制文件")
            return False

        logger.info(f"[{self.symbol}] 合并 {len(files)} 个切片 (PyArrow Dataset)")
        table = ds.dataset(files, format="parquet").to_table()
        df = table.to_pandas()
        df = df.sort_values(by=["timestamp", "side"], ascending=[True, False]).reset_index(drop=True)
        df.to_parquet(self.daily_file_path, index=False)
        logger.info(f"✅ 聚合完成: {len(df):,} 行 -> {self.daily_file_path}")
        return True

    def validate_data(self) -> bool:
        """
        若配置了 HistoricalSource 且其支持 aggTrades，拉取官方 L1 做交叉比对。
        返回 True = 已执行（成功或失败），False = 跳过。
        """
        if self.source is None:
            logger.info("未配置 HistoricalSource，跳过交叉校验")
            return False
        if not self.source.supports("aggTrades", self.market_type):
            logger.info(f"source={self.source.name} 不支持 {self.market_type}/aggTrades，跳过校验")
            return False
        if not os.path.exists(self.daily_file_path):
            logger.error("DAILY 文件不存在，无法校验")
            return False

        official_path = self.source.download_day(
            self.symbol, self.date_str_dash, "aggTrades",
            self.market_type, self.official_dir)
        if official_path is None:
            logger.warning("官方对比文件下载失败，跳过校验")
            return False

        logger.info(f"🔍 交叉验证 {self.symbol} {self.date_str_dash}")

        df_l2 = pd.read_parquet(self.daily_file_path)
        df_local = df_l2[df_l2["side"] == 2].copy()
        df_local["quantity"] = df_local["quantity"].abs()

        df_official = pd.read_parquet(official_path)

        local_count, local_vol = len(df_local), df_local["quantity"].sum()
        official_count, official_vol = len(df_official), df_official["quantity"].sum()
        count_diff = local_count - official_count
        vol_diff = local_vol - official_vol
        vol_diff_pct = (vol_diff / official_vol * 100) if official_vol > 0 else 0

        logger.info("-" * 40)
        logger.info(f"📊 【{self.symbol} {self.date_str_dash}】 L1 交叉校验")
        logger.info(f"▶ 官方:  {official_count:,} 笔 / 总量 {official_vol:.4f}")
        logger.info(f"▶ 本地:  {local_count:,} 笔 / 总量 {local_vol:.4f}")
        logger.info(f"▶ 笔数差 {count_diff:,} | 体积差 {vol_diff:.4f} ({vol_diff_pct:.4f}%)")
        if count_diff == 0 and abs(vol_diff) < 1e-5:
            logger.info("🎉 完美匹配")
        else:
            logger.warning("⚠️ 存在差异（可能断线/漏单）")
        logger.info("-" * 40)
        return True

    def archive_to_cold(self):
        if not self.cold_dir or not os.path.exists(self.daily_file_path):
            return
        dest = os.path.join(self.cold_dir, os.path.basename(self.daily_file_path))
        if os.path.exists(dest):
            logger.info(f"冷数据已存在，跳过: {dest}")
            return
        shutil.copy2(self.daily_file_path, dest)
        logger.info(f"📦 已归档到 {dest}")

    def cleanup_old_fragments(self):
        cutoff = datetime.now().date() - timedelta(days=self.retain_days)
        if self.target_date >= cutoff:
            return
        if not os.path.exists(self.daily_file_path):
            logger.warning("DAILY 文件不存在，跳过碎片清理")
            return

        pattern = os.path.join(self.raw_dir, f"{self.symbol}_RAW_{self.date_str}_*.parquet")
        fragments = [f for f in glob.glob(pattern) if "DAILY" not in f]
        if fragments:
            for f in fragments:
                os.remove(f)
            logger.info(f"🧹 已清理 {len(fragments)} 个碎片（超 {self.retain_days} 天）")

    def run(self):
        if self.compact_small_files():
            self.validate_data()
            self.archive_to_cold()
            self.cleanup_old_fragments()
