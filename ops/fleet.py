"""fleet — 探针 stdout → 结构化 dict 的纯解析器（无 IO，可单测）。

probe_aws 把"每 host 一发"的远端 stdout 用 ###SECTION### 分段返回；这里把各段解析成
结构化数据。所有函数纯函数化，fixture = 真实远端输出。

远端 stdout 契约（probe_aws 的远端脚本产出）::

    ###COMMIT###
    <short-sha>
    <subject>
    ###PS###
    <name>|<status>|<image>          # docker compose ps --format
    ###SCAN###
    {"freshness":[...], "wal":[...]}  # 容器内 recorder_health.scan_* 的 JSON
    ###HEALTH###
    port=<p> code=<http_code>
"""
from __future__ import annotations

import json
import re
import time

PARKED_SEC = 7 * 24 * 3600   # 7 天无新 shard → 视为故意停（同 healthcheck.py），不报警
DEFAULT_STALE_SEC = 1500     # RAW 每 save_interval(默认600s)落一次 → 阈值取 ~2.5×

_SECTION_RE = re.compile(r"^###(?P<name>[A-Z]+)###\s*$")


def split_sections(stdout: str) -> dict[str, str]:
    """按 ###NAME### 行切段，返回 {name: 段内容(strip)}。"""
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in stdout.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            cur = m.group("name")
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_commit(text: str) -> dict:
    lines = [l for l in text.splitlines() if l.strip()]
    return {"sha": lines[0].strip() if lines else "?",
            "subject": lines[1].strip() if len(lines) > 1 else ""}


def _container_state(status: str) -> str:
    """从 docker status 文本归类：healthy|unhealthy|up|restarting|exited|created|unknown。"""
    s = status.lower()
    if s.startswith("up"):
        if "(healthy)" in s:
            return "healthy"
        if "(unhealthy)" in s:
            return "unhealthy"
        return "up"            # 无 healthcheck / starting
    if s.startswith("restarting"):
        return "restarting"
    if s.startswith("exited"):
        return "exited"        # 多为 PARKED（如 gmo）
    if s.startswith("created"):
        return "created"
    return "unknown"


def parse_ps(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        name, status = parts[0].strip(), parts[1].strip()
        rows.append({
            "name": name,
            "status": status,
            "state": _container_state(status),
            "image": parts[2].strip() if len(parts) > 2 else "",
        })
    return sorted(rows, key=lambda r: r["name"])


def parse_health(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        m = re.search(r"port=(\d+)\s+code=(\d+|\w+)", line)
        if not m:
            continue
        code = m.group(2)
        rows.append({"port": int(m.group(1)), "code": code, "ok": code == "200"})
    return sorted(rows, key=lambda r: r["port"])


def parse_scan(text: str) -> dict:
    """###SCAN### 段是容器内 scan_freshness/scan_wal_backlog 的 JSON。"""
    try:
        d = json.loads(text)
    except Exception:
        return {"freshness": [], "wal": []}
    return {"freshness": d.get("freshness", []), "wal": d.get("wal", [])}


def classify_freshness(rows: list[dict], now: float | None = None,
                       stale_sec: int = DEFAULT_STALE_SEC,
                       parked_sec: int = PARKED_SEC) -> list[dict]:
    """用客户端 now + 阈值重算 age/status（容器内 scan 的 status 阈值固定，这里覆盖）。

    status: parked(>7d, 故意停, 不算 RED) | stale(>阈值) | fresh。
    """
    now = time.time() if now is None else now
    out = []
    for r in rows:
        ts = r.get("last_shard_ts")
        age = None if ts is None else round(now - ts, 1)
        if age is None:
            status = "unknown"
        elif age > parked_sec:
            status = "parked"
        elif age > stale_sec:
            status = "stale"
        else:
            status = "fresh"
        out.append({**r, "age_sec": age, "status": status})
    # 排序：stale 置顶（最该看），其后 fresh，最后 parked
    order = {"stale": 0, "unknown": 1, "fresh": 2, "parked": 3}
    return sorted(out, key=lambda r: (order.get(r["status"], 9),
                                      r.get("exchange", ""), r.get("symbol", "")))


def summarize(ps: list[dict], freshness: list[dict]) -> dict:
    """一句话总览：RED 触发 = 有 stale venue 或有 restarting/unhealthy 容器。"""
    stale = [r for r in freshness if r["status"] == "stale"]
    bad_ct = [c for c in ps if c["state"] in ("restarting", "unhealthy")]
    up = [c for c in ps if c["state"] in ("healthy", "up")]
    parked = [c for c in ps if c["state"] == "exited"]
    overall = "RED" if (stale or bad_ct) else ("GREEN" if up else "EMPTY")
    return {
        "overall": overall,
        "n_up": len(up), "n_parked": len(parked), "n_bad": len(bad_ct),
        "stale_venues": [f"{r.get('exchange','?')}/{r.get('symbol','?')}" for r in stale],
        "bad_containers": [c["name"] for c in bad_ct],
    }
