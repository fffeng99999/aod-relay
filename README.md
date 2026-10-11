# aod-relay · AOD 取数中转（网关 / 堡垒服务）

部署在 **workbuddy2api-hub 所在服务器**上的只读边车（sidecar / gateway）。
手机上的 [AOD Bridge APK](https://github.com/fffeng99999/aod-bridge-apk) 只面对本服务一条极简只读接口，
hub 的面板凭据与登录细节全部留在服务器本地，永不进入手机。

```
手机 App ──HTTPS(域名反向代理)──> aod-relay :8790 ──本地回环──> workbuddy2api-hub :8788
```

- 手机侧只持有专用令牌（`AOD_RELAY_TOKEN`），可随时轮换；
- 中转与 hub 走服务器本地回环（`127.0.0.1`），hub 不对公网暴露；
- 上游通过 `POST /panel/login` 换取面板会话，再并发读取 `/usage/analytics?realm=all&range=today|week|month`；每个窗口分别使用 ETag/304，401 时自动重登。数据直接取自 workbuddy2api-hub v1.6.20 dashboard 的 `summary.window`。
- **字段级失败**：某个数据在 hub 侧取不到（字段缺失 / 数值异常）时，该字段名进 JSON 的
  `failed` 字典，文本输出与 AOD 对应位置显示 ❌，其余字段照常显示。

## 快速开始（服务器 · Docker）

### 方式 A：拉取 GitHub 构建好的镜像（推荐，更新最快）

镜像发布在 GitHub Container Registry（GHCR），任意联网机器可直接拉取：

```
ghcr.io/fffeng99999/aod-relay:latest
```

```bash
mkdir -p /opt/aod-relay && cd /opt/aod-relay
curl -fsSLO https://raw.githubusercontent.com/fffeng99999/aod-relay/main/docker-compose.ghcr.yml
curl -fsSLO https://raw.githubusercontent.com/fffeng99999/aod-relay/main/.env.example
cp .env.example .env      # 填 WB_PANEL_PASSWORD 与 AOD_RELAY_TOKEN，见下
docker compose -f docker-compose.ghcr.yml up -d
```

更新：

```bash
docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d
```

### 方式 B：本地源码构建

```bash
git clone https://github.com/fffeng99999/aod-relay.git
cd aod-relay
cp .env.example .env
docker compose up -d --build
```

启动后自测：

```bash
curl -s http://127.0.0.1:8790/aod/health
curl -s -H "Authorization: Bearer <AOD_RELAY_TOKEN>" "http://127.0.0.1:8790/aod/usage?range=today"
```

## 配置（.env）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `WB_PANEL_PASSWORD` | ✅ | — | hub 面板密码（**只在服务器上**） |
| `AOD_RELAY_TOKEN` | ✅ | — | 给手机的专用令牌，长随机串：`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AOD_HUB_URL` | — | `http://127.0.0.1:8788` | hub 地址（中转与 hub 同机时保持回环） |
| `AOD_RELAY_HOST` / `AOD_RELAY_PORT` | — | `0.0.0.0` / `8790` | 监听地址 / 端口 |
| `AOD_COST_IGNORE_MISSING` | — | 关 | 已定价模型的等价费用照常显示；此开关只兼容旧版 relay |

`cost_cny` 是面板中“API 等价花费”的人民币合计，`usd_cny` 来自同一统计载荷的美元汇率；手机显示人民币/美元两种金额。`cost_missing` 只列出无价模型，不会屏蔽已计价模型的合计。

命令行等价参数：`--host/--port/--hub-url/--panel-password/--relay-token/--cost-ignore-missing/--mock/--mock-fail`。

## 对外接口（手机侧）

| 接口 | 说明 |
|---|---|
| `GET /aod/usage` | 精简 JSON（`text` + 各字段 + `failed`） |
| `GET /aod/usage?range=today\|week\|month` | 兼容旧客户端；新协议单次返回三个窗口 |
| `GET /aod/usage?format=text` | `text/plain` 一行短文本，便于浏览器 / curl 直测 |
| `GET /aod/health` | 存活探测（**无鉴权**，供反代与容器健康检查） |

鉴权：`Authorization: Bearer <AOD_RELAY_TOKEN>`，或 `?token=<AOD_RELAY_TOKEN>`（手机浏览器不便带自定义头）。
令牌不对返回 `401`；上游失败返回 `502`，body 里带 `kind=auth|net|api|data`。

## 本地联调（不连 hub）

```bash
python relay.py --mock --relay-token devtoken123            # 内置 mock 数据
python relay.py --mock --mock-fail cost_cny,errors --relay-token devtoken123
python relay.py --mock --host 0.0.0.0 --port 8790 --relay-token devtoken123   # 局域网给手机用
```

自测（CI 也会跑）：

```bash
python3 selftest.py          # 取数层 fetch_usage.py（5 项）
python3 selftest_relay.py    # 中转 relay.py（9 项，含字段级失败/❌）
```

## 目录

| 文件 | 说明 |
|---|---|
| `relay.py` | 中转服务本体（单文件、零依赖，Python 3.8+） |
| `mock_hub.py` | hub 的本地 mock（联调 / 自测用） |
| `fetch_usage.py` | 取数原型脚本（直连 hub 或经中转，`--watch` 定时轮询） |
| `selftest.py` / `selftest_relay.py` | 自测 |
| `Dockerfile` / `.dockerignore` | 镜像构建 |
| `docker-compose.yml` | 源码构建编排（host 网络，回环连 hub） |
| `docker-compose.ghcr.yml` | 预构建镜像编排（pull 即更新） |
| `DEPLOY.md` | 服务器部署速查（上传/配置/反代/排错） |
| `start-mock-lan.ps1` / `.cmd` | Windows 一键起 mock 中转（局域网给手机联调） |

## 镜像发布（GitHub Actions）

`.github/workflows/docker.yml`：

1. `test`：在 ubuntu 上跑两份自测；
2. `smoke`：本机构建 amd64 镜像，起容器验证 `/aod/health`、鉴权与错误码；
3. `build-push`：构建 **linux/amd64 + linux/arm64** 多架构镜像并推 GHCR：
   - 默认分支推送 → `:latest`
   - 打 `vX.Y.Z` 标签 → `:X.Y.Z`、`:X.Y`
   - 每次提交 → `:sha-<短sha>`

> 首次发布后如镜像是私有的：仓库 → Packages → `aod-relay` → Package settings → Change visibility → Public，
> 之后任意联网机器无需登录即可 `docker pull`。

## 安全

- 公网只暴露反向代理（HTTPS）；中转端口本身也不建议直接对公网开放；
- hub 面板密码只出现在服务器 `.env` 与中转进程内，手机只有中转令牌；
- 轮换令牌：改 `.env` → 重启容器 → App 里同步改。

## License

MIT，见 [LICENSE](LICENSE)。
