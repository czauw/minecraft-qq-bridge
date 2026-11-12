> **⚠️ 已过时（2026-11-11）**：本文描述的是旧 OneBot/HTTP webhook 拓扑。
> 现行默认入口为 `python -m mcqq_bridge --config config.yaml`（QQ 官方 Bot + QueQiao 多服务器），
> 请看根目录 `README.md` §10 与 `docker-compose.yml`。

# 部署说明

## 边界

Minecraft 本身不会自动 POST webhook，也不会自动消费 HTTP callback。本仓库不安装 mod、不登录 QQ、不主动读取 Minecraft 日志、不执行 RCON。你需要已有的服务端面板、事件代理、日志 sidecar 或插件（若服务器允许）提供 HTTP 事件源/消息消费者；无 mod 不代表原版 MC 原生支持这些 HTTP 端点。没有 MC 侧事件源时，可先用 README curl 验证后端。

拓扑：

    MC 事件源 -- Bearer HTTP --> bridge --> OneBot HTTP API --> QQ 群
    MC 消费者 <-- Bearer callback -- bridge <-- OneBot HTTP POST / 反向 WS

## Compose

1. 复制 `.env.example` 为 `.env`，`chmod 600 .env`。示例群号仅是占位值，不是真实群。
2. 替换所有 `replace-*` token，`ALLOWED_GROUP_IDS` 写实际授权群；默认 `ONEBOT_ENABLED=false`，默认不产生真实 QQ 出站。
3. 二选一：
   - HTTP：`ONEBOT_TRANSPORT=http`，配置 `ONEBOT_API_URL` 和 `ONEBOT_ACCESS_TOKEN`；上报配置见下一节。
   - reverse WS：`ONEBOT_TRANSPORT=websocket`，`ONEBOT_ACCESS_TOKEN` 必须非空；OneBot 作为客户端连接 `ws://bridge:8000/api/v1/onebot/ws`（同一 Docker 网络内）。
4. `sudo docker compose up -d --build`；`sudo docker compose ps`；`curl -fsS http://127.0.0.1:8000/healthz`。
5. 查看 `sudo docker compose logs --tail=100 bridge`。停止：`sudo docker compose down`。

bridge 默认只映射宿主 `127.0.0.1:8000`。别的容器不能用 `127.0.0.1` 指向宿主或 bridge，应使用同一 Docker 网络的服务名。外部 OneBot 不在本 Compose 内，`onebot` 这个示例主机名不会凭空解析；请改为可达私网 IP，或把服务接入同一 Docker network。宿主上的服务可改用 `host.docker.internal` 并配置 Linux host-gateway，但注意宿主服务监听地址和防火墙。

## OneBot v11 HTTP

标准 OneBot HTTP POST 上报使用 `ONEBOT_WEBHOOK_SECRET`，对应 OneBot 端 `http_post.secret`：服务验证原始 HTTP body 的 `X-Signature: sha1=...` HMAC。若使用该方式，清空 `ONEBOT_WEBHOOK_TOKEN`，否则会同时要求 Bearer。

支持自定义上报 header 的实现也可用 `ONEBOT_WEBHOOK_TOKEN` 加 `Authorization: Bearer <webhook-token>`。`ONEBOT_ACCESS_TOKEN` 是 API/WS 鉴权，和 HTTP webhook secret 不是同一字段。

上报地址 `http://bridge:8000/api/v1/onebot/events`；API 基址示例 `http://onebot:3000`。这些是协议角色示例，不承诺任何 NapCat/AstrBot 版本的配置键。参考所选 OneBot 实现的实际文档。

## OneBot v11 reverse WebSocket

OneBot 启用反向 WS，配置 Universal URL 为 `ws://bridge:8000/api/v1/onebot/ws`，access token 与 `ONEBOT_ACCESS_TOKEN` 一致，发送 `Authorization: Bearer ...`。`ONEBOT_WS_PATH` 可调。推荐 Universal 单连接；也支持 `X-Client-Role: API` 和 `Event` 分离连接，但不与 Universal 混用，拒绝重复 role，当前只支持一个 bot。

