"""
轻量级健康检查 HTTP 服务 (Recording Only)
检查项:
  1. recorder 进程是否存活 (最近 5 分钟内是否有新的 parquet 落盘)
  2. 磁盘空间是否充足
  3. trade 流是否在写 (最近 30 分钟内是否有 side=2 事件)
     —— 防止 2026-04-23 那种 aggTrade endpoint 改动导致 trade 流静默死亡
        但 depth 仍流畅、文件 mtime 仍刷新的情况
"""
import os
import re
import signal
import subprocess
import threading
import time
import glob
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

DATA_DIR = "/app/replay_buffer/realtime"
STALE_THRESHOLD_SEC = 300              # 5 分钟无新文件视为不健康
DISK_WARN_PERCENT = 90                 # 磁盘使用超过 90% 告警
TRADE_WINDOW_SEC = 1800                # 30 分钟检测窗口
TRADE_WARMUP_SEC = 1800                # 启动后前 30 分钟不检 trade（让缓冲落盘）
MIN_TRADES_IN_WINDOW = 1               # 30 分钟内至少 1 笔成交
START_FILE = "/tmp/narci_start_time"

# Auto-heal: when /health is unhealthy for AUTOHEAL_AFTER_SEC continuous
# seconds (with trade_silent specifically), kill the recorder process so
# supervisord restarts it. Forces a fresh WS connection, which may clear
# transient stuck states. Won't fix endpoint-level bugs but at least
# bounds outage to AUTOHEAL_AFTER_SEC instead of indefinite silence.
AUTOHEAL_ENABLED = os.environ.get("NARCI_AUTOHEAL", "1") == "1"
AUTOHEAL_AFTER_SEC = 1800              # restart after 30 min of trade_silent
AUTOHEAL_COOLDOWN_SEC = 1800           # don't restart again within 30 min
AUTOHEAL_CHECK_INTERVAL_SEC = 120      # check every 2 min

_SHARD_NAME_RE = re.compile(r"^([A-Za-z0-9_]+?)_RAW_\d{8}_\d{6}\.parquet$")


def _ensure_start_file() -> float:
    if os.path.exists(START_FILE):
        try:
            return float(open(START_FILE).read().strip())
        except Exception:
            pass
    now = time.time()
    try:
        with open(START_FILE, "w") as f:
            f.write(str(now))
    except Exception:
        pass
    return now


def _safe_mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _list_recent_shards(now: float, max_age_sec: int) -> list[tuple[str, float]]:
    """Return [(path, mtime)] for non-DAILY shards modified within max_age_sec.
    Skips files that vanished between glob and stat (race-safe)."""
    cutoff = now - max_age_sec
    out = []
    for path in glob.glob(os.path.join(DATA_DIR, "**", "*_RAW_*.parquet"),
                          recursive=True):
        if "DAILY" in os.path.basename(path):
            continue
        mt = _safe_mtime(path)
        if mt is None:
            continue
        if mt >= cutoff:
            out.append((path, mt))
    return out


