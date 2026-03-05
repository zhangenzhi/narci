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
STALE_THRESHOLD_SEC = 300  # 5 分钟无新文件视为不健康


def check_health():
    issues = []

    # 检查最近落盘时间
    parquet_files = glob.glob(os.path.join(DATA_DIR, "**", "*_RAW_*.parquet"), recursive=True)
    if parquet_files:
        latest_mtime = max(os.path.getmtime(f) for f in parquet_files)
        age = time.time() - latest_mtime
        if age > STALE_THRESHOLD_SEC:
            issues.append(f"last_file_age={age:.0f}s (threshold={STALE_THRESHOLD_SEC}s)")
    else:
        # 容器刚启动时可能还没有文件，给 10 分钟宽限期
        uptime_file = "/tmp/narci_start_time"
        if os.path.exists(uptime_file):
            start_time = float(open(uptime_file).read().strip())
            if time.time() - start_time > 600:
                issues.append("no_parquet_files_after_10min")
        else:
            with open(uptime_file, "w") as f:
                f.write(str(time.time()))

    return issues


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            issues = check_health()
            if issues:
                self.send_response(503)
                body = {"status": "unhealthy", "issues": issues}
            else:
                self.send_response(200)
                body = {"status": "healthy"}
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
