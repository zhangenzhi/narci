#!/usr/bin/env bash
# Donor-side: download Binance Vision daily archives and push parquets to Drive.
#
# Run on a machine that's allowed to reach data.binance.vision (NOT the
# /lustre1/work/c30636 hosts). Reads the symbol list / date range from
# configs/downloader.yaml. Idempotent: BinanceVisionSource.download_day skips
# files that already exist locally.
#
# Cron example (UTC, daily at 04:30 — Binance publishes T-1 around midnight UTC):
#   30 4 * * * /path/to/narci/deploy/reco/donor/binance_vision_push.sh >> /var/log/bv_push.log 2>&1
#
# Required env (override via shell or systemd):
#   NARCI_DIR        — narci project root (default: derived from script path)
#   DRIVE_REMOTE     — rclone remote (default: gdrive:narci_official)
#   RCLONE_CONFIG    — rclone config path (default: ~/.config/rclone/rclone.conf)
#   PYTHON           — python interpreter with narci deps (default: python)
#   DONOR_RETAIN_DAYS— keep N days of local official_validation staging after
#                      push, prune older (default: 1). Bounds donor disk; gdrive
#                      is the source of truth so older local copies are disposable.
#                      Smaller = less disk but tighter retry buffer if push fails
#                      repeatedly (the donor health alarm backstops that case).

set -eu

NARCI_DIR="${NARCI_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
DRIVE_REMOTE="${DRIVE_REMOTE:-gdrive:narci_official}"
PYTHON="${PYTHON:-python}"
LOG_DIR="${NARCI_DIR}/.donor"
LOG="${LOG_DIR}/binance_vision_push.log"

mkdir -p "$LOG_DIR"

cd "$NARCI_DIR"

echo "[$(date '+%F %T')] >>> donor host: $(hostname)" | tee -a "$LOG"
echo "[$(date '+%F %T')] >>> downloading Binance Vision T-1 only (configs/donor_downloader.yaml)" | tee -a "$LOG"
"$PYTHON" main.py download --config configs/donor_downloader.yaml 2>&1 | tee -a "$LOG"

echo "[$(date '+%F %T')] >>> push replay_buffer/official_validation -> $DRIVE_REMOTE" | tee -a "$LOG"
rclone copy "${NARCI_DIR}/replay_buffer/official_validation" "$DRIVE_REMOTE" \
  --transfers 8 --checkers 16 \
  --log-level NOTICE 2>&1 | tee -a "$LOG"

# 推送后清本地中转。official_validation 在 donor 上只是上传缓冲(下游从 gdrive/lustre
# 取,不从 donor 取),rclone copy 只上传不删本地,而下载又「靠本地已存在跳过重复」——
# 两者叠加 = 无界堆盘(实测 2026-06:攒到 5.6G,把 aws-sg 30G 盘顶到 93%)。
# 留最近 DONOR_RETAIN_DAYS 天(默认 1),更旧的删。正常情况下 ≥N 天前的文件都在更早的
# 成功轮里推过 gdrive,故本地可弃;唯一风险是 push 连续失败 >N 天,此时旧文件可能在成功
# 上传前被删 —— 由 donor health alarm(check_health.sh → CloudWatch)兜底告警。
DONOR_RETAIN_DAYS="${DONOR_RETAIN_DAYS:-1}"
OFFICIAL_DIR="${NARCI_DIR}/replay_buffer/official_validation"
echo "[$(date '+%F %T')] >>> prune local staging (retain ${DONOR_RETAIN_DAYS}d) under $OFFICIAL_DIR" | tee -a "$LOG"
n_before=$(find "$OFFICIAL_DIR" -type f 2>/dev/null | wc -l)
find "$OFFICIAL_DIR" -type f -mtime +"${DONOR_RETAIN_DAYS}" -delete 2>/dev/null || true
find "$OFFICIAL_DIR" -type d -empty -delete 2>/dev/null || true
n_after=$(find "$OFFICIAL_DIR" -type f 2>/dev/null | wc -l)
echo "[$(date '+%F %T')]     pruned $((n_before - n_after)) files ($n_before -> $n_after)" | tee -a "$LOG"

echo "[$(date '+%F %T')] <<< done" | tee -a "$LOG"
