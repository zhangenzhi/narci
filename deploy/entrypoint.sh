#!/bin/bash
set -e

MODE="${1:-record}"

echo "============================================"
echo " Narci Recorder Container"
echo " Mode: $MODE"
echo " Config: ${NARCI_CONFIG:-configs/um_future_recorder.yaml}"
echo " Symbol Override: ${NARCI_SYMBOL:-none}"
echo "============================================"

# 确保数据目录存在
mkdir -p /app/replay_buffer/realtime/um_futures/l2
mkdir -p /app/replay_buffer/realtime/spot/l2

case "$MODE" in
  record)
    echo "[entrypoint] Starting supervisord (recorder + healthcheck)..."
    exec supervisord -c /app/deploy/supervisord.conf
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    echo "[entrypoint] Unknown mode: $MODE"
    echo "Usage: record | shell"
    echo ""
    echo "NOTE: compact / validate 请在本地执行:"
    echo "  python main.py compact --symbol ALL"
    exit 1
    ;;
esac
