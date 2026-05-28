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
    """###SCAN### 段是容器内 scanner 的 JSON(freshness + wal + 当日 coverage bitmap)。"""
    try:
        d = json.loads(text)
    except Exception:
        return {"freshness": [], "wal": [], "coverage": [], "n_buckets": 144}
    return {"freshness": d.get("freshness", []), "wal": d.get("wal", []),
            "coverage": d.get("coverage", []), "n_buckets": d.get("n_buckets", 144)}


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


def parse_cold(stdout: str) -> list[dict]:
    """###COLD### 段每行:ex/mkt|latest|n_daily|n_gaps[|days_csv|bad_csv]。

    后两字段(2026-05 加)为近 120 天 DAILY 日期集合 + .corrupted 损坏集合,
    供 classify_cold_history 算每 venue 近 90 天落地条。缺则视为空(向后兼容)。
    """
    sec = split_sections(stdout).get("COLD", "")
    rows = []
    for line in sec.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4 or "/" not in parts[0]:
            continue
        ex, _, mkt = parts[0].partition("/")
        days = set(d for d in parts[4].split(",") if d) if len(parts) > 4 else set()
        bad = set(d for d in parts[5].split(",") if d) if len(parts) > 5 else set()
        rows.append({"exchange": ex, "market": mkt,
                     "latest_daily": parts[1] or None,
                     "n_daily": int(parts[2]) if parts[2].isdigit() else 0,
                     "n_gaps": int(parts[3]) if parts[3].isdigit() else 0,
                     "days": days, "bad": bad})
    return rows


def classify_cold_history(rows: list[dict], today_yyyymmdd: str,
                          parked_venues: set[tuple[str, str]] | None = None,
                          n_days: int = 90) -> list[dict]:
    """每 venue 近 n_days 天 DAILY 落地 bitmap(oldest→newest,n_days 字符):
       '1' 已落 / '0' 缺(>=2 天前的真 gap)/ '?' lag(今/昨,compact 可能未跑)
       / 'p' parked(venue 容器 exited,缺天豁免)/ 'x' corrupted(.corrupted 文件)。
    """
    import datetime as _dt
    parked_venues = parked_venues or set()
    today = _dt.datetime.strptime(today_yyyymmdd, "%Y%m%d").date()
    out = []
    for r in rows:
        days_set = r.get("days", set())
        bad_set = r.get("bad", set())
        is_parked = (r["exchange"], r["market"]) in parked_venues
        bits = []
        for i in range(n_days):
            offset = n_days - 1 - i              # i=0 → 最早一天, i=n-1 → 今天
            day_str = (today - _dt.timedelta(days=offset)).strftime("%Y%m%d")
            if day_str in bad_set:
                bits.append("x")
            elif day_str in days_set:
                bits.append("1")
            elif is_parked:
                bits.append("p")
            elif offset <= 1:                    # 今天/昨天 missing 算 lag(yellow)
                bits.append("?")
            else:
                bits.append("0")                 # 旧天数 missing 真 gap(red)
        out.append({**r, "history": "".join(bits)})
    return sorted(out, key=lambda r: (r["exchange"], r["market"]))


def classify_cold(rows: list[dict], today_yyyymmdd: str,
                  parked_venues: set[tuple[str, str]] | None = None) -> list[dict]:
    """每 venue cold 落地状态:ok | lag | stale | missing | parked。

    昨日(today-1)是最新可期望的 DAILY(compact 处理上一 UTC 日)。
    behind = (昨日 - latest).days:<=0 ok / ==1 lag(可能今天 compact 未跑) / >=2 stale。
    parked venue(AWS 容器 exited)豁免。
    """
    import datetime as _dt
    parked_venues = parked_venues or set()
    today = _dt.datetime.strptime(today_yyyymmdd, "%Y%m%d").date()
    yesterday = today - _dt.timedelta(days=1)
    out = []
    for r in rows:
        ev = (r["exchange"], r["market"])
        latest = r.get("latest_daily")
        if ev in parked_venues:
            status = "parked"
            behind = None
        elif not latest:
            status, behind = "missing", None
        else:
            d = _dt.datetime.strptime(latest, "%Y%m%d").date()
            behind = (yesterday - d).days
            status = "ok" if behind <= 0 else ("lag" if behind == 1 else "stale")
        out.append({**r, "status": status, "days_behind": behind})
    order = {"stale": 0, "missing": 0, "lag": 1, "ok": 2, "parked": 3}
    return sorted(out, key=lambda r: (order.get(r["status"], 9), r["exchange"], r["market"]))


