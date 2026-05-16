#!/usr/bin/env bash
# narci-reco — 通过 SSM SendCommand 远程重启 recorder
#
# 用法:./aws/recorder_restart.sh jp
#       ./aws/recorder_restart.sh sg
#       ./aws/recorder_restart.sh jp --pull        # 先 git pull 再 restart
#       ./aws/recorder_restart.sh jp --service recorder-bitbank
#       ./aws/recorder_restart.sh sg --ps-only     # 仅 docker compose ps,不动 container
#
# 不需要 SSH key — 走 AWS Systems Manager Run Command。
# Instance 必须装了 SSM agent (Amazon Linux / Ubuntu 默认带)。

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
: "${NARCI_DEPLOY_PATH:=/home/ubuntu/narci}"

target="${1:?usage: $0 <jp|sg> [--pull] [--service <name>]}"
shift || true

do_pull="false"
service=""
ps_only="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --pull)    do_pull="true"; shift ;;
    --service) service="$2"; shift 2 ;;
    --ps-only) ps_only="true"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [ "$ps_only" = "true" ] && { [ "$do_pull" = "true" ] || [ -n "$service" ]; }; then
  echo "--ps-only 跟 --pull/--service 互斥(只读模式)" >&2; exit 1
fi

case "$target" in
  jp) instance_id="$NARCI_JP_INSTANCE_ID"; region="$AWS_REGION_JP" ;;
  sg) instance_id="$NARCI_SG_INSTANCE_ID"; region="$AWS_REGION_SG" ;;
  *)  echo "usage: $0 <jp|sg> [--pull] [--service <name>]" >&2; exit 1 ;;
esac

# 组装命令
cmds=()
cmds+=("cd $NARCI_DEPLOY_PATH")
if [ "$ps_only" = "true" ]; then
  cmds+=("docker compose ps")
else
  if [ "$do_pull" = "true" ]; then
    cmds+=("git pull --ff-only")
  fi
  if [ -n "$service" ]; then
    cmds+=("docker compose restart $service")
  else
    cmds+=("docker compose up -d --force-recreate")
  fi
  cmds+=("docker compose ps")
fi

# 拼成 JSON 数组
json_cmds=$(printf '"%s",' "${cmds[@]}" | sed 's/,$//')
parameters="commands=[$json_cmds]"

echo "[narci-reco] target=$target instance=$instance_id region=$region"
echo "[narci-reco] commands:"
printf '  %s\n' "${cmds[@]}"
echo

command_id=$(aws ssm send-command \
  --region "$region" \
  --instance-ids "$instance_id" \
  --document-name "AWS-RunShellScript" \
  --comment "narci-reco recorder_restart $target" \
  --parameters "$parameters" \
  --query 'Command.CommandId' \
  --output text)

echo "[narci-reco] command_id=$command_id"
echo "[narci-reco] 等待执行 (~30s)..."

# Poll 状态
for _ in $(seq 1 30); do
  sleep 2
  status=$(aws ssm get-command-invocation \
    --region "$region" \
    --command-id "$command_id" \
    --instance-id "$instance_id" \
    --query 'Status' \
    --output text 2>/dev/null || echo "Pending")
  if [ "$status" = "Success" ] || [ "$status" = "Failed" ] || [ "$status" = "Cancelled" ] || [ "$status" = "TimedOut" ]; then
    break
  fi
done

echo "[narci-reco] 最终 status: $status"
echo "---- stdout ----"
aws ssm get-command-invocation \
  --region "$region" \
  --command-id "$command_id" \
  --instance-id "$instance_id" \
  --query 'StandardOutputContent' \
  --output text
echo "---- stderr ----"
aws ssm get-command-invocation \
  --region "$region" \
  --command-id "$command_id" \
  --instance-id "$instance_id" \
  --query 'StandardErrorContent' \
  --output text

[ "$status" = "Success" ]
