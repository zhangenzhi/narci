"""派生缓存文件名的单一入口(P2 冗余底座,2026-05-26)。

此前完全相同的 "排序 basename 拼接 → md5[:8]" 逻辑在 main.py、
backtest/backtest.py、data/feature_builder.py 三处各写一遍;改 hash 规则
(如加版本前缀)要追三处。统一在这里。
"""

import hashlib
import os


def compute_cache_hash(raw_file_paths: list[str]) -> str:
    """对一组源文件算稳定的 8 位 hash:排序 basename 拼接后取 md5 前 8 位。

    与历史实现逐字节一致(顺序无关、只看文件名),保证既有缓存命中不变。
    """
    file_names = "".join(sorted(os.path.basename(p) for p in raw_file_paths))
    return hashlib.md5(file_names.encode("utf-8")).hexdigest()[:8]
