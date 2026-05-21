#!/usr/bin/env bash
# narci-reco — per-venue shard freshness monitor
#
# 解决:docker compose ps healthy + healthcheck.py 看的都是 aggregate buffer,
# 单 venue 死掉聚合永远 healthy。已经被 GMO ×4 + UM ×1 silent fail 坑过。
# 本脚本 SSM 两个 fleet 各 venue 的最新 shard mtime,与 now 比对,过 threshold
# desktop notification + 写 alert log。
#
# 用法:
#   ./aws/venue_stale_monitor.sh                  # 单次跑,stdout 报告
#   ./aws/venue_stale_monitor.sh --quiet          # 仅 alert 写 log,无 stdout
#   ./aws/venue_stale_monitor.sh --threshold 300  # 自定义 stale 阈值(秒)
#
# cron 安装(Mac 本地,每 10 min 一次,quiet 仅警报):
#   echo "*/10 * * * * cd /Users/zhangenzhi/work/narci/reco && \\
#     ./aws/venue_stale_monitor.sh --quiet >> ~/narci_monitor.log 2>&1" \\
#     | crontab -e -

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

if [ -f ".env" ]; then
  set -a; . ./.env; set +a
fi

: "${NARCI_JP_INSTANCE_ID:?need NARCI_JP_INSTANCE_ID in .env}"
: "${NARCI_SG_INSTANCE_ID:?need NARCI_SG_INSTANCE_ID in .env}"
: "${AWS_REGION_JP:=ap-northeast-1}"
: "${AWS_REGION_SG:=ap-southeast-1}"

# 默认 600s 阈值:save_interval=60s 上限 + 600s 缓冲(覆盖 cloud-sync 一轮等)
THRESHOLD_SEC="${VENUE_STALE_THRESHOLD_SEC:-600}"
ALERT_LOG="${HOME}/narci_alerts.log"
QUIET="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --quiet)     QUIET="true"; shift ;;
    --threshold) THRESHOLD_SEC="$2"; shift 2 ;;
    *)           echo "unknown: $1" >&2; exit 1 ;;
  esac
done

# 已知 parked venue — 不监控、不报警。注释行 = 监控范围。
# 格式: fleet:venue_path:short_label[:threshold_override_sec]
# threshold override 必须 ≥ 该 venue 的 save_interval × 2,否则会 false alert。
# 当前各 venue save_interval (per configs/exchanges/*.yaml, 2026-05-19 narci 15ff210):
#   CC / BJ / UM = 60s  → 默认 600s threshold 即可 (10x interval)
#   BS / BB / BF-* = 600s → 需 override 到 1500s
VENUES_ACTIVE=(
  "jp:coincheck/spot:CC"
  "jp:binance_jp/spot:BJ"
  "jp:bitbank/spot:BB:1500"
  "jp:bitflyer/spot:BF-spot:1500"
  "jp:bitflyer/fx:BF-fx:1500"
  "sg:binance/spot:BS:1500"
  "sg:binance/um_futures:UM"
)
# Parked(stopped intentionally) — 不监控:
#   jp:gmo/spot:GMO-spot         # 2026-05-21 silent deep ban,见 incident
#   jp:gmo/leverage:GMO-lev      # 同上

probe_fleet() {
  local fleet="$1" region instance
  case "$fleet" in
    jp) region="$AWS_REGION_JP"; instance="$NARCI_JP_INSTANCE_ID" ;;
    sg) region="$AWS_REGION_SG"; instance="$NARCI_SG_INSTANCE_ID" ;;
    *)  echo "bad fleet: $fleet" >&2; return 1 ;;
  esac

  # 一条 SSM 命令扫所有 venue dir,输出 "venue=mtime_epoch"
  local cmd_id
  cmd_id=$(aws ssm send-command \
    --region "$region" --instance-ids "$instance" \
    --document-name "AWS-RunShellScript" \
    --comment "venue-stale-monitor probe $fleet" \
    --parameters 'commands=[
      "docker run --rm -v narci_narci-data:/data alpine sh -c '"'"'for d in /data/realtime/*/*/l2; do [ -d \"$d\" ] || continue; v=${d#/data/realtime/}; v=${v%/l2}; latest=$(ls -t \"$d\"/*.parquet 2>/dev/null | head -1); [ -z \"$latest\" ] && { echo \"$v=NO_FILES\"; continue; }; mt=$(stat -c %Y \"$latest\" 2>/dev/null); echo \"$v=$mt\"; done'"'"'"
    ]' \
    --query 'Command.CommandId' --output text 2>/dev/null) || return 1

  for _ in $(seq 1 15); do
    local st
    st=$(aws ssm get-command-invocation \
      --region "$region" --command-id "$cmd_id" \
      --instance-id "$instance" --query 'Status' --output text 2>/dev/null || echo Pending)
    case "$st" in Success|Failed|TimedOut|Cancelled) break ;; esac
    sleep 2
  done

  aws ssm get-command-invocation \
    --region "$region" --command-id "$cmd_id" \
    --instance-id "$instance" --query 'StandardOutputContent' --output text 2>/dev/null
}

