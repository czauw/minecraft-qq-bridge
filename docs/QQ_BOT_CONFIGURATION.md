> **⚠️ 已过时（2026-11-11）**：本轮新增了 `config.yaml`（`bot`/`servers`/`relay`/`runtime`/`logging`），
> 与本文描述的 `.env` 键不是同一套。现行配置字段以 `config.example.yaml` 与根目录 `README.md` 为准；
> 另外：**新入口 `python -m mcqq_bridge` 不会自动读取 `.env`**（本文第 2 部分描述的 `Settings(env_file=".env")`
> 只属于历史 `mcqq_bridge.settings` 模块）；新入口需要 `--env-file .env` 或 shell `export`。
> 验证结果见 `docs/VERIFICATION.md`。

# 官方 QQ 群机器人配置文档（MCQQ Bridge）

> **本文分成两个互不混淆的部分：**
> **第 2 部分 = 当前已经支持、可运行的配置（改了就生效，有代码读取）。**
> **第 3 部分 = 拟新增的官方 Bot 配置（⛔ 当前不可用：没有代码读取，写进 `.env` 既不报错也不生效）。**
> 平台资料查阅日期：2026-11-10（UTC）；仓库快照：`/home/czauw/minecraft-qq-bridge`（untracked，`master` 无提交）。
> 事实基线：`docs/research/official-qq-current-state.md`（下称 [R1]）、`docs/research/official-qq-platform-and-architecture.md`（下称 [R2]）。接入流程见 `docs/QQ_BOT_INTEGRATION.md`。

**图例**

| 标记 | 含义 |
|---|---|
| ✅ | 当前代码会读取，改配置立即/重启后生效 |
| 🟡 | 当前代码声明了字段但**从不读取**（死配置，容易误导） |
| ⛔ | 拟新增，**当前不可用**；本文只是语义草案 |
| 🔒 | 敏感值（密钥/令牌），不得进入仓库、日志、截图、群消息 |

**本文的硬约束**（如果只想记一条）：`QQ_OFFICIAL_*` 这类键现在写进 `.env` **不会报错也不会生效**——`Settings` 用 `extra="ignore"`（`src/mcqq_bridge/settings.py:11`），未知环境变量被静默忽略。**「服务启动成功」不能用来判断新配置是否生效。**

---

## 1. 配置是怎么加载的（代码事实）

| 事实 | 证据 |
|---|---|
| `Settings(BaseSettings)`，`env_file=".env"`、`env_file_encoding="utf-8"`、`extra="ignore"` | `settings.py:10-11` |
| 优先级：**进程环境变量 > 当前工作目录下的 `.env` > 类默认值** | pydantic-settings 语义；`settings.py:76` 在导入时实例化 |
| 因此 `.env` 的位置取决于 **CWD**：从仓库根启动才读到根目录 `.env` | `README.md:20-30`；`Dockerfile` 的 `WORKDIR` |
| `config.example.yaml` **不参与运行时**：`grep -rniI "yaml" src tests scripts --include=*.py` = **0** | [R1] §5.1，本轮复测 0 |
| 声明字段总数 **35**（实测 `Settings.model_fields`）；其中 8 个是零引用死配置 | 本轮实测，见 §2.6 |
| 未知环境变量/未知 `.env` 键被忽略，不抛错 | `extra="ignore"`；本轮探针实测 |
| Docker Compose 通过 `env_file: .env` 注入，容器内进程环境优先 | `docker-compose.yml:6-9` |
| Compose 的 `env_file` 默认是**必需**的：仓库根缺 `.env` 时 `docker compose up` 会直接报错（首次部署先 `cp .env.example .env`） | `docker-compose.yml:6-7` |

**验证配置是否真的进入进程（只读、无副作用）：**

```bash
cd /home/czauw/minecraft-qq-bridge
.venv/bin/python -m pytest -q -W error          # 期望 53 passed
# 打印生效值（不要在有真实凭据的环境里对外粘贴输出）
.venv/bin/python /path/to/settings_probe.py     # 见 §10.2
```

---

## 2. 【第一部分】当前已经支持且可运行的配置

### 2.1 运行标识与监听

| 环境变量 | 字段 | 类型 | 默认 | 必填 | 敏感性 | 代码读取位置 | 说明 |
|---|---|---|---|---|---|---|---|
| `APP_NAME` | `app_name` | str | `minecraft-qq-bridge` | 否 | 低 | 🟡 **零引用** | 仅声明，不参与 FastAPI title 或日志 |
| `LOG_LEVEL` | `log_level` | str | `INFO` | 否 | 低 | 🟡 **零引用** | 改它不会改日志级别；实际由 uvicorn CLI 控制 |
| `HOST` | `host` | str | `0.0.0.0` | 否 | 低 | 🟡 **零引用** | 实际监听地址由 `uvicorn --host` / Dockerfile CMD 决定 |
| `PORT` | `port` | int (1–65535) | `8000` | 否 | 低 | 🟡 **零引用** | 同上；改这里不会改端口 |

> 这四项是「看起来能配、实际无效」的死配置（[R1] R12）。部署时请直接改 uvicorn 参数/Compose 端口映射，并在变更记录里写清楚，避免误判。

### 2.2 Minecraft 入站与状态

| 环境变量 | 字段 | 类型 | 默认 | 必填 | 敏感性 | 读取位置 | 说明 |
|---|---|---|---|---|---|---|---|
| `MC_WEBHOOK_TOKEN` | `mc_webhook_token` | str | `""` | 是（生产） | 🔒 | `main.py:68-70` | Bearer 校验，常量时间比较；空值 + 要求校验 ⇒ 全部 401（fail-closed） |
| `MC_WEBHOOK_REQUIRE_TOKEN` | `mc_webhook_require_token` | bool | `true` | 否 | 低 | `main.py:69` | **设为 `false` 等于完全无鉴权**，任何能访问端口的人都能伪造 MC 事件 |
| `STATE_TTL_SECONDS` | `state_ttl_seconds` | float (1–86400) | `120` | 否 | 低 | `state.py:14,46` | 超过该时长未更新即标 `stale`；初始为 `unknown`，不会谎称离线 |
| `MC_NOTIFICATIONS` | `mc_notifications` | dict[str,bool] | `{"chat":true,"death":true,"join":true,"leave":true}` | 否 | 低 | `service.py:29` | 四类 MC→QQ 通知开关；缺键按 `false`；`players`/`status` 本身不广播 |

补充事实：
- `players`/`status` 事件只更新内存快照，不产生 QQ 消息（`service.py:21-24`）。
- 收到 `status.online=false` 会清空玩家名单（`state.py:30-31`）；join/leave 增量修正名单但**不刷新** players 快照时效（`state.py:33-40`）。
- 状态查询接口 `/api/v1/state/*` **无鉴权**（`main.py:167-177`），只应内网可达。

### 2.3 群白名单与 OneBot 出站/入站