_CS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+\[cloud-sync\]\s+(syncing|done \(rc=(-?\d+)\))")


def parse_cloudsync(text: str, now: float | None = None) -> dict:
    """###CLOUDSYNC### 段(cloud-sync 容器日志)→ 推送腿健康摘要。

    日志成对:`<ts> [cloud-sync] syncing...` → `<ts> [cloud-sync] done (rc=N)`。
    rc=0 成功 / rc=124 超时(2026-05-21 那类卡死)/ 其它=rclone 错。
    """
    import datetime as _dt
    now = time.time() if now is None else now
    ev = []
    for line in text.splitlines():
        m = _CS_RE.match(line.strip())
        if not m:
            continue
        ts = (_dt.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S")
              .replace(tzinfo=_dt.timezone.utc).timestamp())
        ev.append((ts, "done", int(m.group(3))) if m.group(2).startswith("done")
                  else (ts, "syncing", None))
    cycles, start = [], None
    for ts, kind, rc in ev:
        if kind == "syncing":
            start = ts
        elif start is not None:
            cycles.append({"start": start, "end": ts, "rc": rc, "dur_sec": round(ts - start, 1)})
            start = None
    last = cycles[-1] if cycles else None
    return {
        "n_cycles": len(cycles),
        "last_rc": last["rc"] if last else None,
        "last_done_age": round(now - last["end"], 1) if last else None,
        "last_dur_sec": last["dur_sec"] if last else None,
        "recent_rcs": [c["rc"] for c in cycles[-5:]],
        "in_progress": bool(ev and ev[-1][1] == "syncing"),
    }


# rc=128+N 是 shell 报告"被信号 N 杀"的约定(143=128+15 SIGTERM、137=128+9 SIGKILL):
# cloud-sync 容器 recreate 时 docker 给运行中的 rclone 发 SIGTERM → rc=143。这是
# redeploy/重启的瞬时产物,不是 rclone 错。新容器起来后下一轮会 rc=0。
_SIGNAL_EXIT_RCS = {137, 143}


def cloudsync_health(summ: dict, stale_sec: int = 3600) -> str:
    """推送腿健康:ok | bad(真超时/rclone 错)| stale(太久没成功)| unknown。

    rc=143/137 = 容器 recreate 时 rclone 被 SIGTERM/SIGKILL 杀(redeploy 瞬时),
    若现在正在跑(in_progress)或近 5 轮里有 rc=0,就算 ok —— 不是真故障。
    rc=124 = `timeout` cmd 杀的(rclone 真卡住,2026-05-21 那类)→ bad。
    """
    rc = summ.get("last_rc")
    age = summ.get("last_done_age")
    if rc is None and not summ.get("in_progress"):
        return "unknown"
    if rc == 124:
        return "bad"                              # rclone 真超时卡住
    if rc in _SIGNAL_EXIT_RCS:
        # SIGTERM/SIGKILL:若新容器在跑或近期有成功轮,视为 transient
        if summ.get("in_progress") or 0 in (summ.get("recent_rcs") or []):
            return "ok"
        return "stale"                            # 持续被信号杀且没恢复 → 等下一轮
    if rc not in (0, None):
        return "bad"                              # rclone 其它错(真故障)
    if age is not None and age > stale_sec and not summ.get("in_progress"):
        return "stale"
    return "ok"


def cold_overall(classified: list[dict]) -> str:
    """cold 总体:有 stale/missing → RED;有 lag → AMBER;否则 GREEN/EMPTY。"""
    if not classified:
        return "EMPTY"
    if any(r["status"] in ("stale", "missing") for r in classified):
        return "RED"
    if any(r["status"] == "lag" for r in classified):
        return "AMBER"
    return "GREEN"


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
