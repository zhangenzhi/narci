#!/usr/bin/env bash
# narci-reco — 把 service 的 :prev image 重新挂回 :latest 然后 force-recreate
#
# 用法:
#   ./aws/recorder_rollback.sh jp recorder-gmo-spot
#   ./aws/recorder_rollback.sh sg recorder-binance-umfut
#
# 前提:之前用 `aws/recorder_build.sh` 跑过(它会自动 tag 旧 :latest 成 :prev)。
# 如果是直接 docker compose build 没经过我们的 build 脚本,可能没 :prev,会失败。
#
# 时间预算:全程 < 30s(只是 tag + recreate,不重新 build)。

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
: "${NARCI_DEPLOY_PATH:=/home/ec2-user/narci}"

target="${1:?usage: $0 <jp|sg> <service-name>}"
service="${2:?need service name}"

case "$target" in
  jp) instance_id="$NARCI_JP_INSTANCE_ID"; region="$AWS_REGION_JP"; profiles="tokyo,tokyo-extra" ;;
  sg) instance_id="$NARCI_SG_INSTANCE_ID"; region="$AWS_REGION_SG"; profiles="global" ;;
  *)  echo "usage: $0 <jp|sg> <service>" >&2; exit 1 ;;
esac

image="narci-${service}"
echo "[narci-reco] ROLLBACK target=$target service=$service image=$image"

# 复杂逻辑 base64 push,避免 SSM shorthand parser 撞内嵌双引号
remote_script() {
  cat <<EOF
set -e
cd $NARCI_DEPLOY_PATH

echo === step 1: 检查 $image:prev 是否存在 ===
PREV=\$(docker images -q "$image:prev" 2>/dev/null)
if [ -z "\$PREV" ]; then
  echo "ERROR: $image:prev 不存在 — 没法回滚"
  echo "  (回滚前提是之前跑过 aws/recorder_build.sh,它会自动备份 :prev)"
  exit 1
fi
echo "  found $image:prev = \$PREV"

echo === step 2: swap :latest <-> :prev ===
CURRENT=\$(docker images -q "$image:latest" 2>/dev/null)
docker tag "$image:prev" "$image:latest"
echo "  retagged $image:latest <- :prev (\$PREV)"
if [ -n "\$CURRENT" ] && [ "\$CURRENT" != "\$PREV" ]; then
  docker tag "\$CURRENT" "$image:prev"
  echo "  retagged $image:prev <- old-latest (\$CURRENT) — enables roll-forward"
fi

echo === step 3: docker compose force-recreate $service ===
COMPOSE_PROFILES=$profiles docker compose up -d --force-recreate $service 2>&1 | tail -10

echo === step 4: docker compose ps ===
docker compose ps $service

echo === step 5: 等 5s 后看 log tail ===
sleep 5
docker compose logs --tail 20 $service 2>&1 | grep -v aliases || true

echo === rollback done ===
EOF
}

B64=$(remote_script | base64 | tr -d '\n')

command_id=$(aws ssm send-command \
  --region "$region" \
  --instance-ids "$instance_id" \
  --document-name "AWS-RunShellScript" \
  --comment "narci-reco rollback $target/$service to :prev" \
  --parameters "commands=[\"echo $B64 | base64 -d > /tmp/narci_rollback.sh\",\"chown ec2-user:ec2-user /tmp/narci_rollback.sh\",\"sudo -u ec2-user -i bash /tmp/narci_rollback.sh\"]" \
  --query 'Command.CommandId' \
  --output text)

echo "[narci-reco] command_id=$command_id"

for _ in $(seq 1 30); do
  sleep 2
  status=$(aws ssm get-command-invocation \
    --region "$region" \
    --command-id "$command_id" \
    --instance-id "$instance_id" \
    --query 'Status' \
    --output text 2>/dev/null || echo Pending)
  case "$status" in Success|Failed|TimedOut|Cancelled) break ;; esac
done

echo "[narci-reco] 最终 status: $status"
echo "---- stdout ----"
aws ssm get-command-invocation \
  --region "$region" --command-id "$command_id" --instance-id "$instance_id" \
  --query 'StandardOutputContent' --output text
echo "---- stderr ----"
aws ssm get-command-invocation \
  --region "$region" --command-id "$command_id" --instance-id "$instance_id" \
  --query 'StandardErrorContent' --output text

if [ "$status" = "Success" ]; then
  echo
  echo "[narci-reco] ✓ rollback done."
  echo "[narci-reco] 提醒:回滚后 :latest 是上一版,这次失败的 image 已被覆盖。"
  echo "[narci-reco] 重新 fix 流程:git pull 后再跑 ./aws/recorder_build.sh"
fi

[ "$status" = "Success" ]