| 环境变量 | 字段 | 类型 | 默认 | 必填 | 敏感性 | 读取位置 | 说明 |
|---|---|---|---|---|---|---|---|
| `ALLOWED_GROUP_IDS` | `allowed_group_ids` | list[int] | `[]` | 是（启用 QQ 时） | 中 | `service.py:33,55`、`onebot.py:28,115` | 逗号分隔或 JSON 数组；**空 = 不向任何群发、不激活任何入站群** |
| `ONEBOT_ENABLED` | `onebot_enabled` | bool | `false` | 否 | 低 | `main.py:126`、`onebot.py:28,115` | 关掉后**出站恒 false**，但 HTTP 入站仍会被处理（实测 `accepted=true, delivered=false`） |
| `ONEBOT_TRANSPORT` | `onebot_transport` | `http` 或 `websocket` | `http` | 否 | 低 | `main.py:36` | 只决定**出站**用哪个 adapter；两条入站路由同时存在（`main.py:100,124`） |
| `ONEBOT_API_URL` | `onebot_api_url` | str | `http://127.0.0.1:3000` | 是（http 模式） | 低 | `onebot.py:35` | 实际请求 `${URL}/send_group_msg` |
| `ONEBOT_ACCESS_TOKEN` | `onebot_access_token` | str | `""` | 条件 | 🔒 | `onebot.py:31-32`、`main.py:126-128` | HTTP 出站 Bearer；反向 WS 模式下同时用于校验客户端 |
| `ONEBOT_WEBHOOK_TOKEN` | `onebot_webhook_token` | str | `""` | 二者之一 | 🔒 | `main.py:103-108` | HTTP 上报的 Bearer |
| `ONEBOT_WEBHOOK_SECRET` | `onebot_webhook_secret` | str | `""` | 二者之一 | 🔒 | `main.py:109-117` | HMAC-SHA1 验签（`X-Signature: sha1=<40hex>`） |
| `ONEBOT_SELF_ID` | `onebot_self_id` | int 或 null | `null` | 否 | 中 | `service.py:57` | 过滤自身消息防回环；留空则用事件里的 `self_id` |
| `ONEBOT_WS_PATH` | `onebot_ws_path` | str | `/api/v1/onebot/ws` | 否 | 低 | `main.py:124` | **启动时绑定**，改后必须重启 |
| `ONEBOT_API_TIMEOUT_SECONDS` | `onebot_api_timeout_seconds` | float (0.1–60) | `5` | 否 | 低 | `onebot.py:22,124` | HTTP 客户端超时 / WS echo 等待超时；超时按未投递处理 |

补充事实：
- 入站 OneBot 的 `token` 与 `secret` **至少要配一个**，否则 `POST /api/v1/onebot/events` 恒 401（`main.py:103-104`）。
- 群白名单在**入口和出站各校验一次**（`service.py:55`、`onebot.py:28`），两道都过才会发。
- `ONEBOT_WS_PATH` 不是运行期可变项，热改无效。

### 2.4 QQ → MC 回调

| 环境变量 | 字段 | 类型 | 默认 | 必填 | 敏感性 | 读取位置 | 说明 |
|---|---|---|---|---|---|---|---|
| `QQ_TO_MC_ENABLED` | `qq_to_mc_enabled` | bool | `true` | 否 | 低 | `mc_adapter.py:23`、`service.py:85` | 关闭后 `say` 直接拒绝 |
| `MC_CALLBACK_URL` | `mc_callback_url` | str | `http://127.0.0.1:25580/bridge/qq` | 是（用 say 时） | 中 | `mc_adapter.py:29` | 固定目标，**用户消息不能改它**；实测该端口当前无监听（[R1] U5） |
| `MC_CALLBACK_TOKEN` | `mc_callback_token` | str | `""` | 是 | 🔒 | `mc_adapter.py:26-27` | Bearer；为空则不带认证头（消费方必须自行校验） |
| `MC_COMMANDS` | `mc_commands` | list[str] | `["players","status"]` | 否 | 低 | `service.py:76` | 逗号分隔或 JSON；自动小写；**不含的命令一律拒绝** |
| `MC_SAY_ENABLED` | `mc_say_enabled` | bool | `false` | 否 | 低 | `service.py:84` | 需同时把 `say` 放进 `MC_COMMANDS` 才生效 |

补充事实：
- `/mc` 必须是消息的**完整首 token**（`/mcfoo` 不匹配，`service.py:70`）。
- `say` 只发固定 JSON 到固定 URL，不执行 shell/RCON/控制台（`mc_adapter.py:22-34`）。
- `hermes` 分支在 `mc_commands` 检查**之前**执行（`service.py:74-77`）——即 `MC_COMMANDS` 里没有 `hermes` 也能触发，真正闸门是 `HERMES_ADMIN_ENABLED` + `HERMES_ADMIN_IDS`（[R1] R11）。

### 2.5 Hermes 管理通道（默认关闭）

| 环境变量 | 字段 | 类型 | 默认 | 必填 | 敏感性 | 读取位置 | 说明 |
|---|---|---|---|---|---|---|---|
| `HERMES_ADMIN_ENABLED` | `hermes_admin_enabled` | bool | `false` | 否 | 低 | `main.py:38`、`service.py:100`、`hermes.py:46` | 关闭时用 `DisabledHermesExecutor`，恒拒绝 |
| `HERMES_ADMIN_IDS` | `hermes_admin_ids` | list[int] | `[]` | 是（启用时） | 中 | `service.py:102`、`hermes.py:48` | 整数 QQ 号白名单 |
| `HERMES_API_URL` | `hermes_api_url` | str | `http://127.0.0.1:8642/v1` | 是 | 中 | `hermes.py:39-40,67` | 实际请求 `${URL}/chat/completions`；必须是 http(s) 且有 host |
| `HERMES_API_TOKEN` | `hermes_api_token` | str | `""` | 是 | 🔒 | `hermes.py:50-51,60` | 独立 `API_SERVER_KEY`；**不得复用 profile 凭据** |
| `HERMES_MODEL` | `hermes_model` | str | `hermes-agent` | 否 | 低 | `hermes.py:56` | 请求体 `model` |
| `HERMES_TIMEOUT_SECONDS` | `hermes_timeout_seconds` | float (0.1–300) | `30` | 否 | 低 | `hermes.py:36` | 超时按「未投递」返回，不伪造回复 |
| `HERMES_MAX_CONCURRENCY` | `hermes_max_concurrency` | int (1–16) | `1` | 否 | 低 | `hermes.py:62` | 占满即拒绝（`concurrency_limit`） |
| `HERMES_MAX_PROMPT_CHARS` | `hermes_max_prompt_chars` | int (1–10000) | `2000` | 否 | 低 | `hermes.py:52`、`service.py:104` | 超长拒绝 |
| `HERMES_MAX_RESPONSE_CHARS` | `hermes_max_response_chars` | int (1–20000) | `6000` | 否 | 低 | `hermes.py:95` | 回复截断 |

补充事实：本机 `127.0.0.1:8642` 当前无监听（[R1] E11），因此即使打开开关也无法真正调用成功。Hermes 具备终端能力，桥接器必须放在受控内网。

### 2.6 占位配置（声明但零引用）

| 环境变量 | 字段 | 默认 | 状态 |
|---|---|---|---|
| `LLM_ENABLED` | `llm_enabled` | `false` | 🟡 零引用 |
| `LLM_PERSONA` | `llm_persona` | `""` | 🟡 零引用 |
| `LLM_RANDOM_REPLY_PROBABILITY` | `llm_random_reply_probability` | `0.0` | 🟡 零引用 |

`personality.py:6-12` 是恒返回 `None` 的占位实现。**开启这些键不会接入任何模型。**

### 2.7 当前配置的实际能力边界

| 问题 | 当前答案 | 依据 |
|---|---|---|
| 能向真实 QQ 群发消息吗？ | 不能：无 OneBot 实现、`:3000` 无监听、`ONEBOT_ENABLED=false` | [R1] E9/U3 |
| 能接真实官方 Bot 吗？ | 不能：无 adapter、无 AppID | [R1] E7 |
| 能收 MC 事件吗？ | 能（HTTP 端点可用），但**没有任何 MC 侧组件推送** | [R1] U4 |
| MC→QQ 发送失败可见吗？ | 不可见：返回值被丢弃，MC 侧恒 202 | `service.py:31-34`、`main.py:98` |
| 重启后状态还在吗？ | 不在：纯内存、建议单 worker | `state.py:9-18`、`docs/DEPLOYMENT.md:58` |

### 2.8 最小可运行示例（✅ 原样可加载、可启动；业务开关全部保持安全关闭）

> 本节示例的每个值要么留空、要么是**能通过类型校验**的安全默认值：`ALLOWED_GROUP_IDS=[]`（空群白名单）、`ONEBOT_ENABLED=false`（QQ 出站关闭）、全部密钥留空（fail-closed）。按原样复制即可启动。
>
> **待填的数字群号只能写在注释里。** `ALLOWED_GROUP_IDS=<NUMERIC_GROUP_ID>` 这种占位字符串会让 `Settings` 在**导入时**抛 `ValidationError: invalid literal for int() with base 10`（`settings.py:68-73`），uvicorn 直接起不来——那不是"安全姿态"，而是启动失败。该字段接受两种写法：整数 JSON 数组 `[123456789]`，或逗号分隔整数 `123456789,987654321`。

