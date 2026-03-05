#!/bin/bash
set -e

MODE="${1:-record}"

echo "============================================"
echo " Narci Recorder Container (Record + Push)"
echo " Mode: $MODE"
echo " Config: ${NARCI_CONFIG:-configs/um_future_recorder.yaml}"
echo " Symbol Override: ${NARCI_SYMBOL:-none}"
echo " Rclone Remote: ${NARCI_RCLONE_REMOTE:-disabled}"
echo "============================================"

# 确保数据目录存在
mkdir -p /app/replay_buffer/realtime/um_futures/l2
mkdir -p /app/replay_buffer/realtime/spot/l2

# -------------------------------------------------------
# rclone 配置: 从环境变量自动生成 Google Drive 配置
# 需要设置 RCLONE_GDRIVE_TOKEN 环境变量 (OAuth token JSON)
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

    if rclone lsd gdrive:/ --config /root/.config/rclone/rclone.conf > /dev/null 2>&1; then
        echo "[entrypoint] rclone Google Drive 连接验证成功"

        # 在 Google Drive 上预建远端目录 (确保 rclone copy 目标存在)
        if [ -n "$NARCI_RCLONE_REMOTE" ]; then
            rclone mkdir "$NARCI_RCLONE_REMOTE" --config /root/.config/rclone/rclone.conf 2>/dev/null \
                && echo "[entrypoint] 远端目录已就绪: $NARCI_RCLONE_REMOTE" \
                || echo "[entrypoint] WARNING: 远端目录创建失败: $NARCI_RCLONE_REMOTE"
        fi
    else
        echo "[entrypoint] WARNING: rclone Google Drive 连接失败，请检查 RCLONE_GDRIVE_TOKEN"
    fi
else
    echo "[entrypoint] INFO: RCLONE_GDRIVE_TOKEN 未设置，录制数据将仅保存在本地"
fi

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
