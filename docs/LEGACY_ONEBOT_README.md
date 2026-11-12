> **历史归档（2026-11-11）**：本文是 OneBot/HTTP webhook 时代的旧版 README，仅用于回归测试参考。
> 当前版本（QQ 官方机器人 × QueQiao 多服务器）请看仓库根目录 `README.md`；
> 默认启动入口为 `python -m mcqq_bridge`，不再使用 `uvicorn mcqq_bridge.main:app`。

# Minecraft ↔ QQ Bridge

一个不安装 Minecraft mod 的、Docker 优先的 Minecraft↔QQ 群互联后端 MVP。它只提供网关和协议适配：Minecraft 侧发送 HTTP webhook，QQ 侧使用 OneBot v11 HTTP API/反向 WebSocket。核心不绑定 NapCat、AstrBot、LLOneBot、QueQiao 或任何 QQ 登录实现。

当前版本适合内网/私网试运行，不等于官方 QQ 机器人合规方案。请使用专用 bot 账号，并遵守 QQ 平台规则。

## 已实现

- FastAPI + asyncio，`/healthz`、`/readyz`、OpenAPI 文档。
- Minecraft 事件：`chat`、`death`、`join`、`leave`、`players`、`status`。
- Minecraft webhook Bearer token 认证（可强制开启）。
- OneBot v11 adapter：向 allowlist 群发送纯文本 `send_group_msg`；实现可调路径的反向 WebSocket 服务端与 API 确认。
- QQ 入站事件：只处理 allowlist 群、只识别 `/mc` 前缀、只允许配置中的 `players`、`status`；默认不允许 `say`。
- `MC→QQ` 的 chat/death/join/leave 独立开关；`QQ→MC` 独立开关。
- QQ→MC 的可选 `say` 仅通过已配置 HTTP callback 发送，绝不执行本地 shell、RCON 或任意服务器命令。
- LLM 人设接口 `PersonalityProvider` 仅为 disabled extension point，没有伪造的模型接入。
- Hermes admin-only HTTP adapter 默认关闭；开启后只调用已运行 Hermes 的官方 OpenAI-compatible API，不启动进程、不读取 profile、不复制 profile 凭据。
- 非 root Docker、read-only filesystem、drop capabilities、healthcheck。

## 快速开始（本地）

```bash
cd /home/czauw/minecraft-qq-bridge
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# 编辑 .env：至少设置 MC_WEBHOOK_TOKEN、ALLOWED_GROUP_IDS；不要提交 .env
uvicorn mcqq_bridge.main:app --reload --host 127.0.0.1 --port 8000
```

文档：`http://127.0.0.1:8000/docs`，OpenAPI：`http://127.0.0.1:8000/openapi.json`。

## Docker Compose

```bash
cp .env.example .env
# 使用 openssl rand -hex 32 生成 token，并在 .env 中填写
openssl rand -hex 32
# 仅把 OneBot API / Minecraft callback 配成实际私网地址
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
```

默认只绑定到 `127.0.0.1:8000`。如需跨主机，建议放在 Caddy/Nginx/TLS、私网 ACL 和限流之后，不要直接暴露未加密 webhook 或 OneBot API。

## API 与 curl 示例

以下假设：`MC_TOKEN` 是 `.env` 中的 token，群号是示例 `123456789`，请替换为自己的值；示例中的 token/QQ 号不是真实凭据。

### Minecraft→QQ

```bash
MC_TOKEN='replace-with-your-token'
BASE='http://127.0.0.1:8000'

curl -i "$BASE/api/v1/minecraft/events/chat" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"player":"Steve","message":"大家好"}'

curl -i "$BASE/api/v1/minecraft/events/death" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"player":"Steve","message":"Steve 被僵尸击败"}'

curl -i "$BASE/api/v1/minecraft/events/join" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"player":"Alex"}'

curl -i "$BASE/api/v1/minecraft/events/leave" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"player":"Alex"}'

curl -i "$BASE/api/v1/minecraft/events/players" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"players":["Steve","Alex"],"max_players":20}'

curl -i "$BASE/api/v1/minecraft/events/status" \
  -H "Authorization: Bearer $MC_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"online":true,"version":"1.21.4","motd":"Survival"}'
```

返回 202 表示事件已被接受；`players` 和 `status` 会更新内存状态，重启后清空。查询：

```bash
curl -fsS "$BASE/api/v1/state/players"
curl -fsS "$BASE/api/v1/state/status"
curl -fsS "$BASE/api/v1/state/meta"
```

### QQ→MC / OneBot v11

将 OneBot 的事件上报到：

```bash
curl -i "$BASE/api/v1/onebot/events" \
  -H 'Authorization: Bearer replace-with-onebot-webhook-token' \
  -H 'Content-Type: application/json' \
  -d '{
    "post_type":"message", "message_type":"group", "group_id":123456789,
    "user_id":10001, "raw_message":"/mc players", "message":"/mc players"
  }'
```

只有 `ALLOWED_GROUP_IDS` 中的群会激活。默认仅允许：

- `/mc players`：查询最近一次 `players` webhook 的内存快照；
- `/mc status`：查询最近一次 `status` webhook 的内存快照。

