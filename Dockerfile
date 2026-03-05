FROM python:3.11-slim

WORKDIR /app

# 系统依赖: cron + supervisor + rclone
RUN apt-get update && \
    apt-get install -y --no-install-recommends cron curl unzip && \
    curl -fsSL https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 设置每日 cron:
#   00:05 compact 所有币种 (聚合 + 校验 + 归档冷数据 + 清理碎片)
#   01:00 rclone sync 冷数据到 Google Drive
RUN echo "5 0 * * * root cd /app && python -u main.py compact --symbol ALL >> /proc/1/fd/1 2>&1" > /etc/cron.d/narci-jobs && \
    echo "0 1 * * * root rclone sync /app/replay_buffer/cold gdrive:/narci_cold --config /root/.config/rclone/rclone.conf --transfers 4 --log-level INFO >> /proc/1/fd/1 2>&1" >> /etc/cron.d/narci-jobs && \
    chmod 0644 /etc/cron.d/narci-jobs && \
    crontab /etc/cron.d/narci-jobs

# 数据持久化挂载点
VOLUME ["/app/replay_buffer"]

# 健康检查端口
EXPOSE 8079

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8079/health || exit 1

ENTRYPOINT ["bash", "deploy/entrypoint.sh"]
CMD ["record"]
