import os
import hashlib
import pandas as pd
import numpy as np
import requests
from datetime import datetime
import glob
import re

class BinanceDataValidator:
    """
    专门用于币安历史数据(aggTrades)校验的工具类
    """
    
    @staticmethod
    def calculate_md5(file_path):
        """计算本地文件的 MD5 哈希值"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def verify_checksum(self, local_file_path, symbol, date_str, file_type="aggTrades"):
        """
        通过币安官方提供的 .CHECKSUM 文件校验本地 ZIP 文件的完整性
        """
        checksum_url = f"https://data.binance.vision/data/spot/daily/{file_type}/{symbol}/{symbol}-{file_type}-{date_str}.zip.CHECKSUM"
        
        try:
            response = requests.get(checksum_url, timeout=10)
            if response.status_code != 200:
                return False, "无法获取远程校验和文件"
            
            # 币安的校验和文件内容通常格式为: "hash  filename"
            remote_checksum = response.text.split()[0].strip()
            local_checksum = self.calculate_md5(local_file_path)
            
            if remote_checksum == local_checksum:
                return True, "MD5 匹配成功"
            else:
                return False, f"MD5 不匹配: 预期 {remote_checksum}, 实际 {local_checksum}"
        except Exception as e:
            return False, f"校验过程出错: {str(e)}"

    def validate_dataframe(self, df, expected_date_str):
        """
        对处理后的 DataFrame 进行业务逻辑校验
        """
        report = {
            "is_valid": True,
            "errors": [],
            "stats": {}
        }

        if df is None or df.empty:
            report["is_valid"] = False
            report["errors"].append("DataFrame 为空或未加载")
            return report

        # 1. 检查 agg_trade_id 是否连续 (核心校验)
        if 'agg_trade_id' in df.columns:
            # 确保按 ID 排序
            df = df.sort_values('agg_trade_id')
            id_diff = df['agg_trade_id'].diff().dropna()
            gaps = id_diff[id_diff != 1]
            if not gaps.empty:
                report["is_valid"] = False
                total_missing = int(gaps.sum() - len(gaps))
                report["errors"].append(f"发现数据断层: {len(gaps)} 处不连续，累计缺失约 {total_missing} 条记录")

        # 2. 检查价格和数量是否合法
        if (df['price'] <= 0).any():
            report["is_valid"] = False
            report["errors"].append("发现异常价格 (<= 0)")
        
        if (df['quantity'] <= 0).any():
            report["is_valid"] = False
            report["errors"].append("发现异常成交量 (<= 0)")

        # 3. 检查时间戳范围
        if 'timestamp' in df.columns:
            # 如果不是时间类型，尝试转换
            if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
                # 启发式转换：检查第一个值的大小
                first_ts = df['timestamp'].iloc[0]
                # 币安 2020年后毫秒量级通常是 13 位数字
                unit = 'ms' if first_ts > 1e11 else 's'
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit=unit)
            
            # 使用 pd.Timestamp 属性比较，避开系统 datetime 库限制
            expected_ts = pd.to_datetime(expected_date_str)
            min_ts = df['timestamp'].min()
            max_ts = df['timestamp'].max()
            
            # 校验年份是否超出了人类常识 (公元 3000 年)
            if min_ts.year > 3000:
                report["is_valid"] = False
                actual_year = min_ts.year
                report["errors"].append(f"时间戳单位错误: 检测到年份为 {actual_year}。请检查 download.py 是否错误地将毫秒识别为了秒。")
            else:
                # 正常日期范围校验
                match_min = (min_ts.year == expected_ts.year and 
                             min_ts.month == expected_ts.month and 
                             min_ts.day == expected_ts.day)
                
                match_max = (max_ts.year == expected_ts.year and 
                             max_ts.month == expected_ts.month and 
                             max_ts.day == expected_ts.day)
                
                if not (match_min and match_max):
                    report["is_valid"] = False
                    actual_start = f"{min_ts.year}-{min_ts.month:02d}-{min_ts.day:02d}"
                    actual_end = f"{max_ts.year}-{max_ts.month:02d}-{max_ts.day:02d}"
                    report["errors"].append(f"日期越界: 预期 {expected_date_str}, 实际区间 {actual_start} 至 {actual_end}")

        # 收集统计信息
        report["stats"] = {
            "row_count": len(df),
            "start_str": f"{min_ts.year}-{min_ts.month:02d}-{min_ts.day:02d} {min_ts.hour:02d}:{min_ts.minute:02d}" if 'timestamp' in df.columns else None,
            "avg_price": round(df['price'].mean(), 2) if not df.empty else 0
        }

        return report

    def scan_directory(self, dir_path):
        """
        扫描目录下的所有 parquet 文件并进行校验
        """
        print(f"🔍 开始扫描目录: {dir_path}")
        if not os.path.exists(dir_path):
            print(f"❌ 目录不存在: {dir_path}")
            return

        files = [f for f in os.listdir(dir_path) if f.endswith('.parquet')]
        
        if not files:
            print("❌ 未找到任何 .parquet 文件")
            return

        summary = []
        for file_name in sorted(files):
            file_path = os.path.join(dir_path, file_name)
            
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', file_name)
            if not date_match:
                print(f"⚠️ 跳过无法识别日期的文件: {file_name}")
                continue
            
            date_str = date_match.group(1)
            try:
                df = pd.read_parquet(file_path, engine='pyarrow')
                res = self.validate_dataframe(df, date_str)
                
                status = "✅ 通过" if res["is_valid"] else "❌ 失败"
                print(f"{status} | 文件: {file_name} | 行数: {res['stats']['row_count']}")
                
                if not res["is_valid"]:
                    for err in res["errors"]:
                        print(f"   └─ 警告: {err}")
                
                summary.append({"file": file_name, "valid": res["is_valid"], "stats": res["stats"]})
            except Exception as e:
                print(f"❌ 无法处理文件 {file_name}: {str(e)}")

        print("\n" + "="*50)
        print(f"扫描完成: 总计 {len(summary)} 个文件")
        print(f"通过: {len([s for s in summary if s['valid']])}")
        print(f"失败: {len([s for s in summary if not s['valid']])}")
        print("="*50)

if __name__ == "__main__":
    import sys
    validator = BinanceDataValidator()
    
    default_path = "./data/parquet"
    target = sys.argv[1] if len(sys.argv) > 1 else default_path

    if os.path.exists(target):
        if os.path.isdir(target):
            validator.scan_directory(target)
        else:
            try:
                # 尝试自动从路径中提取预期日期
                date_match = re.search(r'(\d{4}-\d{2}-\d{2})', target)
                expected = sys.argv[2] if len(sys.argv) > 2 else (date_match.group(1) if date_match else None)
                
                if not expected:
                    print("错误: 请提供预期日期 yyyy-mm-dd")
                    sys.exit(1)
                    
                df_test = pd.read_parquet(target, engine='pyarrow')
                res = validator.validate_dataframe(df_test, expected)
                print(f"校验结果: {'✅ 通过' if res['is_valid'] else '❌ 失败'}")
                if not res["is_valid"]: print(f"详情: {res['errors']}")
            except Exception as e:
                print(f"读取失败: {e}")
    else:
        print(f"路径不存在: {target}")