#!/usr/bin/env bash
# narci-reco — 带 tag 的 recorder image build,自动保留上一版 :prev 作回滚
#
# 用法:
#   ./aws/recorder_build.sh jp recorder-coincheck       # build 单 service
#   ./aws/recorder_build.sh sg recorder-binance-umfut
#   ./aws/recorder_build.sh jp --all                    # build profile 内所有
#
# 流程(每次都做):
#   1. SSM git pull --ff-only
#   2. 把现存的所有 narci-* :latest tag 复制成 :prev (旧版备份)
#   3. docker compose build $service                   (生成新 :latest)
#   4. 给新 :latest 同时打 :<commit-hash> tag         (历史保留 N 个版本)
#   5. **不 force-recreate** — build only。container 还在跑旧 image,等
#      显式 `recorder_restart.sh --service` 才切换。
#
# 配套:
#   - aws/recorder_rollback.sh <fleet> <service>  →  :prev 重新 tag 成 :latest
#     + force-recreate(秒回滚到上一版)
#   - aws/recorder_restart.sh <fleet> --service <name>  → 用新 :latest 推上线

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

target="${1:?usage: $0 <jp|sg> <service-name|--all>}"
service="${2:?need service name or --all}"

case "$target" in
  jp) instance_id="$NARCI_JP_INSTANCE_ID"; region="$AWS_REGION_JP"; profiles="tokyo,tokyo-extra" ;;
  sg) instance_id="$NARCI_SG_INSTANCE_ID"; region="$AWS_REGION_SG"; profiles="global" ;;
  *)  echo "usage: $0 <jp|sg> <service|--all>" >&2; exit 1 ;;
esac

if [ "$service" = "--all" ]; then
  build_args=""
  echo "[narci-reco] target=$target — build ALL services in profile [$profiles]"
else
  build_args="$service"
  echo "[narci-reco] target=$target service=$service"
fi

# 复杂的 docker tag loop 通过 base64 push (避免 SSM shorthand JSON 撞内嵌双引号)。
# 单 script 包括 tag-prev 和 tag-hash 两步,统一一次性扔过去。
remote_script() {
  cat <<EOF
set -e
cd $NARCI_DEPLOY_PATH

echo === step 1: git pull ===
git pull --ff-only
git log --oneline -2

echo === step 2: tag current :latest as :prev ===
for img in \$(docker images --format '{{.Repository}}' | grep '^narci-' | sort -u); do
  id=\$(docker images -q "\$img:latest" 2>/dev/null)
  if [ -n "\$id" ]; then
    docker tag "\$id" "\$img:prev"
    echo "  tagged \$img:prev id=\$id"
  fi
done

echo === step 3: docker compose build $build_args ===
COMPOSE_PROFILES=$profiles docker compose build $build_args 2>&1 | tail -20

HASH=\$(git rev-parse --short HEAD)
echo === step 4: tag new :latest as :\$HASH ===
for img in \$(docker images --format '{{.Repository}}' | grep '^narci-' | sort -u); do
  id=\$(docker images -q "\$img:latest" 2>/dev/null)
  if [ -n "\$id" ]; then
    docker tag "\$id" "\$img:\$HASH"
    echo "  tagged \$img:\$HASH id=\$id"
  fi
done

echo === step 5: image list ===
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' | grep '^narci-' | head -25

echo === build done ===
EOF
}

B64=$(remote_script | base64 | tr -d '\n')

echo "[narci-reco] pushing build script (base64, $(echo "$B64" | wc -c) bytes)..."

command_id=$(aws ssm send-command \
  --region "$region" \
  --instance-ids "$instance_id" \
  --document-name "AWS-RunShellScript" \
  --comment "narci-reco recorder_build $target $service" \
  --parameters "commands=[\"echo $B64 | base64 -d > /tmp/narci_build.sh\",\"chown ec2-user:ec2-user /tmp/narci_build.sh\",\"sudo -u ec2-user -i bash /tmp/narci_build.sh\"]" \
  --timeout-seconds 600 \
  --query 'Command.CommandId' \
  --output text)

echo "[narci-reco] command_id=$command_id"
echo "[narci-reco] 等待 build (~2-5 min)..."

for _ in $(seq 1 90); do
  sleep 4
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

echo
echo "[narci-reco] Next: 部署需手动跑(build only,container 还跑旧 image):"
echo "  ./aws/recorder_restart.sh $target --service $service"
echo "  (出问题秒回滚): ./aws/recorder_rollback.sh $target $service"

[ "$status" = "Success" ]
