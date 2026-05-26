import os
import re
from datetime import datetime

def get_all_parquet_files(directory):
    """递归获取目录下所有 parquet 文件及其完整路径"""
    if not os.path.exists(directory):
        return []
    
    parquet_files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            if filename.endswith(".parquet"):
                parquet_files.append(os.path.join(root, filename))
    return parquet_files

def get_filtered_files(directory, start_date, end_date, symbol=None):
    """根据日期范围和币种筛选 Parquet 文件"""
    all_paths = get_all_parquet_files(directory)
    filtered = []
    
    for f_path in all_paths:
        filename = os.path.basename(f_path)
        
        # 1. 币种过滤 (如果指定了 symbol)
        if symbol and symbol.upper() not in filename.upper() and symbol.upper() not in f_path.upper():
            continue
            
        try:
            # 2. 日期过滤: 
            # 匹配 YYYY-MM-DD (L1) 或 YYYYMMDD (L2 RAW)
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
            if date_match:
                file_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
            else:
                # 尝试匹配无横线的格式 20260222
                date_match_raw = re.search(r'(\d{8})', filename)
                if date_match_raw:
                    file_date = datetime.strptime(date_match_raw.group(1), "%Y%m%d").date()
                else:
                    continue

            if start_date <= file_date <= end_date:
                filtered.append(f_path)
        except Exception:
            continue
            
    # 按文件名排序，确保回测时间顺序
    return sorted(filtered)