```dotenv
# 复制为 .env 后 chmod 600。
# 安全默认：群白名单为空、QQ 出站关闭、密钥全空（空 = fail-closed，见下方说明）。
# 真实值由受控终端写入（§4），不要粘贴进文档/聊天/工单。
MC_WEBHOOK_TOKEN=
MC_WEBHOOK_REQUIRE_TOKEN=true
STATE_TTL_SECONDS=120
MC_NOTIFICATIONS={"chat":true,"death":true,"join":true,"leave":true}

# 群白名单：必须是整数（JSON 数组或逗号分隔）。留空 = 不向任何群发送、不激活任何入站群。
# 接入真实群时按下面任一形式填写数字群号（取消注释并替换）：
# ALLOWED_GROUP_IDS=[123456789]
# ALLOWED_GROUP_IDS=123456789,987654321
ALLOWED_GROUP_IDS=[]
ONEBOT_ENABLED=false
ONEBOT_TRANSPORT=http
ONEBOT_API_URL=http://127.0.0.1:3000
ONEBOT_ACCESS_TOKEN=
ONEBOT_WEBHOOK_TOKEN=
ONEBOT_WEBHOOK_SECRET=

QQ_TO_MC_ENABLED=true
MC_CALLBACK_URL=http://127.0.0.1:25580/bridge/qq
MC_CALLBACK_TOKEN=
MC_COMMANDS=players,status
MC_SAY_ENABLED=false

HERMES_ADMIN_ENABLED=false
HERMES_ADMIN_IDS=
HERMES_API_URL=http://127.0.0.1:8642/v1
HERMES_API_TOKEN=
```

**生产前必须替换的项**（其余保持默认即可起服务；改动都是"填值"，不改键名）：

| 键 | 填写方式 | 不填的后果 |
|---|---|---|
| `MC_WEBHOOK_TOKEN` | 用 `openssl rand -hex 32` 生成后写入 `.env`（不经命令行参数回显） | 留空 ⇒ MC 事件接口对**任何**请求 401（fail-closed） |
| `ALLOWED_GROUP_IDS` | 真实数字群号（JSON 数组/逗号分隔整数） | 留空 ⇒ 不向任何群发送 |
| `MC_CALLBACK_URL` / `MC_CALLBACK_TOKEN` | 真实 MC 消费者地址与其 Bearer | `say` 不可用（且 `MC_SAY_ENABLED=false` 时不发） |
| `ONEBOT_*` | 仅在真正接入 OneBot 时才填，并把 `ONEBOT_ENABLED=true` | 出站恒 false（HTTP 入站另需 `ONEBOT_WEBHOOK_TOKEN` 或 `ONEBOT_WEBHOOK_SECRET`） |

启动与自检：

```bash
cd /home/czauw/minecraft-qq-bridge
. .venv/bin/activate
uvicorn mcqq_bridge.main:app --host 127.0.0.1 --port 8000
curl -fsS http://127.0.0.1:8000/healthz            # {"status":"ok"}
curl -fsS http://127.0.0.1:8000/readyz             # {"status":"ready","onebot_enabled":false}
.venv/bin/python -m pytest -q -W error             # 53 passed
```

> **启动后能通什么、不能通什么（本轮实测）**：`/healthz`、`/readyz` 不读任何凭据，恒 200。MC 事件入口 `/api/v1/minecraft/events/*` 在 `MC_WEBHOOK_TOKEN` 留空时对**所有**请求 401——`bearer_matches()` 对空期望值恒返回 `False`（`security.py:6-10`）。
>
> **请求是否 401，取决于请求携带的 `Authorization: Bearer <token>` 是否与配置值逐字节相同，而不取决于配置值"看起来像不像占位符"。** 实测：同一份配置把 `MC_WEBHOOK_TOKEN` 设成任意值后，带**该值**的请求返回 202，带错值或不带的仍 401。因此不要用"占位符"解释 401，也不要以为填了任意字符串就"必定 401"。

---

## 3. 【第二部分】拟新增官方 Bot 配置（⛔ 当前不可用）

### 3.0 先读这段

- 本节的键**没有任何代码读取**；`Settings` 里不存在 `qq_official*` 字段（本轮实测：`model_fields` 中匹配 0 项）。
- 写进 `.env` 或进程环境后：**不报错、不生效、无提示**（`extra="ignore"`）。
- 本节字段名/默认值是实现草案，**实现阶段可以改名**；改了就同步改本文，不要让文档与代码漂移。
- 命名方案：环境变量 `QQ_OFFICIAL_*` ↔ Settings 字段 `qq_official_*`（pydantic-settings 大小写不敏感映射）；`QQ_PROVIDER` 是唯一的 provider 选择器。
- 与 [R2] §10 的对应：`qq_provider` → `QQ_PROVIDER`；`qq_official.app_id` → `QQ_OFFICIAL_APP_ID`；`qq_official.callback_path` → `QQ_OFFICIAL_CALLBACK_PATH`；`qq_official.allowed_group_openids` → `QQ_OFFICIAL_ALLOWED_GROUP_OPENIDS`；`qq_official.rate_policy` 拆成 §3.4 的四个限流键。

**「来源」列的含义**：`官方` = 数值/语义来自官方文档（给出编号）；`设计` = 本项目策略，官方未规定，**默认值必须由实测/运维确认**。

### 3.1 provider 与总开关

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_PROVIDER` | `qq_provider` | 出站消息投递到哪个平台 | 枚举：`onebot` 或 `qq_official` | 是（迁移时） | `onebot` | 低 | 设计（[R2] §10；单选防双发） |
| `QQ_OFFICIAL_ENABLED` | `qq_official_enabled` | 是否启动官方 adapter（连接/路由） | bool | 否 | `false` | 低 | 设计 |

> 语义要求：`QQ_PROVIDER=qq_official` 且 `QQ_OFFICIAL_ENABLED=true` 才允许官方出站；两者不一致时必须 fail-closed（拒绝启动或拒绝发送），不得「尽力而为」。

### 3.2 凭据与端点

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_OFFICIAL_APP_ID` | `qq_official_app_id` | 机器人 AppID | str | 是 | 无（必须由运维填） | 中 | 官方 [S4] |
| `QQ_OFFICIAL_CLIENT_SECRET` | `qq_official_client_secret` | 换取 access_token 的 `clientSecret` | str | 是 | 无 | 🔒 高 | 官方 [S5] |
| `QQ_OFFICIAL_BOT_SECRET` | `qq_official_bot_secret` | **Webhook 验签/地址验证**用的「Bot Secret」 | str | 仅 webhook 模式 | 无 | 🔒 高 | 官方 [S8][S10]；与 `clientSecret` 是否同值**未确认**（见 §11 U2） |
| `QQ_OFFICIAL_TRANSPORT` | `qq_official_transport` | 事件接收方式 | 枚举：`websocket` 或 `webhook` | 否（有默认） | `websocket` | 低 | 官方 [S1] §8.1 |
| `QQ_OFFICIAL_API_BASE` | `qq_official_api_base` | OpenAPI 基址 | str(URL) | 否 | `https://api.bot.qq.com` | 低 | 官方 [S6] |
| `QQ_OFFICIAL_TOKEN_PATH` | `qq_official_token_path` | 换取 token 的路径 | str | 否 | `/app/getAppAccessToken` | 低 | 官方 [S5] |
| `QQ_OFFICIAL_GATEWAY_PATH` | `qq_official_gateway_path` | 获取 WSS 接入点的路径 | str | 否 | `/gateway` | 低 | 官方 [S11] |
| `QQ_OFFICIAL_HTTP_TIMEOUT_SECONDS` | `qq_official_http_timeout_seconds` | API/换 token 的请求超时 | float | 否 | `10` | 低 | 设计 |
| `QQ_OFFICIAL_TOKEN_REFRESH_MARGIN_SECONDS` | `qq_official_token_refresh_margin_seconds` | 到期前多少秒触发换新 | int | 否 | `60` | 低 | 官方 [S5]（60 秒内可取新 token） |
| `QQ_OFFICIAL_INTENTS` | `qq_official_intents` | 订阅位掩码 | int | 否 | 空=按功能推导（群事件 `1<<25`） | 低 | 官方 [S7][S13]；订阅无权限 intent 会被关（`4014`，[S9]） |
| `QQ_OFFICIAL_SHARD` | `qq_official_shard` | 分片 `shard_id/total` | str `"0,1"` | 否 | `"0,1"`（不分片） | 低 | 官方 [S9] |
| `QQ_OFFICIAL_WS_RECONNECT_BACKOFF_SECONDS` | `qq_official_ws_reconnect_backoff_seconds` | 断线重连退避基数 | float | 否 | `5` | 低 | 设计（官方未给重连间隔） |

