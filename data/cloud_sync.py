"""
独立云同步服务 (Cloud Sync Daemon)

与录制器完全解耦，定时扫描本地数据目录并通过 rclone 推送至远端存储。
支持作为独立进程运行，也可通过 docker-compose sidecar 部署。

用法:
    python -m data.cloud_sync                           # 使用环境变量配置
    python -m data.cloud_sync --local-dir ./replay_buffer/realtime/um_futures/l2 \
                              --remote gdrive:/narci_raw \
                              --interval 300
"""

import os
import sys
import time
import signal
import argparse
import subprocess
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cloud_sync] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cloud_sync")


class CloudSyncDaemon:
    def __init__(self, local_dir, remote, interval=300, transfers=4):
        """
        :param local_dir:   本地待同步目录
        :param remote:      rclone 远端路径，如 gdrive:/narci_raw
        :param interval:    同步间隔（秒）
        :param transfers:   rclone 并发上传数
        """
        self.local_dir = local_dir
        self.remote = remote
        self.interval = interval
        self.transfers = transfers
        self.running = True

    def _sync_once(self):
        """执行一次 rclone copy"""
        if not os.path.exists(self.local_dir):
            logger.warning(f"本地目录不存在，跳过: {self.local_dir}")
            return False

        cmd = [
            "rclone", "copy",
            self.local_dir, self.remote,
            "--transfers", str(self.transfers),
            "--log-level", "NOTICE",
            "--no-traverse",
        ]

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.interval,
            )
            if result.returncode == 0:
                logger.info(f"同步完成 {self.local_dir} -> {self.remote}")
                return True
            else:
                err = result.stderr.decode().strip()
                logger.warning(f"rclone 异常 (rc={result.returncode}): {err}")
                return False
        except subprocess.TimeoutExpired:
            logger.warning(f"rclone 超时 (>{self.interval}s)，本轮跳过")
            return False
        except FileNotFoundError:
            logger.error("rclone 未安装，无法执行云同步")
            return False

    def run(self):
        logger.info(f"启动云同步守护进程")
        logger.info(f"  本地目录: {self.local_dir}")
        logger.info(f"  远端目标: {self.remote}")
        logger.info(f"  同步间隔: {self.interval}s")

        # 优雅退出
        def _handle_signal(signum, frame):
            logger.info(f"收到信号 {signum}，准备退出...")
            self.running = False

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        while self.running:
            self._sync_once()
            # 可中断的 sleep
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("云同步守护进程已退出")


def main():
    parser = argparse.ArgumentParser(description="Narci Cloud Sync Daemon")
    parser.add_argument(
        "--local-dir",
        default=os.environ.get("SYNC_LOCAL_DIR", "/app/replay_buffer/realtime"),
        help="本地数据目录",
    )
    parser.add_argument(
        "--remote",
        default=os.environ.get("NARCI_RCLONE_REMOTE", ""),
        help="rclone 远端路径 (如 gdrive:/narci_raw)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SYNC_INTERVAL", "300")),
        help="同步间隔秒数 (默认 300)",
    )
    parser.add_argument(
        "--transfers",
        type=int,
        default=int(os.environ.get("SYNC_TRANSFERS", "4")),
        help="rclone 并发上传数 (默认 4)",
    )
    args = parser.parse_args()

    if not args.remote:
        logger.error("未指定远端路径。请设置 --remote 或 NARCI_RCLONE_REMOTE 环境变量。")
        sys.exit(1)

    daemon = CloudSyncDaemon(
        local_dir=args.local_dir,
        remote=args.remote,
        interval=args.interval,
        transfers=args.transfers,
    )
    daemon.run()


if __name__ == "__main__":
    main()
