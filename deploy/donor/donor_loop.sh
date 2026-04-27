#!/usr/bin/env bash
# Donor-side daily loop wrapper. Runs binance_vision_push.sh once a day inside
# a persistent tmux session.
#
# Usage on the donor (Mac Mini / linux box with crypto network access):
#   tmux new -d -s bv-push -c /path/to/narci 'bash deploy/donor/donor_loop.sh'
#
# Configuration via env (same as binance_vision_push.sh):
#   NARCI_DIR      — narci project root (default: derived)
#   DRIVE_REMOTE   — rclone remote (default: gdrive:narci_official)
#   PYTHON         — python interpreter (default: python)
#   INTERVAL       — seconds between iterations (default: 86400 = 1 day)

set -u

NARCI_DIR="${NARCI_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
INTERVAL="${INTERVAL:-86400}"
PUSH_SCRIPT="${NARCI_DIR}/deploy/donor/binance_vision_push.sh"

cd "$NARCI_DIR"

while true; do
  echo "[$(date '+%F %T')] >>> donor iteration begin"
  bash "$PUSH_SCRIPT"
  rc=$?
  echo "[$(date '+%F %T')] <<< donor iteration end (rc=$rc), sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