**端点纪律**：`API_BASE`/`TOKEN_PATH`/`GATEWAY_PATH` 只能由部署配置决定，**任何群消息内容都不得改写这些值**；也不得把 AppSecret 发往用户可控 URL。

### 3.3 群、成员与回复上下文

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_OFFICIAL_ALLOWED_GROUP_OPENIDS` | `qq_official_allowed_group_openids` | 允许的群 `group_openid` 列表 | list[str]（逗号或 JSON） | 是（启用时） | `[]`（空=拒绝所有群） | 中 | 官方 [S6]（openid 按 AppID 作用域） |
| `QQ_OFFICIAL_ALLOWED_MEMBER_OPENIDS` | `qq_official_allowed_member_openids` | 允许触发特权命令的成员 `member_openid` | list[str] | 否 | `[]` | 中 | 官方 [S13]；仅用于未来特权命令，首期不启用 |
| `QQ_OFFICIAL_ACCEPT_ALL_GROUP_MESSAGES` | `qq_official_accept_all_group_messages` | 是否处理 `GROUP_MESSAGE_CREATE`（全量） | bool | 否 | `false` | 低 | 官方 [S14][S2]（需群主开启「接收所有消息」） |
| `QQ_OFFICIAL_ACTIVE_NOTIFICATIONS_ENABLED` | `qq_official_active_notifications_enabled` | 是否允许非回复型群消息 | bool | 否 | `false` | 低 | 官方 [S15][S16]（需群主开主动推送 + 配额） |
| `QQ_OFFICIAL_PASSIVE_REPLY_TTL_SECONDS` | `qq_official_passive_reply_ttl_seconds` | 被动回复本地有效窗口 | int | 否 | `300` | 低 | 官方 [S15]（5 分钟） |
| `QQ_OFFICIAL_MAX_REPLIES_PER_MSG` | `qq_official_max_replies_per_msg` | 单条消息最多回复次数 | int | 否 | `5` | 低 | 官方 [S15] |
| `QQ_OFFICIAL_MSG_SEQ_START` | `qq_official_msg_seq_start` | `msg_seq` 起始值 | int | 否 | `1` | 低 | 官方 [S15]（不填默认 1） |

> `msg_seq` 规则：同一 `msg_id` + 同一 `msg_seq` 重复发送**会失败**（[S15]）。本地必须按「每条入站消息维护递增序号」，且不得靠递增序号突破 5 次上限。

### 3.4 限流、队列与账本

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_OFFICIAL_RATE_BOT_QPM` | `qq_official_rate_bot_qpm` | Bot 维度每分钟主动消息上限 | int | 否 | `60`（未认证主体用 `30`） | 低 | 官方 [S15]（企业/个人身份证认证 60，未认证 30） |
| `QQ_OFFICIAL_RATE_GROUP_QPM` | `qq_official_rate_group_qpm` | 单群每分钟上限 | int | 否 | `20` | 低 | 官方 [S15]（单关系 20/qpm） |
| `QQ_OFFICIAL_DAILY_PER_GROUP` | `qq_official_daily_per_group` | 单群每日主动消息预算 | int | 否 | `800`（低于官方 1000，留余量） | 低 | 官方 [S15]（每群 1 天最多 1000 条）+ 设计余量 |
| `QQ_OFFICIAL_HTTP_QPS` | `qq_official_http_qps` | 发送接口并发/速率上限 | int | 否 | `10`（远低于官方 100） | 低 | 官方 [S15]（100 QPS 是接口上限，不覆盖主动配额） |
| `QQ_OFFICIAL_QUEUE_MAX` | `qq_official_queue_max` | 待发队列最大长度 | int | 否 | `100` | 低 | 设计 |
| `QQ_OFFICIAL_COALESCE_WINDOW_SECONDS` | `qq_official_coalesce_window_seconds` | 同类通知合并窗口 | int | 否 | `5` | 低 | 设计 |
| `QQ_OFFICIAL_NOTIFICATION_TTL_SECONDS` | `qq_official_notification_ttl_seconds` | 通知在队列里的最长存活时间 | int | 否 | `300` | 低 | 设计 |
| `QQ_OFFICIAL_RETRY_MAX_ATTEMPTS` | `qq_official_retry_max_attempts` | 可重试错误的次数上限 | int | 否 | `2` | 低 | 设计 |
| `QQ_OFFICIAL_RETRY_BACKOFF_SECONDS` | `qq_official_retry_backoff_seconds` | 退避基数（指数退避起点） | float | 否 | `2` | 低 | 设计 |

> **禁止的语义**：被动回复过期（`304103`/`40034005`/`40034128`）**不得**自动转成主动消息；`429`/`40034100` 只能延迟与合并，不能忙循环重试；队列满时丢弃最旧通知并计数，不阻塞 MC 事件入口。