def _count_trades_in_files(paths: list[str]) -> int:
    """Sum side=2 rows across the given parquet files. Returns 0 on
    error (caller treats as 'unknown')."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return -1   # pyarrow missing, can't check
    total = 0
    for p in paths:
        try:
            tbl = pq.read_table(p, columns=["side"],
                                 filters=[("side", "=", 2)])
            total += tbl.num_rows
        except Exception:
            # one bad file shouldn't fail the whole check
            continue
    return total


def check_trade_rate(now: float, recent_shards: list[tuple[str, float]],
                     warmup_done: bool) -> tuple[list[str], dict]:
    """Verify at least one trade event landed in the last TRADE_WINDOW_SEC.
    During warmup (first 30 min after recorder start), skip the check —
    the recorder buffers events in memory and only flushes every
    save_interval, so a brand-new container hasn't written trade rows
    even if the WS is healthy."""
    info = {}
    if not warmup_done:
        info["trade_check"] = "warmup"
        return [], info

    in_window = [(p, mt) for p, mt in recent_shards
                 if mt >= now - TRADE_WINDOW_SEC]
    if not in_window:
        # No shards in window — file-mtime check above will catch this.
        info["trade_check"] = "no_shards_in_window"
        return [], info

    # Group by symbol so we can also flag per-symbol when handy.
    n_trades = _count_trades_in_files([p for p, _ in in_window])
    if n_trades < 0:
        info["trade_check"] = "pyarrow_unavailable"
        return [], info

    info["trades_in_30min"] = n_trades
    info["shards_in_window"] = len(in_window)

    if n_trades < MIN_TRADES_IN_WINDOW:
        return [
            f"trade_silent: 0 trades in last {TRADE_WINDOW_SEC//60}min "
            f"across {len(in_window)} shards "
            "(WS depth flowing? aggTrade endpoint changed?)"
        ], info
    return [], info


def check_health():
    issues = []
    info = {}
    now = time.time()
    start_time = _ensure_start_file()
    uptime = now - start_time
    info["uptime_sec"] = int(uptime)
    warmup_done = uptime >= TRADE_WARMUP_SEC

    # 1. 最近落盘时间 (基础存活检查)
    recent = _list_recent_shards(now, max_age_sec=max(STALE_THRESHOLD_SEC,
                                                       TRADE_WINDOW_SEC))
    if recent:
        latest_mtime = max(mt for _, mt in recent)
        age = now - latest_mtime
        info["last_file_age_sec"] = int(age)
        info["total_recent_shards"] = len(recent)
        if age > STALE_THRESHOLD_SEC:
            issues.append(
                f"last_file_age={age:.0f}s (threshold={STALE_THRESHOLD_SEC}s)")
    else:
        if uptime > 600:
            issues.append("no_parquet_files_after_10min")

    # 2. Trade 流检测 (新增, 防 aggTrade silent death)
    trade_issues, trade_info = check_trade_rate(now, recent, warmup_done)
    issues.extend(trade_issues)
    info.update(trade_info)

    # 3. 磁盘空间
    try:
        stat = os.statvfs("/app/replay_buffer")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used_pct = ((total - free) / total) * 100 if total > 0 else 0
        info["disk_used_percent"] = round(used_pct, 1)
        info["disk_free_gb"] = round(free / (1024 ** 3), 2)
        if used_pct > DISK_WARN_PERCENT:
            issues.append(
                f"disk_usage={used_pct:.1f}% (threshold={DISK_WARN_PERCENT}%)")
    except Exception:
        pass

    return issues, info


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            try:
                issues, info = check_health()
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"status": "error", "error": f"{type(e).__name__}: {e}"}
                ).encode())
                return
            if issues:
                self.send_response(503)
                body = {"status": "unhealthy", "issues": issues, "info": info}
            else:
                self.send_response(200)
                body = {"status": "healthy", "info": info}
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默日志，避免刷屏


_autoheal_state = {
    "trade_silent_since": None,
    "last_restart_at": 0,
}
_autoheal_lock = threading.Lock()


def _restart_recorder() -> None:
    """SIGTERM the recorder process; supervisord will autorestart per
    its conf (autorestart=true)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "main.py record"],
            capture_output=True, text=True, check=False, timeout=10,
        )
        pids = [p for p in result.stdout.strip().split() if p.isdigit()]
        if not pids:
            print("[autoheal] no recorder process found via pgrep")
            return
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"[autoheal] sent SIGTERM to recorder pid={pid}")
            except OSError as e:
                print(f"[autoheal] kill {pid} failed: {e}")
    except Exception as e:
        print(f"[autoheal] _restart_recorder error: {type(e).__name__}: {e}")


def _autoheal_loop() -> None:
    """Background thread: every AUTOHEAL_CHECK_INTERVAL_SEC, re-evaluate
    health. If trade_silent persists past AUTOHEAL_AFTER_SEC, restart the
    recorder (subject to cooldown)."""
    while True:
        time.sleep(AUTOHEAL_CHECK_INTERVAL_SEC)
        try:
            issues, _ = check_health()
        except Exception as e:
            print(f"[autoheal] check_health error: {e}")
            continue
        now = time.time()
        is_silent = any("trade_silent" in i for i in issues)
        with _autoheal_lock:
            if is_silent:
                if _autoheal_state["trade_silent_since"] is None:
                    _autoheal_state["trade_silent_since"] = now
                    print(f"[autoheal] trade_silent detected, monitoring...")
                silent_for = now - _autoheal_state["trade_silent_since"]
                cooldown_remaining = (
                    AUTOHEAL_COOLDOWN_SEC
                    - (now - _autoheal_state["last_restart_at"])
                )
                if (silent_for >= AUTOHEAL_AFTER_SEC
                        and cooldown_remaining <= 0):
                    print(
                        f"[autoheal] trade_silent for {silent_for/60:.0f}min, "
                        f"restarting recorder (will retry in "
                        f"{AUTOHEAL_COOLDOWN_SEC//60}min if still silent)")
                    _restart_recorder()
                    _autoheal_state["last_restart_at"] = now
                    _autoheal_state["trade_silent_since"] = None
            else:
                if _autoheal_state["trade_silent_since"] is not None:
                    print("[autoheal] trades resumed, clearing silent timer")
                    _autoheal_state["trade_silent_since"] = None


if __name__ == "__main__":
    if AUTOHEAL_ENABLED:
        threading.Thread(target=_autoheal_loop, daemon=True).start()
        print(f"[healthcheck] autoheal enabled "
              f"(threshold={AUTOHEAL_AFTER_SEC//60}min trade_silent)")
    else:
        print("[healthcheck] autoheal disabled (NARCI_AUTOHEAL=0)")
    server = HTTPServer(("0.0.0.0", 8079), HealthHandler)
    print("[healthcheck] Listening on :8079")
    server.serve_forever()
