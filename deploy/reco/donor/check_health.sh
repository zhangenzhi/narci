#!/usr/bin/env bash
# Donor-side health check (aws-sg / linux 版)。
#
# 判活:donor loop 是否仍在产出新日志(若装了 tmux,额外验 bv-push session)。
# 告警:写 PASS/FAIL 到 .donor/health_status.txt + 发 CloudWatch 自定义指标
#   Narci/Donor/DonorHealthy(1=健康 / 0=失败),由 CloudWatch Alarm → SNS 寻呼。
#   (旧 Mac donor 上额外发桌面通知。)
#
# 部署:cron / systemd-timer ~每天跑一次。loop sleep 86400s,故容忍 30h 日志静默。
# 权限:aws-sg 实例 IAM 角色需 cloudwatch:PutMetricData。
#
# 环境变量:
#   AWS_REGION            CloudWatch region(默认 ap-southeast-1 / aws-sg)
#   DONOR_CW_NAMESPACE    指标 namespace(默认 Narci/Donor)
#   DONOR_TMUX_SESSION    tmux session 名(默认 bv-push;无 tmux 则跳过该检查)

set -u

NARCI_DIR="${NARCI_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
LOG="${NARCI_DIR}/.donor/binance_vision_push.log"
STATUS="${NARCI_DIR}/.donor/health_status.txt"
SESSION="${DONOR_TMUX_SESSION:-bv-push}"
MAX_LOG_AGE_SEC=$((30 * 3600))
CW_NAMESPACE="${DONOR_CW_NAMESPACE:-Narci/Donor}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-ap-southeast-1}}"

mkdir -p "$(dirname "$STATUS")"   # 确保 .donor/ 存在(健康检查可能先于 loop 首跑)

now_epoch=$(date +%s)
ts=$(date '+%F %T %Z')
fails=()

# 可移植 mtime:GNU/linux `stat -c %Y`,回退 BSD/macOS `stat -f %m`
_mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# tmux 判活只在装了 tmux 时做(旧 Mac donor);aws-sg 走 systemd/cron 无 session,
# 靠下面的日志新鲜度判活。
if command -v tmux >/dev/null 2>&1; then
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    fails+=("tmux session '$SESSION' not found")
  fi
fi

age=""
if [[ ! -f "$LOG" ]]; then
  fails+=("log file missing: $LOG")
else
  log_mtime=$(_mtime "$LOG")
  age=$((now_epoch - log_mtime))
  if (( age > MAX_LOG_AGE_SEC )); then
    fails+=("log untouched for ${age}s (>$MAX_LOG_AGE_SEC)")
  fi
fi

# 发 CloudWatch 指标(best-effort;需 aws cli + PutMetricData 权限)
_emit_cw() {  # $1 = 1|0 healthy
  command -v aws >/dev/null 2>&1 || return 0
  aws cloudwatch put-metric-data \
    --region "$AWS_REGION" --namespace "$CW_NAMESPACE" \
    --metric-name DonorHealthy --value "$1" \
    --dimensions "Host=$(hostname)" >/dev/null 2>&1 || true
  if [[ -n "$age" ]]; then
    aws cloudwatch put-metric-data \
      --region "$AWS_REGION" --namespace "$CW_NAMESPACE" \
      --metric-name DonorLogAgeSec --value "$age" --unit Seconds \
      --dimensions "Host=$(hostname)" >/dev/null 2>&1 || true
  fi
}

if [[ ${#fails[@]} -eq 0 ]]; then
  printf 'PASS\t%s\tlog_age=%ss\n' "$ts" "$age" > "$STATUS"
  _emit_cw 1
  exit 0
fi

reason=$(IFS='; '; echo "${fails[*]}")
printf 'FAIL\t%s\t%s\n' "$ts" "$reason" > "$STATUS"
_emit_cw 0

# 旧 Mac donor:额外桌面通知(linux 无 osascript,自动跳过)
if [[ "$(uname)" == "Darwin" ]] && command -v osascript >/dev/null 2>&1; then
  osascript -e "display notification \"$reason\" with title \"narci donor unhealthy\" sound name \"Basso\"" 2>/dev/null || true
fi
exit 1