### 3.5 Webhook 专用（仅 `transport=webhook` 时需要）

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_OFFICIAL_CALLBACK_PATH` | `qq_official_callback_path` | 回调路由 | str | 否 | `/api/v1/qq-official/events` | 低 | 设计（与 OneBot 路径分离） |
| `QQ_OFFICIAL_CALLBACK_PUBLIC_URL` | `qq_official_callback_public_url` | 管理端登记的 HTTPS 地址 | str(URL) | 是（仅 webhook 模式） | 无默认 | 中 | 官方 [S8]（端口 80/443/8080/8443） |
| `QQ_OFFICIAL_CALLBACK_MAX_BODY_BYTES` | `qq_official_callback_max_body_bytes` | 请求体上限 | int | 否 | `65536` | 低 | 设计（与现有 64 KiB 一致） |
| `QQ_OFFICIAL_CALLBACK_TIMESTAMP_TOLERANCE_SECONDS` | `qq_official_callback_timestamp_tolerance_seconds` | 时间戳允许偏差 | int | 否 | `300`（**无官方依据，需实测后定**） | 低 | 设计；官方未规定容差（§11 U3） |

> Webhook 模式还**必须**配置 `QQ_OFFICIAL_BOT_SECRET`；`op=13` 地址验证与每请求 Ed25519 验签都依赖它（[S8][S10]）。验签必须先读**原始 body 字节**，再 `timestamp + body` 组合。

### 3.6 持久化与去重

| 环境变量 | 字段 | 含义 | 类型 | 必填 | 默认建议 | 敏感性 | 来源 |
|---|---|---|---|---|---|---|---|
| `QQ_OFFICIAL_STATE_DIR` | `qq_official_state_dir` | 去重表/账本/身份映射的落盘目录 | str(path) | 否 | `/var/lib/mcqq-bridge` | 中 | 设计（当前 `state.py` 纯内存，不能假装持久） |
| `QQ_OFFICIAL_DEDUPE_TTL_SECONDS` | `qq_official_dedupe_ttl_seconds` | 事件去重键保留时长 | int | 否 | `86400`（**需按实际重投行为调整**） | 低 | 设计；官方未给重投策略（§11 U3） |
| `QQ_OFFICIAL_DAILY_LEDGER_PATH` | `qq_official_daily_ledger_path` | 每群每日发送计数文件 | str(path) | 否 | `${STATE_DIR}/daily.json` | 低 | 设计 |
| `QQ_OFFICIAL_IDENTITY_STORE_PATH` | `qq_official_identity_store_path` | 已批准 openid 的本地映射 | str(path) | 否 | `${STATE_DIR}/identities.json` | 中 | 设计 |

> 容器部署时 `docker-compose.yml` 现在是 `read_only: true` + tmpfs `/tmp`；要落盘必须显式加可写卷，否则上面这些路径写入会失败——这也是为什么它们默认不在首期开启。

### 3.7 与现有键的关系（复用/禁用）

| 现有键 | 迁移后处置 | 原因 |
|---|---|---|
| `MC_NOTIFICATIONS` | 复用，但语义变化：每事件广播 → 合并/限额通知 | `service.py:33-34` 现在是逐群直发且丢结果 |
| `MC_COMMANDS` | 复用（`players/status`） | 协议无关 |
| `MC_SAY_ENABLED` | 复用，默认仍 `false` | 需真实 MC 消费者 |
| `STATE_TTL_SECONDS` | 复用 | 与协议无关 |
| `ALLOWED_GROUP_IDS` | **不得迁移为官方白名单** | 是数字群号；openid 是另一套字符串命名空间（[S6]） |
| `ONEBOT_*`（全部） | 官方模式下**禁用**，但保留作回滚 | 鉴权头/事件形状/WS 方向都不同 |
| `HERMES_ADMIN_IDS` | 官方路由首期不启用 | 整数 actor 契约；见 [R2] §9 |
| `HERMES_ADMIN_ENABLED` | 首期强制 `false` | 同上 |

### 3.8 完整示例（⛔ 不可加载，仅示意语义）

```dotenv
# ⛔ 当前代码不读取以下任何键；写进 .env 不会生效（会被静默忽略）
# 所有值均为占位符，禁止填入真实密钥
QQ_PROVIDER=qq_official
QQ_OFFICIAL_ENABLED=true
QQ_OFFICIAL_APP_ID=<APP_ID>
QQ_OFFICIAL_CLIENT_SECRET=<CLIENT_SECRET_PLACEHOLDER>
QQ_OFFICIAL_BOT_SECRET=<BOT_SECRET_PLACEHOLDER>
QQ_OFFICIAL_TRANSPORT=websocket
QQ_OFFICIAL_API_BASE=https://api.bot.qq.com
QQ_OFFICIAL_ALLOWED_GROUP_OPENIDS=<GROUP_OPENID_PLACEHOLDER>
QQ_OFFICIAL_ACCEPT_ALL_GROUP_MESSAGES=false
QQ_OFFICIAL_ACTIVE_NOTIFICATIONS_ENABLED=false
QQ_OFFICIAL_PASSIVE_REPLY_TTL_SECONDS=300
QQ_OFFICIAL_MAX_REPLIES_PER_MSG=5
QQ_OFFICIAL_MSG_SEQ_START=1
QQ_OFFICIAL_RATE_BOT_QPM=60
QQ_OFFICIAL_RATE_GROUP_QPM=20
QQ_OFFICIAL_DAILY_PER_GROUP=800
QQ_OFFICIAL_QUEUE_MAX=100
QQ_OFFICIAL_COALESCE_WINDOW_SECONDS=5
QQ_OFFICIAL_NOTIFICATION_TTL_SECONDS=300
QQ_OFFICIAL_STATE_DIR=/var/lib/mcqq-bridge
QQ_OFFICIAL_DEDUPE_TTL_SECONDS=86400
# webhook 模式才需要：
# QQ_OFFICIAL_TRANSPORT=webhook
# QQ_OFFICIAL_CALLBACK_PATH=/api/v1/qq-official/events
# QQ_OFFICIAL_CALLBACK_PUBLIC_URL=https://<YOUR_DOMAIN>:8443/api/v1/qq-official/events
# QQ_OFFICIAL_CALLBACK_MAX_BODY_BYTES=65536
```

对照写法（真实凭据只在 secret 存储里，不进文件）：

```bash
# 示例：把占位符替换为从 secret 存储取出的值，写入权限 600 的 .env
# （由运维在自己的受控终端执行；不要在这里粘贴真实值到聊天/工单/文档）
printf 'QQ_OFFICIAL_APP_ID=%s\n' '<APP_ID>' >> .env
```

---

## 4. 真实秘密的保存方法

| 方式 | 适用 | 要点 | 禁止 |
|---|---|---|---|
| `.env`（`chmod 600`，属主=服务账号） | 本机/单机 | 已在 `.gitignore`；目录不可被其他用户读 | 提交到 git、备份到公开网盘、贴进群 |
| Docker Compose `env_file` + secrets | 容器 | 优先 `secrets:` 挂载文件而非明文环境变量；环境变量会出现在 `docker inspect` | 把 secret 写进 `docker-compose.yml` 本体 |
| systemd `EnvironmentFile=`（0600） | 宿主机服务 | 文件 `root:root 0600`，服务用专用用户 | 用 `ExecStart=` 内联 secret（会进 `ps`） |
| 密钥管理服务（Vault/KMS 等） | 多实例 | 启动时拉取，内存持有，不落盘 | 打印到日志 |

**轮换与泄漏处置**

1. 在管理端重置 `AppSecret`（以及 `Bot Secret`，若为独立字段）→ 更新 secret 存储 → 重启服务 → 确认新 token 可取。
2. 泄漏时的顺序：**先停出站**（`QQ_OFFICIAL_ENABLED=false` 或停队列）→ 轮换 → 审计日志与发送记录 → 再恢复。
3. 排查历史泄漏：**只列命中文件名/路径，不打印行内容和值**，并限定扫描范围（本仓库工作区，排除依赖与版本库目录）：

   ```bash
   cd /home/czauw/minecraft-qq-bridge
   SCAN_ROOT=.            # 只扫本仓库工作区；不要指向 / 、$HOME 或网络挂载
   grep -rIl --binary-files=without-match \
     --exclude-dir=.venv --exclude-dir=.git --exclude-dir=.pytest_cache \
     -e 'QQ_OFFICIAL_CLIENT_SECRET' -e 'QQ_OFFICIAL_BOT_SECRET' \
     "$SCAN_ROOT"
   # -l 只输出文件名（不含命中的整行），-I 跳过二进制；输出可直接贴进工单/排障记录
   ```

   **怎么读这份清单（批准存储 vs 误放）**

   | 命中位置 | 判定 | 处置 |
   |---|---|---|
   | 本机 `.env`（`chmod 600`、属主=服务账号） | **批准的 secret 存储**，不是泄漏 | 只审计权限/属主/备份保留策略；**不打开内容、不输出值** |
   | systemd `EnvironmentFile=` 文件、compose `secrets:` 的源文件 | 批准存储 | 同上（权限 0600，仅服务账号可读） |
   | `docs/`、`src/`、`tests/`、`docker-compose.yml`、`config.example.yaml`、日志、仓库外的网盘/聊天备份副本 | **误放** | 立即停出站 → 轮换（第 1、2 步）→ 清理该文件 → 记录时间线 |

   - `.env` 出现在清单里 **不等于** 泄漏；反过来，把 `.env` 从扫描中排除也 **不等于**"没有泄漏"——批准存储必须按权限/保留策略单独审计，只是审计时**不输出值**。
   - 如果怀疑**值**被复制到别的文件名下（键名被改动的情况），需要按值排查。此时不要写 `grep -e "$SECRET" …`：值会进入 argv、`ps` 和 shell 历史。用 `-f` 从进程替换读模式，并交互式读入变量（`read -rs`），使值不出现在命令行里：

     ```bash
     read -rs QQ_SECRET_TO_SCAN          # 交互输入，不回显、不进历史
     grep -rIl --binary-files=without-match \
       --exclude-dir=.git --exclude-dir=.venv \
       -f <(printf '%s\n' "$QQ_SECRET_TO_SCAN") .
     unset QQ_SECRET_TO_SCAN
     # printf 是 shell 内建，不产生进程，因此值不会出现在 ps 的 argv 里
     ```

   - 该扫描只能证明"这些文件里没出现这个键名/这个值"，**不能**证明凭据从未被使用过；是否已被滥用要看管理端调用记录与出站日志。
4. **不得复用** OneBot 的 `ONEBOT_ACCESS_TOKEN` / `ONEBOT_WEBHOOK_TOKEN` / `ONEBOT_WEBHOOK_SECRET` 作为官方凭据：一个是静态共享密钥，一个是由 AppID+Secret 换来的短期 token，语义完全不同（[S5]）。
5. `AppSecret` 只进服务端；官方明确「请勿在应用前端使用访问凭证」（[S5]）。

---

## 5. 身份字段：openid 与各种 ID

| 字段 | 出现位置 | 含义 | 作用域 | 能否当数字群号/QQ 号用 | 存储建议 |
|---|---|---|---|---|---|
| `group_openid` | 事件 `d.group_openid`；发送路径 `/v2/groups/{group_openid}/messages` | 群标识 | **按 AppID 不同而不同**（[S6]） | **不能** | 原样字符串存白名单；日志可散列 |
| `member_openid` | 事件 `d.author.member_openid` | 群内成员标识 | 按 AppID + 群 | **不能** | 仅存授权必需者；不做跨群合并 |
| `user_openid` | 单聊场景 | 单聊用户标识 | 按 AppID | **不能** | 首期不用 |
| `union_openid` | `d.author.union_openid`，**可能为空**（[S13]） | 跨应用统一标识 | 需单独申请（[S2] 提到开通规则） | **不能** | 首期不用，不得据此免除绑定流程 |
| `msg_id` | 事件 `d.id` | 消息 ID，用于被动回复/撤回 | 单条消息 | 不能 | 只在内存/短期上下文中保留 |
| `event_id` | `payload.id`（最外层） | 事件 ID，用于特定事件回复 | 单个事件 | 不能 | 仅 3 类事件可用（[S15]） |
| `msg_seq` | 发送请求体 | 同一 `msg_id` 下的回复序号 | 单条消息 | 不能 | 按消息维护递增整数 |
| `s` | `payload.s` | 连接级下行序列号（心跳/Resume 用） | 单连接 | 不能 | 只用于 Resume，不是消息 ID |
| `trace_id` / `X-Tps-trace-ID` | 响应体/响应头（[S6]） | 平台链路追踪 | 单请求 | 不能 | 记日志用于排障，不含凭据 |

**采集与批准流程（人工）**

1. 让机器人加入测试群，由群成员发一条 @ 消息。
2. 从**已认证来源**（官方 WS/Webhook 事件）读取 `d.group_openid`、`d.author.member_openid`，记录到本地待批清单（不要用群昵称/数字群号猜测）。
3. 运营者核对「这确实是我的测试群」后，把该 `group_openid` 写入 `QQ_OFFICIAL_ALLOWED_GROUP_OPENIDS`（⛔ 未来键）。
4. 重启/重载生效，再做一次 @ 查询验证。
5. **首次见到的群不得自动加入白名单**；群昵称、成员昵称、`member_role=owner/admin` 都不构成本项目授权。

**禁止事项**

- 把数字群号写进 `QQ_OFFICIAL_ALLOWED_GROUP_OPENIDS`（永不匹配，且掩盖配置错误）。
- 把 `member_openid` 当 QQ 号展示给用户或做跨群合并。
- 把 `message_scene.ext` 里的 `auth_token` 落盘/打日志（它是鉴权令牌，[S13] 示例中可见）。

---

## 6. 网络、TLS 与回调校验

### 6.1 出站（两种模式都需要）

| 项 | 要求 | 检查方法 |
|---|---|---|
| 目标域名 | `api.bot.qq.com`（HTTPS/WSS 443） | `getent hosts api.bot.qq.com` |
| TLS 校验 | **不得**关闭证书校验；不得使用 `verify=False` | 代码审查 |
| 出口 IP | 必须与管理端「服务器 IP 白名单」一致（仅单个 IPv4、最多 50 条、**不设置则全放行**，[S1] §8.2） | `curl -s https://api.ipify.org` 与白名单比对 |
| 代理 | 若经代理出网，需确认代理支持 **WSS 长连接**，且白名单填**代理出口 IP** | 代理日志 + 出口 IP 比对 |
| 时间同步 | 系统时钟偏差会影响 Webhook 时间戳与 token 判定 | `timedatectl` / `chronyc tracking` |
| 长连接 | WS 需容忍官方示例约 45 秒（`heartbeat_interval: 45000`）的心跳间隔与断线重连；中间设备不得 60 秒空闲断开 | 抓包/代理配置 |

