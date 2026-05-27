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
                       parked_sec: int = PARKED_SEC,
                       parked_venues: set[tuple[str | None, str | None]] | None = None) -> list[dict]:
    """用客户端 now + 阈值重算 age/status（容器内 scan 的 status 阈值固定，这里覆盖）。

    status: parked | stale(>阈值) | fresh | unknown。

    parked 判定优先看 **parked_venues**(其 recorder 容器已 exited，故意停的 venue)——
    这比"7 天无 shard"准且即时(否则刚 park 几天的 venue 会被误判 stale 触发 RED,
    如 GMO 5/21 park 后第 6 天)。无容器信息时回退 parked_sec 老 shard 启发式。
    """
    now = time.time() if now is None else now
    parked_venues = parked_venues or set()
    out = []
    for r in rows:
        ts = r.get("last_shard_ts")
        age = None if ts is None else round(now - ts, 1)
        if (r.get("exchange"), r.get("market")) in parked_venues:
            status = "parked"               # 容器已 exited → 故意停,即时豁免
        elif age is None:
            status = "unknown"
        elif age > parked_sec:
            status = "parked"               # 回退:超 7 天无 shard
        elif age > stale_sec:
            status = "stale"
        else:
            status = "fresh"
        out.append({**r, "age_sec": age, "status": status})
    # 排序：stale 置顶（最该看），其后 fresh，最后 parked
    order = {"stale": 0, "unknown": 1, "fresh": 2, "parked": 3}
    return sorted(out, key=lambda r: (order.get(r["status"], 9),
                                      r.get("exchange", ""), r.get("symbol", "")))


# 容器服务名 → 写盘 (exchange, market) 目录的显式映射。
# 真相源 = recorder 实际写到 realtime/{exchange}/{market}/ 的目录名(2026-05-27 实测)。
# 注意 binance-jp 写到 exchange=**binance_jp**(不是 binance) —— "jp" 是 exchange 变体,
# 不是 market;启发式("第一段当 exchange")会错,故走显式表。新增 venue 在此登记。
_VENUE_MAP = {
    "coincheck": ("coincheck", "spot"),
    "binance-jp": ("binance_jp", "spot"),
    "binance-spot": ("binance", "spot"),
    "binance-umfut": ("binance", "um_futures"),
    "bitbank": ("bitbank", "spot"),
    "bitflyer-fx": ("bitflyer", "fx"),
    "bitflyer-spot": ("bitflyer", "spot"),
    "gmo-spot": ("gmo", "spot"),
    "gmo-leverage": ("gmo", "leverage"),
}
_MARKET_ALIAS = {"umfut": "um_futures", "fx": "fx", "leverage": "leverage", "spot": "spot"}


def container_venue(name: str) -> tuple[str | None, str | None]:
    """narci-recorder-<svc> → (exchange, market)；非 recorder(cloud-sync 等)→ (None, None)。

    优先查显式 _VENUE_MAP(已部署 venue 的真相);未登记的走启发式兜底
    (第一段=exchange,末段=market),新 venue 上线时建议补进 _VENUE_MAP。
    """
    if "recorder-" not in name:
        return (None, None)
    base = name.split("recorder-", 1)[1]
    if base in _VENUE_MAP:
        return _VENUE_MAP[base]
    parts = base.split("-")
    mkt = _MARKET_ALIAS.get(parts[-1], parts[-1]) if len(parts) > 1 else "spot"
    return (parts[0], mkt)


def parked_venues(containers: list[dict]) -> set[tuple[str, str]]:
    """recorder 容器 exited 的 venue 集合(故意停的,如 GMO)——freshness 据此即时豁免。"""
    s = set()
    for c in containers:
        if c.get("state") == "exited":
            ex, mkt = container_venue(c.get("name", ""))
            if ex is not None:
                s.add((ex, mkt))
    return s


def tile_health(container: dict, freshness: list[dict]) -> str:
    """单模块色块健康度:ok(绿) | bad(红) | parked(灰) | warn(黄)。

    容器异常直接 bad/灰;容器在跑则看其 venue 的 symbol 有没有 stale(catch 静默死)。
    """
    state = container.get("state")
    if state in ("restarting", "unhealthy"):
        return "bad"
    if state == "exited":
        return "parked"
    if state in ("created", "unknown"):
        return "warn"
    ex, mkt = container_venue(container.get("name", ""))
    if ex is None:                      # cloud-sync / event-publisher：无 venue，仅看 up
        return "ok"
    matched = [f for f in freshness if f.get("exchange") == ex and f.get("market") == mkt]
    if any(f.get("status") == "stale" for f in matched):
        return "bad"
    return "ok"


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