断线重连由 OneBot 客户端配置，本后端不主动连接 OneBot WS。发送 action 时带 `echo`，只在匹配 `status=ok`、`retcode=0` 响应后认为传输成功；超时默认 5 秒。API in-flight / 每连接事件并发有上限。不应同时启用同一事件的 HTTP 与 WS 上报，否则可能重复。

## 已有 Hermes 管理员调用（默认关闭）

本桥接器不会安装、启动或复制 Hermes。官方文档说明，已有 Hermes 的 API server 使用 OpenAI-compatible `POST /v1/chat/completions`，并要求独立 `API_SERVER_KEY` Bearer 鉴权；API server 具备 agent 工具能力，包含终端，因此只能通过受控私网访问。

启用前必须由运维在现有 Hermes 实例上完成 API server 配置，并把独立 key 以 bridge 环境变量 `HERMES_API_TOKEN` 注入；不要挂载 Hermes profile、`auth.json`、`.env`、skills 或 memory。bridge 只接受允许群内 `HERMES_ADMIN_IDS` 中 QQ 号的 `/mc hermes <任务>`，任务通过 JSON HTTP 请求发送，回复只取 assistant 文本发回原群；默认关闭、超时 30 秒、并发上限 1、输入/输出有长度上限。

当前本机 Hermes dashboard 在 9119，但没有检测到官方 API server 在 8642，且 bridge 不会把 dashboard 或内部 WebSocket 当作 API 猜测连接。因此在启用 API server、配置独立 key、并由运维确认私网 ACL 前，Hermes 真实联调未完成；失败会明确返回未投递，不伪造回复。

## QQ→MC

`QQ_TO_MC_ENABLED=true`，`MC_COMMANDS=players,status,say`，`MC_SAY_ENABLED=true` 后，在授权群发 `/mc say 内容`。只有 `say` 会触发固定 `chat` 回调；普通群聊天不自动转发，以减少刷屏和误操作。

回调 POST `MC_CALLBACK_URL`，使用 `Authorization: Bearer <callback-token>`；示例 JSON：

    {"event":"chat","message":"内容","group_id":123456789,"user_id":10001,"nickname":"示例用户"}

MC 消费者只能把它作为聊天广播输入，必须再次验证 token、字符长度、展示转义；绝不能把 message 拼进控制台/shell 命令。callback 目标由运维配置，用户消息不能改变 URL；不跟随重定向。跨主机必须 HTTPS/私网 ACL。默认 `say` 关闭。

## 健康与升级

`/healthz` 是进程存活，`/readyz` 是配置/adapter 信息，不代表 QQ 登录成功或真实 MC 在线。状态查询只反映最近收到的内存快照。建议只运行一个 uvicorn worker / 一个 replica（无共享状态）；重启会清空缓存。

使用 `/api/v1/state/meta` 查看更新时间和 unknown/fresh/stale。默认 `STATE_TTL_SECONDS=120`；初始服务器状态是未知而非离线。离线状态会清空旧玩家缓存，join/leave 增量事件不证明完整玩家快照新鲜。OneBot HTTP 响应的 accepted 是安全分发结果，delivered 是独立的 adapter 确认结果；失败 retcode 会记录不含消息/凭据的告警。

更新前保留镜像标签和 .env 备份，运行测试与 smoke，再构建；回滚到前一镜像。没有数据库迁移。依赖版本在 pyproject 中固定，传递依赖仍可能更新，生产发布应锁定镜像 digest 并定期扫描 CVE。

## 实测与未验证

本地验证命令：`.venv/bin/python -m pytest -q`、`.venv/bin/python scripts/smoke.py`。smoke 使用本地模拟 OneBot/MC peer 进行真实 TCP 通信，不代表真实 QQ 与游戏内联调。具体结果见 `docs/VERIFICATION.md`。
