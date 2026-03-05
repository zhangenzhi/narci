FROM python:3.11-slim

WORKDIR /app

# 系统依赖: curl (健康检查) + rclone (落盘后即时推送至 Google Cloud)
# 注意: 云服务器仅负责录制 + 推送，compact/validate 请在本地执行
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl unzip && \
    curl -fsSL https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据持久化挂载点
VOLUME ["/app/replay_buffer"]

# 健康检查端口
EXPOSE 8079

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8079/health || exit 1

ENTRYPOINT ["bash", "deploy/entrypoint.sh"]
CMD ["record"]
