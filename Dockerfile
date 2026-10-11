# aod-relay —— AOD 取数中转（workbuddy2api-hub 的本地边车）
#
# 构建:  docker build -t aod-relay:latest .
# 运行:  docker run -d --name aod-relay --restart unless-stopped --network host \
#          -e AOD_HUB_URL=http://127.0.0.1:8788 \
#          -e WB_PANEL_PASSWORD='<hub 面板密码>' \
#          -e AOD_RELAY_TOKEN='<手机侧令牌>' \
#          aod-relay:latest
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="aod-relay" \
      org.opencontainers.image.description="AOD 取数中转：手机 <- relay -> workbuddy2api-hub（本地链接）" \
      org.opencontainers.image.source="https://github.com/fffeng99999/aod-relay" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY relay.py /app/relay.py

# 容器内默认绑 0.0.0.0，端口 8790；hub 默认本地回环。
ENV AOD_RELAY_HOST=0.0.0.0 \
    AOD_RELAY_PORT=8790 \
    AOD_HUB_URL=http://127.0.0.1:8788 \
    PYTHONUNBUFFERED=1

EXPOSE 8790

# /aod/health 无鉴权，适合做存活探测（alpine 自带 busybox wget）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null "http://127.0.0.1:${AOD_RELAY_PORT}/aod/health" || exit 1

ENTRYPOINT ["python", "/app/relay.py"]
