#!/usr/bin/env bash
# Daily compact wrapper. Run via cron at UTC 01:00 to catch all of
# the previous UTC day, OR run manually with --date YYYYMMDD.
#
# Cron install (UTC 01:00 daily):
#   crontab -e
#   0 1 * * * /lustre1/work/c30636/narci/tools/compact_daily.sh
#
# Manual (any day):
#   /lustre1/work/c30636/narci/tools/compact_daily.sh --date 20260507
#
# Skips days where every (sym × venue) combination already has a
# DAILY parquet in cold tier — safe to run repeatedly.
#
# Env:
#   NARCI_RETAIN_DAYS=999  (default; per memory rule, never let
#                           recorder retain_days delete raw shards)
#   BINANCE_VISION_OFFLINE=1 (default; assume gdrive donor → rclone
#                             pulls Vision; never reach external net)

set -u
NARCI_ROOT="/lustre1/work/c30636/narci"
LOG_DIR="${NARCI_ROOT}/replay_buffer/.compact_logs"
mkdir -p "$LOG_DIR"

# Pick target date
if [[ "${1:-}" == "--date" && -n "${2:-}" ]]; then
    TARGET_DATE="$2"
else
    # Default: yesterday UTC (so cron at 01:00 picks up the just-finished day)
    TARGET_DATE=$(date -u -d "yesterday" +%Y%m%d)
fi

LOG_FILE="${LOG_DIR}/compact_${TARGET_DATE}_$(date -u +%Y%m%d_%H%M%S).log"

cd "$NARCI_ROOT"
export NARCI_RETAIN_DAYS=999
export BINANCE_VISION_OFFLINE=1

echo "===========================================" | tee -a "$LOG_FILE"
echo "compact_daily start UTC $(date -u '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "  target_date: $TARGET_DATE" | tee -a "$LOG_FILE"
echo "  cwd: $NARCI_ROOT" | tee -a "$LOG_FILE"
echo "===========================================" | tee -a "$LOG_FILE"

python3 -u main.py compact --symbol ALL --date "$TARGET_DATE" 2>&1 | tee -a "$LOG_FILE"
RC=${PIPESTATUS[0]}

echo "===========================================" | tee -a "$LOG_FILE"
echo "compact_daily done rc=$RC at UTC $(date -u '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"

# rotate: keep last 30 logs
ls -t "$LOG_DIR"/compact_*.log 2>/dev/null | tail -n +31 | xargs -r rm -f

exit $RC
