#!/usr/bin/env bash
# narci-reco — 通过 SSM 在 EC2 instance 内 curl localhost:807x/health
#
# 走 SSM SendCommand 是因为 health 端口默认不对外暴露 (安全组只放
# 22/443),从 Mac Studio 直接 HTTP 打不通。
#
# 用法:./aws/health_probe.sh jp
#       ./aws/health_probe.sh sg

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

target="${1:?usage: $0 <jp|sg>}"

case "$target" in
  jp) instance_id="$NARCI_JP_INSTANCE_ID"; region="$AWS_REGION_JP"; ports="$NARCI_JP_HEALTH_PORTS" ;;
  sg) instance_id="$NARCI_SG_INSTANCE_ID"; region="$AWS_REGION_SG"; ports="$NARCI_SG_HEALTH_PORTS" ;;
  *)  echo "usage: $0 <jp|sg>" >&2; exit 1 ;;
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
