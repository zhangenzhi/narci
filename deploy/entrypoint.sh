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
mkdir -p /app/replay_buffer/official_validation

case "$MODE" in
  record)
    echo "[entrypoint] Starting supervisord (recorder + cron + healthcheck)..."
    exec supervisord -c /app/deploy/supervisord.conf
    ;;
  compact)
    SYMBOL="${NARCI_SYMBOL:-ETHUSDT}"
    DATE_ARG="${NARCI_DATE:+--date $NARCI_DATE}"
    echo "[entrypoint] Running compaction for $SYMBOL $DATE_ARG"
    exec python -u main.py compact --symbol "$SYMBOL" $DATE_ARG
    ;;
  validate)
    echo "[entrypoint] Running data validation..."
    exec python -u -c "
from data.validator import BinanceDataValidator
v = BinanceDataValidator()
v.scan_directory('/app/replay_buffer')
"
    ;;
  shell)
    exec /bin/bash
    ;;
  *)
    echo "[entrypoint] Unknown mode: $MODE"
    echo "Usage: record | compact | validate | shell"
    exit 1
    ;;
esac
