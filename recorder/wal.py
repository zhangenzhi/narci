"""
段式 WAL(write-ahead log)—— 录制器的崩溃安全落盘底座。

动机
----
原录制器每 ``save_interval_sec``(线上 600s)把内存 buffer 一次性写成
parquet。硬崩溃(OOM / SIGKILL / 掉电)会丢掉每 symbol 最多一个 interval
(10 分钟)的数据;且 ``to_parquet`` 写到一半崩溃会留下损坏 shard。

本模块把落盘拆成两级:
  1. **micro-flush**:每隔几秒(或每 K 事件)把内存 micro-buffer 原子写成
     一个**完整、独立可读**的 parquet 段。崩溃最多丢最后一个未 flush 的
     micro-buffer(~秒级)。
  2. **合并**:录制器每 ``save_interval`` 把本 symbol 的所有段 harvest 成
     一个规范的 ``{SYMBOL}_RAW_*.parquet`` 后删段 —— RAW 输出格式与文件数
     都不变,段只是临时实现细节。

隐形性
------
段文件用扩展名 ``.segwal``(内容仍是 parquet,``pd.read_parquet`` 按内容
读,与扩展名无关),并默认放在与 ``realtime/`` 平级的 ``wal/`` 树。所有扫
``*.parquet`` / ``*_RAW_*`` 的消费方(GUI ``os.walk``、daily_compactor
glob、main.py 正则)都看不到它。

崩溃恢复
--------
段都是完整 parquet,进程重启时 :func:`recover_orphans` 把上次残留的段合并
成 RAW 交还,零数据丢失。
"""

import os
import time
from datetime import datetime

import pandas as pd

# 原子写统一在 data/_io;此处 re-export 以不改既有 import
# (l2_recorder 从 data.wal 导入 write_parquet_atomic)。
from core.io import write_parquet_atomic, load_parquet

DEFAULT_COLUMNS = ["timestamp", "side", "price", "quantity"]
SEGMENT_SUFFIX = ".segwal"


class SegmentWAL:
    """单个 symbol 的段式 WAL。

    线程模型:假定在单个 asyncio 事件循环内串行调用(append/flush/harvest/
    clear 之间无并发)。``flush`` 的实际 parquet 写可由调用方丢到
    ``asyncio.to_thread`` 执行。
    """

    def __init__(self, wal_dir: str, symbol: str,
                 columns: list[str] | None = None, *, fsync: bool = True):
        self.wal_dir = wal_dir
        self.symbol = symbol.upper()
        self.columns = columns or DEFAULT_COLUMNS
        self.fsync = fsync
        self._buffer: list = []
        # 续接已存在段的编号,避免重启后覆盖未被恢复的残留段。
        self._seq = self._next_seq()

    # ----------------------------- 写入 ----------------------------- #

    def append(self, records) -> None:
        """把若干 ``[ts, side, price, qty]`` 记录追加进内存 micro-buffer。"""
        self._buffer.extend(records)

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def flush(self) -> str | None:
        """把当前 micro-buffer 原子写成一个段。空则返回 None。"""
        if not self._buffer:
            return None
        data = self._buffer
        self._buffer = []
        os.makedirs(self.wal_dir, exist_ok=True)
        path = os.path.join(
            self.wal_dir, f"{self.symbol}_{self._seq:06d}{SEGMENT_SUFFIX}"
        )
        df = pd.DataFrame(data, columns=self.columns)
        write_parquet_atomic(df, path, fsync=self.fsync)
        self._seq += 1
        return path

    # --------------------------- 合并 / 清理 -------------------------- #

    def segment_paths(self) -> list[str]:
        """本 symbol 当前在盘的段,按编号升序(= 时间序)。"""
        return _segment_paths(self.wal_dir, self.symbol)

    def harvest(self) -> tuple[pd.DataFrame | None, list[str]]:
        """读回本 symbol 所有段并按序 concat。

        返回 ``(df, paths)``;``df`` 为 None 表示无段。``paths`` 是被读取的
        确切段路径,调用方应在成功写出 RAW 后传给 :meth:`discard` 删除 ——
        显式传 paths 避免误删 harvest 之后新 flush 出的段。
        """
        paths = self.segment_paths()
        if not paths:
            return None, []
        frames = [load_parquet(p) for p in paths]
        df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        return df, paths

    def discard(self, paths: list[str]) -> None:
        for p in paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def _next_seq(self) -> int:
        existing = self.segment_paths()
        if not existing:
            return 0
        last = os.path.basename(existing[-1])[len(self.symbol) + 1:-len(SEGMENT_SUFFIX)]
        try:
            return int(last) + 1
        except ValueError:
            return len(existing)


# ------------------------------------------------------------------ #
# 模块级辅助
# ------------------------------------------------------------------ #

def _segment_paths(wal_dir: str, symbol: str) -> list[str]:
    if not os.path.isdir(wal_dir):
        return []
    prefix = f"{symbol.upper()}_"
    names = [
        f for f in os.listdir(wal_dir)
        if f.startswith(prefix) and f.endswith(SEGMENT_SUFFIX)
    ]
    return [os.path.join(wal_dir, f) for f in sorted(names)]


def discovered_symbols(wal_dir: str) -> list[str]:
    """扫 wal_dir,返回有残留段的 symbol 列表(大写)。"""
    if not os.path.isdir(wal_dir):
        return []
    syms = set()
    for f in os.listdir(wal_dir):
        if f.endswith(SEGMENT_SUFFIX) and "_" in f:
            syms.add(f.rsplit("_", 1)[0].upper())
    return sorted(syms)


def recover_orphans(wal_dir: str, raw_dir: str, *, fsync: bool = True) -> list[str]:
    """把上次进程残留的段合并成规范 RAW 文件,交还后删段。

    每个有残留段的 symbol:按序读所有段 → concat → 原子写
    ``{SYMBOL}_RAW_{YYYYMMDD}_{HHMMSS}.parquet`` 到 ``raw_dir`` → 删段。
    返回写出的 RAW 路径列表(用于日志)。

    注意:RAW 文件名时间戳用恢复时刻(与原 ``_flush_all_buffers`` 关停落盘
    一致);文件内事件携带真实 ts,daily_compactor 按文件名日期 glob —— 与
    现有"interval 跨天文件以收尾时刻命名"的既有行为一致,不在 P1 处理跨午夜
    边界细节。
    """
    recovered = []
    for sym in discovered_symbols(wal_dir):
        paths = _segment_paths(wal_dir, sym)
        if not paths:
            continue
        frames = [load_parquet(p) for p in paths]
        df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = os.path.join(raw_dir, f"{sym}_RAW_{ts_str}.parquet")
        os.makedirs(raw_dir, exist_ok=True)
        write_parquet_atomic(df, raw_path, fsync=fsync)
        for p in paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        recovered.append(raw_path)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ♻️ WAL 恢复 {sym}: "
              f"{len(paths)} 段 → {os.path.basename(raw_path)} ({len(df)} 行)")
    return recovered
