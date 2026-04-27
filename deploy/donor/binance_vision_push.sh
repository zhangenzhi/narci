#!/usr/bin/env bash
# Donor-side: download Binance Vision daily archives and push parquets to Drive.
#
# Run on a machine that's allowed to reach data.binance.vision (NOT the
# /lustre1/work/c30636 hosts). Reads the symbol list / date range from
# configs/downloader.yaml. Idempotent: BinanceVisionSource.download_day skips
# files that already exist locally.
#
# Cron example (UTC, daily at 04:30 — Binance publishes T-1 around midnight UTC):
#   30 4 * * * /path/to/narci/deploy/donor/binance_vision_push.sh >> /var/log/bv_push.log 2>&1
#
# Required env (override via shell or systemd):
#   NARCI_DIR        — narci project root (default: derived from script path)
#   DRIVE_REMOTE     — rclone remote (default: gdrive:narci_official)
#   RCLONE_CONFIG    — rclone config path (default: ~/.config/rclone/rclone.conf)
#   PYTHON           — python interpreter with narci deps (default: python)

set -eu

NARCI_DIR="${NARCI_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DRIVE_REMOTE="${DRIVE_REMOTE:-gdrive:narci_official}"
PYTHON="${PYTHON:-python}"
LOG_DIR="${NARCI_DIR}/.donor"
LOG="${LOG_DIR}/binance_vision_push.log"

mkdir -p "$LOG_DIR"

cd "$NARCI_DIR"

echo "[$(date '+%F %T')] >>> donor host: $(hostname)" | tee -a "$LOG"
echo "[$(date '+%F %T')] >>> downloading Binance Vision (configs/downloader.yaml)" | tee -a "$LOG"
"$PYTHON" main.py download 2>&1 | tee -a "$LOG"

echo "[$(date '+%F %T')] >>> push replay_buffer/official_validation -> $DRIVE_REMOTE" | tee -a "$LOG"
rclone copy "${NARCI_DIR}/replay_buffer/official_validation" "$DRIVE_REMOTE" \
  --transfers 8 --checkers 16 \
  --log-level NOTICE 2>&1 | tee -a "$LOG"

echo "[$(date '+%F %T')] <<< done" | tee -a "$LOG"