### 6.2 Webhook 入站（仅备选方案）

| 项 | 要求 | 依据 |
|---|---|---|
| 公网 HTTPS | 必须是 HTTPS，**端口仅 80/443/8080/8443** | [S8] |
| 反代保真 | 透传原始 body 字节、`X-Signature-Ed25519`、`X-Signature-Timestamp`、`X-Bot-Appid`；不得 JSON 重排/美化 | [S10] |
| 只暴露必要路径 | 仅 `QQ_OFFICIAL_CALLBACK_PATH`；`/healthz`、MC webhook、OneBot、状态查询、Hermes 一律不对外 | 安全底线 |
| body 上限 | 与现有 64 KiB 一致，超限 413 | `main.py:60-66` |
| 地址验证 | 处理 `op=13`：读 `d.event_ts`+`d.plain_token`，用 Bot Secret 派生 32 字节种子签名，返回 `{plain_token, signature}` | [S8] |
| 逐请求验签 | 每次回调验 `timestamp + 原始 body` 的 Ed25519 签名；校验签名长度与 `sig[63]&224==0` | [S10] |
| 去重 | 以 `(app_id, group_openid, msg_id)`（或事件 `id`）为键，重复请求正常 ACK 但不重复执行 | [S13] 重复推送提示；[R2] §5.2 |
| ACK | 按 `op=12`（HTTP Callback ACK）语义回包；业务处理与 ACK 解耦，ACK 失败不等于业务失败 | [S7] |
| 重试/超时 | 官方未给出重投次数、间隔、ACK 时限、时间戳容差 → **必须实测后再定配置**，不得编造 | §11 U3 |

**反代示例（占位，仅示意形态；实际按你的 Caddy/Nginx 配置）**

```caddyfile
<YOUR_DOMAIN> {
    handle /api/v1/qq-official/events {
        reverse_proxy 127.0.0.1:8000
    }
    respond 404
}
```

> 本机现状：Caddy 只监听 `127.0.0.1:8443`，已有 3 个站点（panel/cockpit/hermes），**没有**为本项目配置站点（[R1] E14）；公网入口经 frp 映射到 8443，是否满足官方的端口/证书/稳定性要求**未验证**（§11 U7）。

### 6.3 WS 模式（首期推荐）

- 桥接器作为**客户端**出站连 `wss://api.bot.qq.com/websocket/`（[S9][S11]）；本机 HTTP 监听保持 `127.0.0.1:8000`，不新增公网暴露面。
- 必须实现：Hello → Identify（`token="QQBot {AccessToken}"`）→ READY 保存 `session_id` → 心跳（带最新 `s`）→ 断线 Resume（`session_id`+`seq`）→ `RESUMED`；关闭码按 [S9] 表处理。
- `/gateway/bot` 的 `remaining` 是连接数上限，`max_concurrency` 是启动并发限制（[S9]）；不要并发风暴式重连。

---

## 7. 消息政策与限流

### 7.1 官方定义

| 类型 | 特征 | 官方约束 | 来源 |
|---|---|---|---|
| 被动消息（回复用户） | 携带 `msg_id` | 5 分钟内、每条消息最多 5 次 | [S15] |
| 被动消息（响应事件） | 携带 `event_id` | 仅 3 类事件支持 | [S15] |
| 主动消息 | 不带 `msg_id`/`event_id` | 用户/群可关闭；Bot 60/30 qpm；单群 20 qpm；每群每天 1000 条 | [S15][S16] |

### 7.2 本项目建议策略

