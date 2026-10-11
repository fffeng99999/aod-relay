# aod-relay 部署速查（服务器 · Docker）

目标链路：

    手机 App ──HTTPS(域名)──> 反向代理 ──> aod-relay:8790 ──本地回环──> hub:8788

## 0. 前置检查（在服务器上执行）

```bash
curl -s http://127.0.0.1:8788/health     # hub 在跑且宿主机能访问（端口按实际改）
docker compose version                   # 有 compose 插件
```

## 1. 取镜像（二选一）

方式 A —— 直接拉 GitHub 构建好的镜像（推荐）：

```bash
docker pull ghcr.io/fffeng99999/aod-relay:latest
```

方式 B —— 源码构建：

```bash
git clone https://github.com/fffeng99999/aod-relay.git
cd aod-relay
docker build -t aod-relay:latest .
```

## 2. 准备目录与配置

```bash
mkdir -p /opt/aod-relay && cd /opt/aod-relay
```

方式 A（镜像）：下载两份配置文件：

```bash
curl -fsSLO https://raw.githubusercontent.com/fffeng99999/aod-relay/main/docker-compose.ghcr.yml
curl -fsSLO https://raw.githubusercontent.com/fffeng99999/aod-relay/main/.env.example
```

方式 B（源码）：`cp -r` 上一步 clone 的目录内容即可（用 `docker-compose.yml`）。

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成手机侧令牌
nano .env    # 填 WB_PANEL_PASSWORD 与 AOD_RELAY_TOKEN
```

## 3. 启动

方式 A：

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

方式 B：

```bash
docker compose up -d --build
```

## 4. 自测

```bash
docker compose logs --tail 20 aod-relay     # 应看到 listening on http://0.0.0.0:8790
curl -s http://127.0.0.1:8790/aod/health
curl -s -H "Authorization: Bearer <AOD_RELAY_TOKEN>" "http://127.0.0.1:8790/aod/usage?range=today"
```

预期：`ok:true`、`source:"hub"`，`lines` 同时包含今天、本周、本月窗口；花费以人民币和美元显示，并与面板「API 等价花费」使用同一统计源。单个字段读取失败时对应位置显示 ❌。

## 5. 域名反向代理

把 `aod.<你的域名>` 反代到 `http://127.0.0.1:8790`（TLS 在反代层终止）。

Caddy：

```caddyfile
aod.example.com {
    reverse_proxy 127.0.0.1:8790
}
```

Nginx：

```nginx
server {
    listen 443 ssl;
    server_name aod.example.com;
    # ssl_certificate / ssl_certificate_key ...;
    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

- 反代跑在 Docker 容器里（NPM / Caddy 容器）时，容器内的 `127.0.0.1` 不是宿主机：
  上游改填宿主机地址（如 `http://192.168.x.x:8790`）或 `http://host.docker.internal:8790`。
- 不要改路径前缀：中转只认 `/aod/usage` 与 `/aod/health`。

## 6. 手机端

App「中转地址」填 `https://aod.<你的域名>`（裸域名会自动补 https），
「中转令牌」填 `.env` 里的 `AOD_RELAY_TOKEN` → 「立即测试取数」→「保存并启动轮询」。

## 更新到最新镜像

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

## 常用维护

```bash
docker compose ps
docker compose logs -f aod-relay
docker compose up -d --build     # 更新 relay.py 后重建
docker compose down              # 停止
```

## 排错

| 现象 | 处理 |
|---|---|
| 启动即退出，日志「拒绝启动：请填入 hub 面板密码」 | `.env` 未填或仍是占位值 |
| `/aod/usage` 502 kind=auth | hub 面板密码不对；改 `.env` 后 `docker compose up -d` |
| 502 kind=net | hub 不可达：宿主机 `curl http://127.0.0.1:8788/health` 先通；hub 在 Docker 未映射端口时，把中转改为与 hub 同网络 |
| 手机 401 | App 令牌与 `.env` 的 `AOD_RELAY_TOKEN` 不一致 |
| 手机连不上域名 | 反代未指向 8790 / 改了路径前缀 / 防火墙 |
| 金额显示 `￥❌ / $❌` | hub 未返回有效价格合计，或面板价格折算已关闭；确认面板「API 等价花费」已启用 |

安全：公网只暴露反代（HTTPS）；令牌要轮换时改 `.env` 重启容器 + App 同步改。
