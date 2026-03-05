#!/bin/bash
# ============================================================
# Narci Recorder - GCP Free Tier 一键部署脚本
# 目标: e2-micro (免费) + Docker + rclone → Google Drive
# ============================================================
set -e

PROJECT_ID=$(gcloud config get-value project)
ZONE="us-west1-b"  # Free tier 可用区
INSTANCE_NAME="narci-recorder"

echo "============================================"
echo " Narci GCP Deployment"
echo " Project: $PROJECT_ID"
echo " Zone: $ZONE"
echo " Instance: $INSTANCE_NAME"
echo "============================================"

# 1. 创建 e2-micro 实例 (Free Tier)
echo "[1/4] Creating e2-micro instance..."
gcloud compute instances create "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --machine-type=e2-micro \
  --image-family=cos-stable \
  --image-project=cos-cloud \
  --boot-disk-size=30GB \
  --tags=narci-recorder \
  --metadata=startup-script='#!/bin/bash
    # Container-Optimized OS 自带 Docker
    echo "VM started at $(date)" >> /var/log/narci-startup.log
  '

# 2. 开放健康检查端口 (可选，内网访问即可)
echo "[2/4] Configuring firewall..."
gcloud compute firewall-rules create narci-health \
  --allow=tcp:8079 \
  --target-tags=narci-recorder \
  --description="Narci recorder health check" \
  2>/dev/null || echo "Firewall rule already exists, skipping."

# 3. 输出 SSH 命令和后续步骤
EXTERNAL_IP=$(gcloud compute instances describe "$INSTANCE_NAME" \
  --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

echo ""
echo "============================================"
echo " VM 创建完成!"
echo " External IP: $EXTERNAL_IP"
echo "============================================"
echo ""
echo "接下来请 SSH 到实例并完成部署:"
echo ""
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE"
echo ""
echo "然后在实例内执行:"
echo ""
cat << 'INSTRUCTIONS'
  # --- 在 GCP VM 内执行 ---

  # 1. 克隆代码
  git clone https://github.com/zhangenzhi/narci.git
  cd narci

  # 2. 在本地电脑上获取 rclone token (需要浏览器授权):
  #    本地执行: rclone authorize "drive"
  #    授权完成后会输出一个 JSON token, 复制它

  # 3. 创建 .env 文件 (粘贴 token)
  cat > .env << 'EOF'
  RCLONE_GDRIVE_TOKEN={"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}
  EOF

  # 4. 启动录制
  docker compose up -d

  # 5. 检查状态
  docker compose logs -f recorder
  curl http://localhost:8079/health

  # 6. 手动触发同步测试
  docker compose run --rm sync
INSTRUCTIONS
