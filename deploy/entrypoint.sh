#!/bin/bash
set -e

MODE="${1:-record}"

echo "============================================"
echo " Narci Recorder Container"
echo " Mode: $MODE"
echo " Config: ${NARCI_CONFIG:-configs/um_future_recorder.yaml}"
echo " Symbol Override: ${NARCI_SYMBOL:-none}"
echo " Retain Days: ${NARCI_RETAIN_DAYS:-7}"
echo "============================================"

# 确保数据目录存在
mkdir -p /app/replay_buffer/realtime/um_futures/l2
mkdir -p /app/replay_buffer/realtime/spot/l2
mkdir -p /app/replay_buffer/official_validation
mkdir -p /app/replay_buffer/cold

# -------------------------------------------------------
# rclone 配置: 从环境变量自动生成 Google Drive 配置
# 需要设置 RCLONE_GDRIVE_TOKEN 环境变量 (OAuth token JSON)
# 获取方式: 本地运行 rclone config, 授权后从 ~/.config/rclone/rclone.conf 中复制 token 值
# -------------------------------------------------------
if [ -n "$RCLONE_GDRIVE_TOKEN" ]; then
    mkdir -p /root/.config/rclone
    cat > /root/.config/rclone/rclone.conf <<RCLONE_EOF
[gdrive]
type = drive
scope = drive
token = ${RCLONE_GDRIVE_TOKEN}
root_folder_id = ${RCLONE_GDRIVE_FOLDER_ID:-}
RCLONE_EOF
    echo "[entrypoint] rclone Google Drive 配置已生成"

    # 验证连通性
    if rclone lsd gdrive:/ --config /root/.config/rclone/rclone.conf > /dev/null 2>&1; then
        echo "[entrypoint] rclone Google Drive 连接验证成功"
    else
        echo "[entrypoint] WARNING: rclone Google Drive 连接失败，请检查 RCLONE_GDRIVE_TOKEN"
    fi
else
    echo "[entrypoint] WARNING: RCLONE_GDRIVE_TOKEN 未设置，冷数据将不会同步到 Google Drive"
fi

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
  sync)
    echo "[entrypoint] Running manual rclone sync to Google Drive..."
    exec rclone sync /app/replay_buffer/cold gdrive:/narci_cold \
      --config /root/.config/rclone/rclone.conf \
      --transfers 4 \
      --log-level INFO \
      --progress
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
    echo "Usage: record | compact | sync | validate | shell"
    exit 1
    ;;
esac
