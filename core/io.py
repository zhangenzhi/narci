"""parquet 读写的单一入口(P2 冗余底座,2026-05-26)。

此前 ``df.to_parquet(path, engine="pyarrow", compression="snappy",
index=False)`` 与 ``pd.read_parquet(path)`` 散落在 data/ 十余处,改一处
默认值要追十几个文件。这里统一:

  - :func:`save_parquet` — 默认 pyarrow + snappy + 不写 index。
  - :func:`load_parquet` — 直读。
  - :func:`write_parquet_atomic` — 崩溃安全的原子写(.tmp→fsync→
    os.replace→fsync 目录),录制器 WAL 与一切"宁可不写也不要写坏"的
    落盘都用它。

改全局编码/压缩只需改这里一处。
"""

import os

import pandas as pd


def save_parquet(df: pd.DataFrame, path: str, *,
                 compression: str = "snappy", index: bool = False) -> None:
    """统一的 parquet 写(非原子)。默认 pyarrow + snappy + 不写 index。"""
    df.to_parquet(path, engine="pyarrow", compression=compression, index=index)


def load_parquet(path: str, **kwargs) -> pd.DataFrame:
    """统一的 parquet 读。"""
    return pd.read_parquet(path, **kwargs)


def write_parquet_atomic(df: pd.DataFrame, path: str, *,
                         compression: str = "snappy", index: bool = False,
                         fsync: bool = True) -> None:
    """把 DataFrame 原子地写到 ``path``。

    写到同目录的 ``.tmp`` → fsync 文件 → ``os.replace``(同文件系统 rename
    原子)→ fsync 目录(让 rename 本身落盘)。崩溃只会留下可丢弃的 ``.tmp``,
    绝不产生半个有效的目标文件。录制器 WAL 段、RAW、关停 flush 全走它。
    """
    tmp = f"{path}.tmp"
    df.to_parquet(tmp, engine="pyarrow", compression=compression, index=index)
    if fsync:
        # 确保 tmp 内容落盘后再 rename,避免 rename 先于数据持久化。
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    os.replace(tmp, path)
    if fsync:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