`MC_COMMANDS` 是显式 allowlist；不要加入 `shell`、`console`、`rcon` 等任意执行语义。`say` 只有同时满足 `MC_COMMANDS` 包含 `say` 且 `MC_SAY_ENABLED=true` 时才会把固定格式 callback 发给 `MC_CALLBACK_URL`。bridge 自己不执行 MC 命令，消费 callback 的服务必须自行鉴权和限制。

OneBot HTTP 返回包含 `accepted` 和 `delivered`：查询命令 `accepted=true` 仅表示已识别并生成回复，只有 `delivered=true` 表示 OneBot API 已确认；这仍不代表用户已读。未知命令/未授权群两者均 false。`say` 的 delivered 表示 callback HTTP 成功，不代表游戏内展示成功。

缓存不会被宣称为实时探测：`/state/status` 初始 `online=null`；`/state/meta` 返回玩家和状态各自的 `updated_at`、`freshness=unknown/fresh/stale`，默认 `STATE_TTL_SECONDS=120`。QQ 回复明确标注未知/缓存/过期快照。join/leave 增量修正名单但不刷新完整 players 快照的时效；收到 offline 状态时清空旧玩家名单。旧版简洁 `/state/players` 字段保持不变，需要判定时效应同时读取 meta。

## OneBot 配置边界

本服务不是 QQ 客户端。外部 OneBot v11 实现负责登录/风控/事件上报：

- 出站：本服务调用 `${ONEBOT_API_URL}/send_group_msg`，携带可选 Bearer token；只对 allowlist 群发送。
- 入站：OneBot 实现把 group message POST 到 `/api/v1/onebot/events`，需要配置 `ONEBOT_WEBHOOK_TOKEN` 或 `ONEBOT_WEBHOOK_SECRET`；不能留空。
`ONEBOT_WS_PATH` 是反向 WebSocket endpoint；OneBot 作为客户端连接，使用 `Authorization: Bearer <onebot-token>`，并按实现要求发送 `X-Client-Role: Universal`（或 `API`/`Event`）以及标准 action frame。MVP 不假设某个实现的 JSON 扩展字段。
- `ONEBOT_SELF_ID` 可用于过滤自己产生的消息，避免环路；生产上仍应在 OneBot/网关侧做事件过滤。

不同 NapCat/AstrBot/LLOneBot 版本的反向 WS 方向和鉴权头可能不同，部署前以所选实现的文档为准，不要直接套用别人的路径。

## 配置

- `.env.example`：环境变量模板；不要填真实 token/QQ 号并提交。
- `config.example.yaml`：同一组概念的可读配置示例；当前运行时使用环境变量，YAML 不会被自动加载。
- `MC_NOTIFICATIONS`：JSON 对象，支持 `chat`、`death`、`join`、`leave` 独立开关。
- `ALLOWED_GROUP_IDS`：逗号分隔群号，默认空即不向任何群发送。
- `QQ_TO_MC_ENABLED`：独立控制 callback；不影响 MC→QQ。

## 安全威胁模型

假设攻击者可以：伪造互联网 HTTP 请求、向已接入 QQ 群发消息、尝试修改事件 JSON、诱导 bot 执行危险命令，或利用错误配置扩大访问面。

防护与边界：

1. Minecraft webhook 默认需要 Bearer token，使用常量时间比较；token 必须通过 secret 管理并使用长随机值。
2. QQ 入口只接受明确的 group message、allowlist 群和 `/mc` 前缀；未知命令拒绝；消息/名称有长度限制。
3. 不存在任意 MC 命令执行、shell 或 RCON 执行路径；Hermes 仅能通过单独配置的官方 HTTP API 适配器调用。
4. OneBot 出站群号再次检查 allowlist；机器人自身消息过滤，降低回环风险。
5. Docker 以非 root、只读根文件系统、丢弃 Linux capabilities 和 no-new-privileges 运行。
6. 状态是内存缓存，不应作为权限或审计依据；callback consumer 必须独立校验 token、来源、群号和消息策略。

Hermes 管理员命令（默认关闭）：在允许群内使用 `/mc hermes <任务>`。只有 `HERMES_ADMIN_IDS` 中的 QQ 号可以触发；未授权、关闭、未配置、超时、上游失败和并发占满均安全失败。任务作为 JSON 字符串通过 `POST /v1/chat/completions` 发送，不经过 shell、CLI、RCON 或进程启动；回复仅取 assistant 文本并再发回原群。Hermes API 具备完整 agent 工具能力（包括终端），因此必须把桥接器放在受控内网，并使用 Hermes 官方 API 的独立 `API_SERVER_KEY`；绝不要把 profile 的 auth、`.env` 或其他凭据复制进容器。若现有 Hermes 没有已启用且可安全访问的 API server，本桥接器不会猜测 9119 dashboard/内部接口，也不会伪造成功。

不在 MVP 范围：QQ 账号登录、官方机器人资质、TLS termination、持久化审计、MC 服务端插件/mod、RCON、用户绑定、LLM 随机回复。不要把本服务端口或 OneBot WebUI 直接暴露公网。

## 测试与 smoke test

```bash
. .venv/bin/activate
pytest -q
uvicorn mcqq_bridge.main:app --host 127.0.0.1 --port 18000
# 另一个终端
curl -fsS http://127.0.0.1:18000/healthz
```

测试覆盖认证、事件状态、群 allowlist、命令 allowlist、QQ→MC 开关/回调和输入长度限制。