| 策略 | 建议值 | 理由 |
|---|---|---|
| MC 聊天默认 | `mc_notifications.chat=false` | 高频、易触发审核与配额 |
| join/leave | 优先聚合（如 5 秒窗口合并成一条） | 同一玩家抖动进服会刷屏 |
| death | 独立开关，可与 join/leave 合并 | 同上 |
| 每群日预算 | ≤ 800（官方上限 1000） | 留 20% 余量应对人工补发与重试 |
| 每分钟 | ≤ 20（Bot 维度和单群都设） | 官方单关系 20/qpm 是硬约束 |
| 队列满 | 丢弃最旧 + 计数 + 告警 | 不能阻塞 MC webhook |
| 被动过期 | 丢弃并计数，**禁止转主动** | 语义不同，转投会误导用户 |
| 发送失败 | 按 `err_code` 分类：可重试（5xx/超时/429）有限退避；不可重试（无权限/禁言/非成员/违规/去重）立即停 | [S15] |
| 内容 | 纯文本；过滤 URL（`40054010`）；按本地 2000 字符上限截断（官方未公布明确长度上限，`40054007` 仅为错误码） | 现状模型上限 `ChatEvent.message` 2000 |
| 回环 | 加 `[MC]` / `[QQ]` 前缀；过滤 bot 消息与自身回声 | 防无限转发 |

### 7.3 错误分类（与官方错误码对应）

| 类别 | `err_code` 示例 | 动作 |
|---|---|---|
| 回复过期/次数 | `304103`、`40034005`、`40034128` | 终止该回复，不重试、不转主动 |
| 无权限/状态 | `40034105`（无主动权限）、`40054002`（禁言）、`40054003`/`40034101`（非群成员）、`40054016`（机器人下线） | 暂停该群，等人工处理 |
| 限频 | `429`、`40034100` | 延迟 + 合并，指数退避 |
| 去重 | `40054005` | 对账原发送，禁止改序号重发同一逻辑消息 |
| 内容 | `40034006`（违规）、`40054010`（URL 禁止）、`40054007`（长度） | 标记失败并审计，原文不无条件重发 |
| 未知 | `50055001` 等 | 有界退避；超时视为「投递结果不明」，不承诺 exactly-once |

> 认证失败另有两套语义：换 token 接口是 **HTTP 200 + 业务 `code`**（`100016` 等，[S5]）；OpenAPI 是 **HTTP 状态码 + `err_code`**（`401` 等，[S6]）。两套不要混用同一个解析器。

---

## 8. 故障排查

| 现象 | 优先检查 | 命令/位置 | 常见原因 |
|---|---|---|---|
| 改了 `QQ_OFFICIAL_*` 没反应 | 是否已被实现 | `grep -rn "qq_official" src/`（当前应为空） | ⛔ 键不存在，静默忽略 |
| 改了 `HOST`/`PORT`/`LOG_LEVEL` 没反应 | 死配置 | `grep -rn --include=*.py -e "settings\.host" -e "settings\.port" -e "settings\.log_level" -e "config\.host" -e "config\.port" src/`（命中 0） | 🟡 需用 uvicorn 参数/Compose 端口 |
| 服务起来了但 MC 事件 401 | 请求携带的 Bearer 是否与配置值**逐字节相同** | `curl -i -X POST -H "Authorization: Bearer $TOKEN" .../events/chat` | `MC_WEBHOOK_TOKEN` 留空（则任何请求都 401）；或请求没带 token / 带了别的值（与"值看起来像占位符"无关） |
| `POST /api/v1/onebot/events` 恒 401 | 一个凭据都没配 | 看 `main.py:103-104` | `ONEBOT_WEBHOOK_TOKEN` 与 `SECRET` 都空 |
| QQ 命令被识别但不回复 | 出站被关 | 返回 `accepted=true, delivered=false` | `ONEBOT_ENABLED=false` 或群不在白名单 |
| 换 token 总失败 | HTTP 200 但 `code!=0` | 看响应体 `code` | `100016` secret 错、`100007` AppID 无效/机器人异常、`100001` 过频 |
| WS 连不上/刚连就断 | 关闭码 | 日志记录 close code | `4013` 无效 intent、`4014` intent 无权限、`4008` 发送过快 |
| WS 频繁掉线 | 心跳/网络 | 心跳 ACK 间隔、代理空闲超时 | 未按 `heartbeat_interval` 发心跳；中间设备断长连接 |
| @ 机器人无回复但私聊正常 | 群主权限 | 手机 QQ「设置权限」 | 未开主动推送；或未 @；或群未授权给该机器人 |
| 主动通知全部失败 | 主动推送开关 | 管理端/群主设置 | `40034105` 无权限、`40054002` 禁言、`40054003` 非成员 |
| Webhook 显示"验证失败" | `op=13` 处理与 Bot Secret | 本地用官方示例向量自测 | 种子派生错（未 repeat 到 32 字节）、返回体字段名错 |
| Webhook 验签 401 | 是否用了重排后的 JSON | 断言原始 body 字节 | 反代/框架重排 JSON、头未透传 |
| 消息重复发送 | 去重键 | 检查是否按 `msg_id` 去重 | 平台重投 + 未做幂等 |
| 中文乱码 | 编码 | `Content-Type: application/json; charset=utf-8` | 未声明 charset |
| 时间戳/签名校验随机失败 | 时钟 | `timedatectl` | NTP 未同步 |
| 出口被拒 | IP 白名单 | 管理端白名单 vs `curl ifconfig.me` | 填了内网 IP；代理出口未加入 |

---

## 9. 回滚

**触发条件（任一）**：主动消息连续失败、群主关闭权限、内容审核告警、配额持续打满、连接不稳定影响 MC 状态维护。

| 步骤 | 动作 | 校验 |
|---|---|---|
| 1 | 停止出站队列（清空待发、记录丢弃数） | 队列长度归零，无新发送 |
| 2 | `QQ_PROVIDER` 切回 `onebot`（或关闭 `QQ_OFFICIAL_ENABLED`） | `/readyz` 反映实际 provider |
| 3 | 保留 MC 入站与状态缓存运行（只读查询仍可用） | `/api/v1/state/meta` 正常 |
| 4 | 若怀疑凭据泄漏：在管理端重置 secret → 更新存储 → 重启 | 新 token 可取；旧 token 失效 |
| 5 | 清理临时产物：未批准 openid、发送账本、去重表（按保留策略） | 落盘文件按预期被清理 |
| 6 | 记录时间线：现象、错误码、`trace_id`、回滚时刻 | 写进运维记录（不含凭据） |

**回滚到 OneBot 的注意事项**：重新启用 OneBot 会恢复第三方登录路径与相应账号风险（[R2] §11）；这不是「官方化」的一部分，必须由运维显式决定并记录，不能自动回滚后仍宣称「已完全官方化」。

---

## 10. 验证记录（本轮实际执行）

### 10.1 文档与代码一致性

| 检查 | 命令 | 结果 |
|---|---|---|
| 现有字段总数 | 探针打印 `Settings.model_fields` | **35** |
| 官方字段是否已存在 | 探针过滤 `qq_official*` | **0**（全部 ⛔ 未实现） |
| 未知环境变量是否报错 | 探针设置 `QQ_OFFICIAL_APP_ID=probe-app-id` 后实例化 | **不报错、未进入字段**（`extra="ignore"`） |
| 死配置 | `grep -rn --include=*.py --exclude=settings.py -e "\bapp_name\b" -e "\blog_level\b" -e "\bhost\b" src/` | **0 命中** |
| YAML 是否被加载 | `grep -rniI --include=*.py yaml src tests scripts`（统计输出行数） | **0** |
| 测试基线 | `.venv/bin/python -m pytest -q -W error` | **53 passed in 1.18s** |
| `service.py` 丢弃发送结果 | 阅读 `service.py:31-34` | 确认未使用返回值；MC 入口恒 202（`main.py:98`） |

### 10.2 生效配置探针（只读；放在会话 scratch，不入仓库）

```python
# ~/.hermes/profiles/worker/cache/scratch/settings_probe.py
import os, sys
sys.path.insert(0, "/home/czauw/minecraft-qq-bridge/src")
from mcqq_bridge.settings import Settings
os.environ["QQ_OFFICIAL_APP_ID"] = "probe-app-id"
s = Settings(_env_file=None)
print("declared fields =", len(Settings.model_fields))
print("qq_official* fields =", [f for f in Settings.model_fields if f.startswith("qq_official")])
print("probe ignored =", not any(f.startswith("qq_official") for f in s.model_dump()))
```

实测输出（摘要）：

