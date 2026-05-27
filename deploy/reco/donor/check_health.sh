#!/usr/bin/env bash
# Donor-side health check. Verifies the bv-push tmux session is alive and the
# donor loop has produced log output recently. Writes PASS/FAIL + reason to
# .donor/health_status.txt; on FAIL, fires a macOS notification.
#
# Designed to be run by launchd ~once a day. The donor loop sleeps 86400s
# between iterations, so we tolerate a 30h log-silence window.

set -u

NARCI_DIR="${NARCI_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
LOG="${NARCI_DIR}/.donor/binance_vision_push.log"
STATUS="${NARCI_DIR}/.donor/health_status.txt"
SESSION="bv-push"
MAX_LOG_AGE_SEC=$((30 * 3600))

now_epoch=$(date +%s)
ts=$(date '+%F %T %Z')
fails=()

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  fails+=("tmux session '$SESSION' not found")
fi

if [[ ! -f "$LOG" ]]; then
  fails+=("log file missing: $LOG")
else
  log_mtime=$(stat -f %m "$LOG")
  age=$((now_epoch - log_mtime))
  if (( age > MAX_LOG_AGE_SEC )); then
    fails+=("log untouched for ${age}s (>$MAX_LOG_AGE_SEC)")
  fi
fi

if [[ ${#fails[@]} -eq 0 ]]; then
  printf 'PASS\t%s\ttmux=alive log_age=%ss\n' "$ts" "$age" > "$STATUS"
  exit 0
fi

reason=$(IFS='; '; echo "${fails[*]}")
printf 'FAIL\t%s\t%s\n' "$ts" "$reason" > "$STATUS"

osascript -e "display notification \"$reason\" with title \"narci donor unhealthy\" sound name \"Basso\"" 2>/dev/null || true
exit 1
