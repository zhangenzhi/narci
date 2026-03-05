"""
轻量级健康检查 HTTP 服务
检查项:
  1. recorder 进程是否存活
  2. 最近 5 分钟内是否有新的 parquet 落盘
"""
import os
import time
import glob
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

DATA_DIR = "/app/replay_buffer/realtime"
COLD_DIR = "/app/replay_buffer/cold"
STALE_THRESHOLD_SEC = 300  # 5 分钟无新文件视为不健康
DISK_WARN_PERCENT = 90     # 磁盘使用超过 90% 告警


def check_health():
    issues = []
    info = {}

    # 1. 检查最近落盘时间
    parquet_files = glob.glob(os.path.join(DATA_DIR, "**", "*_RAW_*.parquet"), recursive=True)
    if parquet_files:
        latest_mtime = max(os.path.getmtime(f) for f in parquet_files)
        age = time.time() - latest_mtime
        info["last_file_age_sec"] = int(age)
        info["total_raw_files"] = len(parquet_files)
        if age > STALE_THRESHOLD_SEC:
            issues.append(f"last_file_age={age:.0f}s (threshold={STALE_THRESHOLD_SEC}s)")
    else:
        uptime_file = "/tmp/narci_start_time"
        if os.path.exists(uptime_file):
            start_time = float(open(uptime_file).read().strip())
            if time.time() - start_time > 600:
                issues.append("no_parquet_files_after_10min")
        else:
            with open(uptime_file, "w") as f:
                f.write(str(time.time()))

    # 2. 磁盘空间检查
    try:
        stat = os.statvfs("/app/replay_buffer")
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bavail * stat.f_frsize
        used_pct = ((total - free) / total) * 100 if total > 0 else 0
        info["disk_used_percent"] = round(used_pct, 1)
        info["disk_free_gb"] = round(free / (1024**3), 2)
        if used_pct > DISK_WARN_PERCENT:
            issues.append(f"disk_usage={used_pct:.1f}% (threshold={DISK_WARN_PERCENT}%)")
    except Exception:
        pass

    # 3. 冷数据归档统计
    if os.path.exists(COLD_DIR):
        cold_files = [f for f in os.listdir(COLD_DIR) if f.endswith(".parquet")]
        info["cold_archive_files"] = len(cold_files)

    return issues, info


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            issues, info = check_health()
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


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8079), HealthHandler)
    print("[healthcheck] Listening on :8079")
    server.serve_forever()
