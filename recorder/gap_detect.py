"""Coincheck 式时间 gap 检测(cold-tier 离线)。

Coincheck / bitbank / GMO / bitFlyer 等**无 U/u 序列号**的 venue,录制热路径无法
在带内检测"漏了一段增量"。本模块在 **cold-tier**(离线、数据完整、schema 可自由
演进)上做基于时间的 gap 检测,作为这些 venue 的数据完整度信号(它们也没有
Binance Vision 这类官方归档可交叉校验)。

算法(两个关键正确性点):
  1. **排除 side 3/4 快照**:录制器每 `save_interval`(线上 600s)从内存 book 注入
     一条全量快照(side 3/4)。即便 WS channel 静默死亡、进程仍活,这些注入快照仍每
     ~600s 出现一次 —— 若把它们算进事件流,任何 gap 都 ≤600s,**完全掩盖**真实丢数据。
     故只在**真实 WS 事件**(side 0/1=depth 增量、2=trade)上测相邻间隔。
  2. **按 stream 分别测**(depth vs trade):2026-05-08 incident 即 CC orderbook channel
     死了 15h 但 trade channel 仍在推 —— 合并测会被 trade 填满、漏判;分流测才抓得到。

输出 per-stream 的 gap 区间 + 覆盖率统计。daily_compactor 把它写成 cold-tier sidecar
`{SYMBOL}_GAPS_{YYYYMMDD}.json`(见 build_report)。**不改 RAW/DAILY 的 4 列 schema。**
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict

import pandas as pd

DEPTH_SIDES = (0, 1)        # 增量 bid/ask
TRADE_SIDE = 2
SNAPSHOT_SIDES = (3, 4)     # 录制器注入的全量快照 —— gap 检测必须忽略
DEFAULT_THRESHOLD_SEC = 30.0

# 检测的 stream → 其 side 集合
_STREAMS = {"depth": DEPTH_SIDES, "trade": (TRADE_SIDE,)}


@dataclass
class Gap:
    start_ms: int    # gap 前最后一个事件
    end_ms: int      # gap 后第一个事件
    gap_sec: float


def _gaps_in(ts_sorted: list[int], threshold_ms: float) -> list[Gap]:
    gaps = []
    for a, b in zip(ts_sorted, ts_sorted[1:]):
        d = b - a
        if d > threshold_ms:
            gaps.append(Gap(int(a), int(b), round(d / 1000.0, 3)))
    return gaps


def _stream_stats(ts_sorted: list[int], gaps: list[Gap]) -> dict:
    if len(ts_sorted) < 2:
        return {"n_events": len(ts_sorted), "n_gaps": 0, "max_gap_sec": 0.0,
                "total_gap_sec": 0.0, "span_sec": 0.0, "coverage_pct": 100.0}
    span = (ts_sorted[-1] - ts_sorted[0]) / 1000.0
    total_gap = sum(g.gap_sec for g in gaps)
    # 覆盖率:span 内被 gap 吞掉的时间占比(粗略完整度,非精确 uptime)
    cov = 100.0 * (1 - total_gap / span) if span > 0 else 100.0
    return {"n_events": len(ts_sorted), "n_gaps": len(gaps),
            "max_gap_sec": round(max((g.gap_sec for g in gaps), default=0.0), 3),
            "total_gap_sec": round(total_gap, 3), "span_sec": round(span, 3),
            "coverage_pct": round(cov, 3)}


def detect_gaps(df: pd.DataFrame, threshold_sec: float = DEFAULT_THRESHOLD_SEC) -> dict:
    """在 4 列 DataFrame 上按 stream 检测时间 gap。

    :param df: `[timestamp(ms), side, price, quantity]`
    :param threshold_sec: 相邻事件间隔超过此值即记为 gap
    :return: ``{"depth": {"gaps": [...], "stats": {...}}, "trade": {...}}``
    """
    thr_ms = threshold_sec * 1000.0
    out: dict[str, dict] = {}
    if df is None or df.empty or "side" not in df.columns or "timestamp" not in df.columns:
        for name in _STREAMS:
            out[name] = {"gaps": [], "stats": _stream_stats([], [])}
        return out
    for name, sides in _STREAMS.items():
        ts = df.loc[df["side"].isin(sides), "timestamp"].astype("int64")
        ts_sorted = sorted(ts.tolist())
        gaps = _gaps_in(ts_sorted, thr_ms)
        out[name] = {"gaps": [asdict(g) for g in gaps], "stats": _stream_stats(ts_sorted, gaps)}
    return out


def build_report(symbol: str, date_str: str, df: pd.DataFrame,
                 threshold_sec: float = DEFAULT_THRESHOLD_SEC) -> dict:
    """gap 检测 + 元数据,组成可写盘的 cold-tier sidecar 报告。"""
    streams = detect_gaps(df, threshold_sec)
    total_gaps = sum(len(s["gaps"]) for s in streams.values())
    return {
        "symbol": symbol.upper(),
        "date": date_str,
        "threshold_sec": threshold_sec,
        "generated_at": int(time.time() * 1000),
        "total_gaps": total_gaps,
        "streams": streams,
        "note": "gaps 基于真实 WS 事件(side 0/1/2),已排除注入快照(side 3/4);"
                "depth/trade 分流检测。无 U/u 序列 venue 的离线完整度信号。",
    }
