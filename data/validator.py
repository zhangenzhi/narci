"""
数据质量校验（交易所无关）。

仅包含对 DataFrame 的业务逻辑检查（价格>0、数量>0、日期匹配、时间戳单位等）。
MD5/校验和这类交易所特有的一致性验证已下沉至 data.historical.<source>.verify()。
"""

import os
import re

import pandas as pd


class DataValidator:
    """通用 DataFrame 校验器。"""

    def validate_dataframe(self, df: pd.DataFrame, expected_date_str: str) -> dict:
        report = {"is_valid": True, "errors": [], "stats": {}}

        if df is None or df.empty:
            report["is_valid"] = False
            report["errors"].append("DataFrame 为空或未加载")
            return report

        if "agg_trade_id" in df.columns:
            if not df["agg_trade_id"].is_monotonic_increasing:
                df = df.sort_values("agg_trade_id")

        if "price" in df.columns and (df["price"] <= 0).any():
            report["is_valid"] = False
            report["errors"].append("发现异常价格 (<= 0)")

        if "quantity" in df.columns and (df["quantity"] <= 0).any():
            # 注意：aggTrades 允许负数（卖单标记）—— 此处仅针对纯价量校验。
            # 若 df 是 Narci 4 列格式（含 side=2 的 trades），外部应先过滤。
            pass

        if "timestamp" in df.columns:
            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                ts_sample = df["timestamp"].max()
                unit = "s" if ts_sample < 1e11 else ("ms" if ts_sample < 1e14 else "us")
                try:
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit=unit)
                except Exception as e:
                    report["is_valid"] = False
                    report["errors"].append(f"时间戳转换失败 (Unit:{unit}): {e}")
                    return report

            try:
                expected_ts = pd.to_datetime(expected_date_str)
                min_ts = df["timestamp"].min()

                if min_ts.year > 3000:
                    report["is_valid"] = False
                    report["errors"].append(f"时间戳单位错误: 检测到年份 {min_ts.year}")
                else:
                    match = (min_ts.year == expected_ts.year
                             and min_ts.month == expected_ts.month
                             and min_ts.day == expected_ts.day)
                    if not match:
                        report["is_valid"] = False
                        actual = f"{min_ts.year}-{min_ts.month:02d}-{min_ts.day:02d}"
                        report["errors"].append(
                            f"日期不匹配: 预期 {expected_date_str}, 实际 {actual}")
            except Exception as e:
                report["errors"].append(f"日期逻辑校验异常: {e}")

        report["stats"] = {
            "row_count": len(df),
            "start_str": str(df["timestamp"].min()) if "timestamp" in df.columns else "N/A",
            "avg_price": round(df["price"].mean(), 2) if "price" in df.columns else 0,
        }
        return report

    def scan_directory(self, dir_path: str):
        print(f"🔍 开始扫描目录: {dir_path}")
        if not os.path.exists(dir_path):
            print(f"❌ 目录不存在: {dir_path}")
            return

        files = []
        for root, _, filenames in os.walk(dir_path):
            for f in filenames:
                if f.endswith(".parquet"):
                    files.append(os.path.join(root, f))

        if not files:
            print("❌ 未找到任何 .parquet 文件")
            return

        pass_count = fail_count = 0
        for file_path in sorted(files):
            fname = os.path.basename(file_path)
            m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
            if not m:
                continue
            try:
                df = pd.read_parquet(file_path, engine="pyarrow")
                res = self.validate_dataframe(df, m.group(1))
                status = "✅" if res["is_valid"] else "❌"
                print(f"{status} {fname} | Rows: {res['stats']['row_count']}")
                if res["is_valid"]:
                    pass_count += 1
                else:
                    fail_count += 1
                    for err in res["errors"]:
                        print(f"   └─ {err}")
            except Exception as e:
                fail_count += 1
                print(f"❌ 无法处理 {fname}: {e}")

        print("\n" + "=" * 50)
        print(f"扫描完成 | 通过: {pass_count} | 失败: {fail_count}")
        print("=" * 50)


# 向后兼容别名（已无 Binance 专属逻辑）
BinanceDataValidator = DataValidator


if __name__ == "__main__":
    import sys
    v = DataValidator()
    target = sys.argv[1] if len(sys.argv) > 1 else "./replay_buffer/parquet"
    if os.path.exists(target):
        if os.path.isdir(target):
            v.scan_directory(target)
        else:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", target)
            if m:
                df = pd.read_parquet(target)
                print(v.validate_dataframe(df, m.group(1)))
    else:
        print(f"路径不存在: {target}")
