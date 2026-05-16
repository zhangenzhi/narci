#!/usr/bin/env bash
# narci-reco — 列两台 recorder EC2 实例状态
#
# 用法:./aws/ec2_describe.sh
#       ./aws/ec2_describe.sh jp
#       ./aws/ec2_describe.sh sg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

# 加载 .env (允许 .env 缺失,仅警告)
if [ -f ".env" ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
else
  echo "[warn] .env 不存在,使用 shell 环境变量" >&2
fi

: "${NARCI_JP_INSTANCE_ID:?need NARCI_JP_INSTANCE_ID in .env}"
: "${NARCI_SG_INSTANCE_ID:?need NARCI_SG_INSTANCE_ID in .env}"
: "${AWS_REGION_JP:=ap-northeast-1}"
: "${AWS_REGION_SG:=ap-southeast-1}"

target="${1:-all}"

describe_one() {
  local label="$1"
  local instance_id="$2"
  local region="$3"
  echo "=== $label ($instance_id, $region) ==="
  aws ec2 describe-instances \
    --region "$region" \
    --instance-ids "$instance_id" \
    --query 'Reservations[0].Instances[0].{State:State.Name,Type:InstanceType,LaunchTime:LaunchTime,PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,AZ:Placement.AvailabilityZone}' \
    --output table
  echo
}

case "$target" in
  jp)  describe_one "aws-jp" "$NARCI_JP_INSTANCE_ID" "$AWS_REGION_JP" ;;
  sg)  describe_one "aws-sg" "$NARCI_SG_INSTANCE_ID" "$AWS_REGION_SG" ;;
  all) describe_one "aws-jp" "$NARCI_JP_INSTANCE_ID" "$AWS_REGION_JP"
       describe_one "aws-sg" "$NARCI_SG_INSTANCE_ID" "$AWS_REGION_SG" ;;
  *)   echo "usage: $0 [jp|sg|all]" >&2; exit 1 ;;
esac
