#!/bin/bash
# =============================================================================
# Narci EC2 启动脚本 (Amazon Linux 2023 / ARM64)
#
# 使用方式：
#   1. 在 AWS 控制台 Launch Instance → Advanced details → User data
#   2. 粘贴本脚本
#   3. 在下方 ▼▼▼ 区域填入你的 Google Drive 凭证
#   4. 启动实例 → 约 3 分钟后自动开始录制 + 推送
#
# 前置要求：
#   - 区域: ap-northeast-1 (Tokyo)
#   - AMI:  Amazon Linux 2023 ARM64
#   - 规格: t4g.small (2 vCPU / 2GB)
#   - 存储: gp3 30GB
#   - Security Group: 开 22 (SSH)
# =============================================================================

# ▼▼▼▼▼ 填入 Google Drive Token + 部署模式 ▼▼▼▼▼
MY_GDRIVE_TOKEN='FILL_ME'
MY_GDRIVE_FOLDER_ID=''
# 部署模式: "tokyo" = Coincheck only, "global" = Binance only
MY_PROFILE='tokyo'
# ▲▲▲▲▲ 填完上面即可 ▲▲▲▲▲

set -euo pipefail
exec > >(tee /var/log/narci-bootstrap.log) 2>&1

echo "[$(date)] Narci bootstrap start"

# ---------- 1. 系统更新 + Docker ----------
dnf update -y
dnf install -y docker git jq
systemctl enable --now docker
usermod -aG docker ec2-user

# Compose plugin + Buildx (AL2023 不自带)
DOCKER_PLUGIN_DIR=/usr/libexec/docker/cli-plugins
mkdir -p "$DOCKER_PLUGIN_DIR"

COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | jq -r .tag_name)
curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64" \
    -o "$DOCKER_PLUGIN_DIR/docker-compose"
chmod +x "$DOCKER_PLUGIN_DIR/docker-compose"

BUILDX_VERSION=$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | jq -r .tag_name)
curl -SL "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/buildx-${BUILDX_VERSION}.linux-arm64" \
    -o "$DOCKER_PLUGIN_DIR/docker-buildx"
chmod +x "$DOCKER_PLUGIN_DIR/docker-buildx"

# ---------- 2. 克隆仓库 ----------
NARCI_HOME=/home/ec2-user/narci
if [ ! -d "$NARCI_HOME" ]; then
    sudo -u ec2-user git clone https://github.com/zhangenzhi/narci.git "$NARCI_HOME"
fi
cd "$NARCI_HOME"

# ---------- 3. 环境变量 ----------
# 从 SSM 读取 S3 bucket（可选；否则直接写在本脚本）
REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)
# 生成 .env（凭证来自脚本顶部的变量，不进 git）
cat > .env <<EOF
COMPOSE_PROFILES=${MY_PROFILE}
NARCI_RCLONE_REMOTE=gdrive:/narci_raw
RCLONE_GDRIVE_TOKEN=${MY_GDRIVE_TOKEN}
RCLONE_GDRIVE_FOLDER_ID=${MY_GDRIVE_FOLDER_ID}
NARCI_RETAIN_DAYS=7
SYNC_INTERVAL=600
EOF
chown ec2-user:ec2-user .env
chmod 600 .env

# ---------- 4. 启动 ----------
sudo -u ec2-user docker compose pull || true
sudo -u ec2-user docker compose up -d --build

echo "[$(date)] Narci bootstrap complete. Running:"
sudo -u ec2-user docker compose ps