```text
declared fields = 35
qq_official* fields = []
probe ignored = True
```

### 10.3 官方链接可用性（2026-11-10 04:08 UTC，`curl -A 'Mozilla/5.0' -o /dev/null -w '%{http_code}'`）

| URL | 状态 |
|---|---|
| https://bot.q.qq.com/wiki | 200 |
| https://bot.q.qq.com/wiki/bot_FAQ | **301**（跳转链 `http://bot.q.qq.com/bot_FAQ/` → `https://bot.q.qq.com/bot_FAQ/` → `https://q.qq.com`，取不到正文） |
| https://bot.q.qq.com/wiki/bot_FAQ/ | **200**（引用请用带尾斜杠的地址） |
| https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html | 200 |
| https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/shard_url_get.html | 200 |

复现命令：

```bash
for u in \
  https://bot.q.qq.com/wiki \
  https://bot.q.qq.com/wiki/bot_FAQ/ \
  https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html \
  https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html \
  https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html \
  https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html \
  https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html ; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -A 'Mozilla/5.0' "$u")" "$u"
done
```

> 说明：裸域名 `https://api.bot.qq.com` 直接 GET 返回 **404** 属正常——只有具体接口路径有意义；`/app/getAppAccessToken` 对 GET 也返回 200，但业务成败仍以响应体 `code` 判定（[S5]）。因此**不要用「访问基址不通」判断平台故障**。

### 10.4 未执行（不得当作已验证）

- 未用任何 AppID/AppSecret 调用 `https://api.bot.qq.com`（无凭据）。
- 未建立真实 WebSocket 连接，未收取真实事件，未发送任何 QQ 消息。
- 未测试 Webhook 地址验证/验签（无公网 HTTPS 入口与 Bot Secret）。
- 未核实本机 Caddy/frp 能否满足官方 Webhook 端口与证书要求。
- 未登录管理端核验「Bot Secret 与 clientSecret 是否同值」。

### 10.5 阻断项修复验证（2026-11-10，scratch 目录，均在仓库外执行）

| 项 | 命令（摘要） | 结果 |
|---|---|---|
| 修复前复现 | 从 Markdown 抽取 §2.8 dotenv → `Settings(_env_file=scratch/.env)` | **失败**：`ValidationError: invalid literal for int() with base 10: '<NUMERIC_GROUP_ID>'` |
| 修复后加载 | 同上，抽取**当前** §2.8 → `Settings(_env_file=scratch/run3/.env)` | **SAMPLE_LOAD_OK**：`allowed_group_ids=[]`、`onebot_enabled=False`、`mc_say_enabled=False`、token 全空 |
| 修复后启动 | scratch 目录内 `PYTHONPATH=src uvicorn mcqq_bridge.main:app --host 127.0.0.1 --port 18082` | `/healthz`=200 `{"status":"ok"}`；`/readyz`=200 `{"status":"ready","onebot_enabled":false}` |
| 401 语义 | 同进程 `POST /api/v1/minecraft/events/chat`：无 token / 错 token / 匹配 token | 401 / 401 / **202**（故 401 取决于请求 token，与配置值"像不像占位符"无关） |
| M2 对照（被替换的旧写法） | 旧写法用 `grep -rIn …`（打印命中整行，含值）在虚构 canary 夹具上实测 | **回显了 canary 值**——这正是删除它的原因，勿再使用 |
| M2 新命令 | `SCAN_ROOT=. grep -rIl --binary-files=without-match --exclude-dir=.venv --exclude-dir=.git --exclude-dir=.pytest_cache -e … "$SCAN_ROOT"` | 只输出 `./.env`、`./docker-compose.yml`、`./docs/notes.md`；stdout **不含**任何 canary 值；`.venv` 被排除 |
| M2 按值排查 | `read -rs` + `grep -rIl -f <(printf '%s\n' "$VAR") .`，用 grep 包装器记录 argv | argv 为 `-f /dev/fd/63 .`，**不含**值（对照：`-e "$VAR"` 写法会把值写进 argv） |
| 回归 | `.venv/bin/python -m pytest -q -W error` | **53 passed in 1.17s** |

> 上述均为本机 loopback + 虚构 canary，**没有**使用真实凭据、没有扫描真实 `.env`（本机也不存在）、没有调用 QQ API；不能据此认为官方 Bot 联通。

---

## 11. 未解决项

| # | 事项 | 影响 |
|---|---|---|
| U1 | `QQ_OFFICIAL_*` 全套键尚未实现（`model_fields` 中 0 项） | 第 3 部分全部不可用；不能作为部署依据 |
| U2 | `Bot Secret` 与 `clientSecret` 是否同一值未确认（[S8][S10] vs [S5]） | 决定是否拆两个配置键；webhook 实现前必须核对 |
| U3 | Webhook 重投次数/间隔、ACK 时限、时间戳容差无官方数值 | `DEDUPE_TTL`、`TIMESTAMP_TOLERANCE` 默认值只能实测后定 |
| U4 | 官方未给出群消息文本的明确长度上限（仅有 `40054007`） | 截断阈值暂按本地 2000，需实测 |
| U5 | 全量消息/主动推送在本应用的实际开通状态未知 | 决定 §3.3 两个开关能否打开 |
| U6 | openid 在机器人重建后是否变化未核实 | 白名单可能需要重采 |
| U7 | 本机公网入口（Caddy :8443 / frp）对 Webhook 的适配未验证 | webhook 备选方案可行性未知 |
| U8 | `docker-compose.yml` 现为 `read_only: true`，落盘键需要额外可写卷 | 阶段 1 实现时必须处理，否则账本/去重写失败 |
| U9 | 现有 `service.py` 丢弃发送结果、状态纯内存、单 worker | 配置层面无法修复，属代码差距（见接入文档 §6） |

---

## 12. 参考来源（查阅日期 2026-11-10 UTC）

| 编号 | 标题 | URL |
|---|---|---|
| S1 | QQ Bot 介绍与接入指南（连接方式、IP 白名单、体验号、服务范围） | https://bot.q.qq.com/wiki/ |
| S2 | QQ Bot 常见问题答疑（沙箱、群主授权、封禁、主体升级） | https://bot.q.qq.com/wiki/bot_FAQ/ |
| S3 | 开发文档 v2 | https://bot.q.qq.com/wiki/develop/api-v2/ |
| S4 | 启动接入（AppID/AppSecret） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html |
| S5 | 获取访问凭证（token 接口、HTTP 200 语义、TTL、`QQBot` 头） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html |
| S6 | API 调用指南（基址、错误结构、openid 命名空间） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html |
| S7 | 通用数据结构（payload/opcode/intents） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html |
| S8 | Webhook 方式（端口、op13 地址验证、种子派生） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html |
| S9 | WebSocket 方式（Hello/Identify/心跳/Resume/关闭码） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html |
| S10 | 安全和授权（Ed25519 验签） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html |
| S11 | 获取通用 WSS 接入点（`GET /gateway`） | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html |
| S12 | 获取带分片 WSS 接入点（`GET /gateway/bot`） | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/shard_url_get.html |
| S13 | 群@机器人消息（事件字段、重复推送提示） | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html |
| S14 | 群消息（全量模式） | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html |
| S15 | 发送群聊消息（被动 5 分钟/5 次、配额、错误码） | https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html |
| S16 | 消息收发概述（主动/被动定义、消息类型） | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html |
| S17 | QQ 开放平台（管理端入口） | https://q.qq.com/ |
| S18 | 管理端回调设置（需登录） | https://q.qq.com/qqbot/#/developer/webhook-setting |

---

## 13. 本轮改动范围

- 仅新增/修改两份文档：`docs/QQ_BOT_INTEGRATION.md`、`docs/QQ_BOT_CONFIGURATION.md`。
- 未修改 `src/`、`tests/`、`scripts/`、`README.md`、`.env.example`、`config.example.yaml`、部署工件；未创建 `.env`；未 commit/reset/clean（工作区保持全 untracked）。
- 探针脚本只写在会话 scratch 目录（`~/.hermes/profiles/worker/cache/scratch/settings_probe.py`、`fetch_kw.py`），不在仓库内。
