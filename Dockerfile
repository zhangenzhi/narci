FROM python:3.11-slim

WORKDIR /app

# 系统依赖: cron + supervisor
RUN apt-get update && \
    apt-get install -y --no-install-recommends cron curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 设置每日 compact cron (UTC 00:05)
RUN echo "5 0 * * * root cd /app && python -u main.py compact --symbol ALL >> /proc/1/fd/1 2>&1" > /etc/cron.d/narci-compact && \
    chmod 0644 /etc/cron.d/narci-compact && \
    crontab /etc/cron.d/narci-compact

# 数据持久化挂载点
VOLUME ["/app/replay_buffer"]

# 健康检查端口
EXPOSE 8079

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8079/health || exit 1

ENTRYPOINT ["bash", "deploy/entrypoint.sh"]
CMD ["record"]
