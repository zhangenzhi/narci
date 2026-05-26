#!/usr/bin/env bash
# narci-reco — 通过 SSM 在 EC2 instance 内 curl localhost:807x/health
#               + cloud-sync 状态 + volume per-venue shard 概览
#
# 走 SSM SendCommand 是因为 health 端口默认不对外暴露 (安全组只放
# 22/443),从 Mac Studio 直接 HTTP 打不通。
#
# 用法:./aws/health_probe.sh jp
#       ./aws/health_probe.sh sg
#       ./aws/health_probe.sh jp --rclone   # 加 rclone 深度验证 (烧 gdrive API quota)
#
# 默认覆盖 (零 API quota):
#   1. /health endpoint × N (recorder 自报)
#   2. docker compose ps
#   3. cloud-sync 容器状态 + 近 30 min log
#   4. volume 每个 venue 目录下 parquet 计数 (recorder→volume 这一段)
#
# --rclone 追加:rclone lsd gdrive 看每个 venue 是否真到云端 (volume→gdrive 那一段)。

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
: "${NARCI_JP_HEALTH_PORTS:=8079,8081,8082}"
: "${NARCI_SG_HEALTH_PORTS:=8080}"

target="${1:?usage: $0 <jp|sg> [--rclone]}"
shift || true

do_rclone="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --rclone) do_rclone="true"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

case "$target" in
  jp) instance_id="$NARCI_JP_INSTANCE_ID"; region="$AWS_REGION_JP"; ports="$NARCI_JP_HEALTH_PORTS" ;;
  sg) instance_id="$NARCI_SG_INSTANCE_ID"; region="$AWS_REGION_SG"; ports="$NARCI_SG_HEALTH_PORTS" ;;
  *)  echo "usage: $0 <jp|sg> [--rclone]" >&2; exit 1 ;;
esac

# 组装命令数组,每条一个 JSON 元素 (跟 recorder_restart.sh 同 pattern)。
# 不能把 for/done 折单行 — newline → ';' 会产生 'do;' 语法错。
cmds=()
cmds+=("echo '=== narci-reco health probe ==='")
for port in $(echo "$ports" | tr ',' ' '); do
  cmds+=("echo '--- port $port ---'")
  cmds+=("curl -sS --max-time 3 http://localhost:${port}/health || echo '[FAIL] port $port unreachable'")
done
cmds+=("echo '=== docker compose ps ==='")
cmds+=("cd $NARCI_DEPLOY_PATH 2>/dev/null && docker compose ps || echo '[warn] $NARCI_DEPLOY_PATH 不存在'")

# === cloud-sync / gdrive 数据流终端验证 (默认开,见 feedback memory gdrive-in-health-probe) ===
cmds+=("echo '=== cloud-sync 状态 ==='")
cmds+=("docker inspect narci-cloud-sync --format 'state={{.State.Status}} started={{.State.StartedAt}}' 2>/dev/null || echo '[FAIL] cloud-sync 容器不存在'")
cmds+=("echo '--- cloud-sync 近 30 min 关键 log ---'")
cmds+=("docker logs --since 30m narci-cloud-sync 2>&1 | grep -E 'cloud-sync|STALE-VENUE-ALERT|ERROR|Failed' | tail -15 || echo '[warn] 无相关 log'")
cmds+=("echo '--- volume 每 venue 目录大小 (recorder→volume 段,~4K = 空,无数据落盘) ---'")
cmds+=("docker exec narci-cloud-sync sh -c 'du -sh /data/realtime/*/*/l2 2>/dev/null | sort -k2' || echo '[warn] cloud-sync exec 失败'")

if [ "$do_rclone" = "true" ]; then
  cmds+=("echo '=== rclone gdrive 深度验证 (烧 API quota) ==='")
  cmds+=("docker exec narci-cloud-sync rclone lsd gdrive:/narci_raw/realtime/ 2>&1 | head -20 || echo '[FAIL] rclone lsd 失败'")
fi

json_cmds=$(printf '"%s",' "${cmds[@]}" | sed 's/,$//')
parameters="commands=[$json_cmds]"

echo "[narci-reco] target=$target instance=$instance_id region=$region ports=$ports"

command_id=$(aws ssm send-command \
  --region "$region" \
  --instance-ids "$instance_id" \
  --document-name "AWS-RunShellScript" \
  --comment "narci-reco health_probe $target" \
  --parameters "$parameters" \
  --query 'Command.CommandId' \
  --output text)

for _ in $(seq 1 15); do
  sleep 2
  status=$(aws ssm get-command-invocation \
    --region "$region" \
    --command-id "$command_id" \
    --instance-id "$instance_id" \
    --query 'Status' \
    --output text 2>/dev/null || echo "Pending")
  case "$status" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
done

echo "[narci-reco] status: $status"
aws ssm get-command-invocation \
  --region "$region" \
  --command-id "$command_id" \
  --instance-id "$instance_id" \
  --query 'StandardOutputContent' \
  --output text

[ "$status" = "Success" ]