# 收集两 fleet 输出到 flat 文本(Mac bash 3.x 无 declare -A,grep 查询)
jp_out="$(probe_fleet jp 2>/dev/null || true)"
sg_out="$(probe_fleet sg 2>/dev/null || true)"

now_ts=$(date -u +%s)
alerts=()

# venue=mtime 查询 helper (从 fleet 输出找指定 venue 行)
lookup_mt() {
  local fleet="$1" vpath="$2" out
  case "$fleet" in
    jp) out="$jp_out" ;;
    sg) out="$sg_out" ;;
  esac
  # 行格式: "<venue_path>=<mtime>" e.g. "coincheck/spot=1779394823"
  echo "$out" | awk -F= -v key="$vpath" '$1==key {print $2; exit}'
}

# 对照 active 列表,缺数据或过 threshold 都报警
for v in "${VENUES_ACTIVE[@]}"; do
  IFS=: read -r vfleet vpath vlabel vthresh <<<"$v"
  # 没指定 override 用全局 threshold
  : "${vthresh:=$THRESHOLD_SEC}"
  mt="$(lookup_mt "$vfleet" "$vpath")"

  if [ -z "$mt" ]; then
    alerts+=("[$vfleet] $vlabel ($vpath): NO MTIME RETURNED (instance unreachable / dir missing)")
  elif [ "$mt" = "NO_FILES" ]; then
    alerts+=("[$vfleet] $vlabel ($vpath): directory empty (recorder never wrote?)")
  else
    age=$(( now_ts - mt ))
    if [ "$age" -gt "$vthresh" ]; then
      min=$(( age / 60 ))
      alerts+=("[$vfleet] $vlabel ($vpath): stale ${min}min (>$((vthresh / 60))min threshold)")
    fi
  fi
done

# 报告
ts_now="$(date -u +%FT%TZ)"
if [ ${#alerts[@]} -gt 0 ]; then
  body=$(printf '%s\n' "${alerts[@]}")
  {
    echo "[$ts_now] STALE-VENUE-ALERT (threshold=${THRESHOLD_SEC}s)"
    printf '  %s\n' "${alerts[@]}"
  } >> "$ALERT_LOG"
  if [ "$QUIET" != "true" ]; then
    echo "🚨 [$ts_now] STALE-VENUE-ALERT"
    printf '  %s\n' "${alerts[@]}"
  fi
  # Mac desktop notification (osascript 在 Mac 上,Linux 上 silently no-op)
  if command -v osascript >/dev/null 2>&1; then
    title="narci venue stale (${#alerts[@]})"
    # osascript 一次只能传一条,合并多条
    short=$(printf '%s\n' "${alerts[@]}" | head -3)
    osascript -e "display notification \"$short\" with title \"$title\"" 2>/dev/null || true
  fi
  exit 1
fi

if [ "$QUIET" != "true" ]; then
  echo "[$ts_now] all active venues fresh (default threshold=${THRESHOLD_SEC}s)"
  for v in "${VENUES_ACTIVE[@]}"; do
    IFS=: read -r vfleet vpath vlabel vthresh <<<"$v"
    : "${vthresh:=$THRESHOLD_SEC}"
    mt="$(lookup_mt "$vfleet" "$vpath")"
    if [[ "$mt" =~ ^[0-9]+$ ]]; then
      age=$((now_ts - mt))
    else
      age="?"
    fi
    printf '  [%s] %-10s mtime=%s age=%ss (thresh %ss)\n' \
      "$vfleet" "$vlabel" "${mt:-?}" "$age" "$vthresh"
  done
fi
