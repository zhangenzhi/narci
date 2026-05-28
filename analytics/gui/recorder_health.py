"""录制健康数据层(纯函数,供 GUI 面板 + 测试用)。

把"录制器健不健康、数据全不全"拆成三个可扫描的信号,均只读文件系统、无 streamlit
依赖,便于单测:

  1. scan_freshness   —— 每 (venue, market, symbol) 最新 RAW shard 的年龄 → 录制停没停
  2. scan_gap_reports —— cold-tier 的 {SYMBOL}_GAPS_*.json(recorder/gap_detect 产出)
                         → 静默丢没丢(覆盖率 / gap 数 / 最大 gap)
  3. scan_wal_backlog —— replay_buffer/wal/ 的 .segwal 残留 → 落盘卡没卡(段没被合并)

路径约定(P5 后):
  realtime: {base}/replay_buffer/realtime/{exchange}/{market}/l2/{SYMBOL}_RAW_*.parquet
  cold:     {base}/replay_buffer/cold/{exchange}/{market}/{SYMBOL}_GAPS_{date}.json
  wal:      {base}/replay_buffer/wal/{exchange}/{market}/l2/{SYMBOL}_*.segwal
"""
from __future__ import annotations

import json
import os
import re
import time

_RAW_RE = re.compile(r"^(?P<sym>.+)_RAW_\d{8}_\d{6}\.parquet$")
_GAPS_RE = re.compile(r"^(?P<sym>.+)_GAPS_(?P<date>\d{8})\.json$")
_SEGWAL_EXT = ".segwal"
DEFAULT_STALE_SEC = 300


def _venue_market(rel_parts: list[str]) -> tuple[str, str]:
    """从 realtime/ 下的相对路径段解析 (exchange, market)，**正向锚 l2**。

    l2 在 venue 根下位置固定:``{exchange}/{market}/l2/...``,其前两段恒为
    (exchange, market),不管 l2 后是扁平文件还是 ``{symbol}/{date}/`` 分区 —— 故
    本布局与旧扁平布局都兼容(2026-05 symbol/day 分区后仍正确)。symbol 由文件名取。
    """
    if "l2" in rel_parts:
        i = rel_parts.index("l2")
        ex = rel_parts[i - 2] if i >= 2 else "?"
        mkt = rel_parts[i - 1] if i >= 1 else "?"
        return ex, mkt
    return "?", rel_parts[-3] if len(rel_parts) >= 3 else "?"   # 旧无 l2 结构兜底


def scan_freshness(realtime_root: str, now: float | None = None,
                   stale_sec: int = DEFAULT_STALE_SEC) -> list[dict]:
    """每 (exchange, market, symbol) 的最新 RAW shard 年龄 + 状态。"""
    now = time.time() if now is None else now
    if not os.path.isdir(realtime_root):
        return []
    latest: dict[tuple[str, str, str], dict] = {}
    for base, _, files in os.walk(realtime_root):
        for fn in files:
            m = _RAW_RE.match(fn)
            if not m or "DAILY" in fn:
                continue
            rel = os.path.relpath(os.path.join(base, fn), realtime_root).split(os.sep)
            ex, mkt = _venue_market(rel)
            sym = m.group("sym").upper()
            key = (ex, mkt, sym)
            mt = os.path.getmtime(os.path.join(base, fn))
            cur = latest.get(key)
            if cur is None:
                latest[key] = {"exchange": ex, "market": mkt, "symbol": sym,
                               "last_shard_ts": mt, "n_shards": 1}
            else:
                cur["n_shards"] += 1
                if mt > cur["last_shard_ts"]:
                    cur["last_shard_ts"] = mt
    rows = []
    for r in latest.values():
        age = now - r["last_shard_ts"]
        r["age_sec"] = round(age, 1)
        r["status"] = "fresh" if age <= stale_sec else "stale"
        rows.append(r)
    return sorted(rows, key=lambda x: (x["status"] != "stale", x["exchange"], x["market"], x["symbol"]))


def scan_gap_reports(cold_root: str) -> list[dict]:
    """cold-tier {SYMBOL}_GAPS_{date}.json → 扁平化的覆盖率/ gap 摘要(每文件一行)。"""
    if not os.path.isdir(cold_root):
        return []
    rows = []
    for base, _, files in os.walk(cold_root):
        for fn in files:
            m = _GAPS_RE.match(fn)
            if not m:
                continue
            try:
                rep = json.loads(open(os.path.join(base, fn), encoding="utf-8").read())
            except Exception:
                continue
            d = rep.get("streams", {}).get("depth", {}).get("stats", {})
            t = rep.get("streams", {}).get("trade", {}).get("stats", {})
            rows.append({
                "symbol": rep.get("symbol", m.group("sym").upper()),
                "date": rep.get("date") or m.group("date"),
                "total_gaps": rep.get("total_gaps", 0),
                "depth_coverage_pct": d.get("coverage_pct"),
                "depth_n_gaps": d.get("n_gaps"),
                "depth_max_gap_sec": d.get("max_gap_sec"),
                "trade_coverage_pct": t.get("coverage_pct"),
                "trade_n_gaps": t.get("n_gaps"),
                "trade_max_gap_sec": t.get("max_gap_sec"),
            })
    return sorted(rows, key=lambda x: (x["date"], x["symbol"]), reverse=True)


def scan_wal_backlog(wal_root: str) -> list[dict]:
    """每 (exchange, market, symbol) 当前在盘的 .segwal 段数(>0 = 尚未被合并落 RAW)。"""
    if not os.path.isdir(wal_root):
        return []
    counts: dict[tuple[str, str, str], int] = {}
    for base, _, files in os.walk(wal_root):
        for fn in files:
            if not fn.endswith(_SEGWAL_EXT):
                continue
            rel = os.path.relpath(os.path.join(base, fn), wal_root).split(os.sep)
            ex, mkt = _venue_market(rel)
            sym = fn.rsplit("_", 1)[0].upper()  # {SYMBOL}_{seq}.segwal
            counts[(ex, mkt, sym)] = counts.get((ex, mkt, sym), 0) + 1
    return sorted(
        ({"exchange": e, "market": mk, "symbol": s, "pending_segments": n}
         for (e, mk, s), n in counts.items()),
        key=lambda x: -x["pending_segments"],
    )


def health_summary(freshness: list[dict]) -> dict:
    """聚合一句话总览。"""
    stale = [r for r in freshness if r["status"] == "stale"]
    return {
        "n_symbols": len(freshness),
        "n_fresh": len(freshness) - len(stale),
        "n_stale": len(stale),
        "stale": [f"{r['exchange']}/{r['market']}/{r['symbol']}" for r in stale],
        "overall": "RED" if stale else ("GREEN" if freshness else "EMPTY"),
    }
