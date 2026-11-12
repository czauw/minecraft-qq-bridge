# 现有 MCQQ 实现盘点与迁移差距

- 文档性质：现状盘点（as-is）＋ 向官方 QQ Bot 迁移的差距与风险登记。**不包含**实现方案、排期与承诺。
- 盘点对象：`/home/czauw/minecraft-qq-bridge`（工作区 `dir`），当前 git 无提交、全部文件未跟踪。
- 生成时间：2026-11-10T03:55Z（UTC）。
- 本轮约束遵守：未修改 `src/`、`tests/`、部署配置；未执行部署、未做任何 QQ 登录；未读取/输出 `.env`（仓库中不存在 `.env` 文件，只有 `.env.example`）；未提交或重置 git；未修改任何 profile 的配置/技能/记忆。唯一写入文件为本文件。
- 官方平台事实的来源与获取时间见第 12 节；**平台策略细节以 `docs/research/QQ_BOT_PLATFORM_FINDINGS.md`（t_34b670ec）为准**，本文件只引用与"接口形状差异"直接相关的最小集合，并把不确定项显式标注。

---

## 1. 结论摘要

1. 现有实现是一个 **FastAPI 单进程网关**：MC 侧入站 HTTP webhook，QQ 侧 OneBot v11（HTTP API 出站 + HTTP POST / 反向 WebSocket 入站）。共 12 个 Python 源文件、1653 行（含测试与 smoke）。
2. 安全设计是这套代码里最值得保留的部分：默认 fail-closed、群 allowlist 双处校验、`/mc` 命令白名单、无 shell/RCON/任意命令执行路径、纯文本出站防 CQ 注入、Hermes 默认关闭且四道闸。细节见第 5 节。
3. **与官方 QQ Bot 的差距不在业务逻辑，而在"接入层"**：鉴权头格式、ID 模型（数字群号 → `group_openid`/`member_openid`）、入站验签算法（HMAC-SHA1 → Ed25519 + 时间戳）、WebSocket 方向（本服务是反连服务端，官方网关要求本服务作为客户端外出连接）、被动回复语义（`msg_id` + `msg_seq`，5 分钟窗口，每消息最多 5 次）完全不同。
4. 可复用度较高的是 **与协议无关的部分**：`service.py` 的编排与命令处理、`models.py`、`state.py`、`mc_adapter.py`、`hermes.py`、`security.py`（部分）。**必须新写/重写**的是 `onebot.py` 整个适配器、`main.py` 的 `/api/v1/onebot/*` 三条路由、以及 `settings.py` 中 OneBot 相关字段与 ID 类型。
5. 最大的单点风险是 **"MC→QQ 广播"在当前实现里是每个 MC chat 事件都向所有 allowlist 群发送一条消息**（`service.py:29-34`）。在官方平台上这属于**主动消息**，受频控与配额约束，且群管理员/用户可关闭主动推送；因此当前"实时全量群聊同步"的语义无法原样搬迁，需要产品层面的降级设计（聚合、@bot 查询、订阅）。本文不承诺普通群全文监听，也不承诺不受限主动推送。
6. 记录与实现的偏差：`docs/VERIFICATION.md` 自称修复后版本"未取得第二次独立审查通过结论"；`pyproject.toml:22` 的测试依赖 `httpx2==2.16.0` 在测试中并未被引用（测试全部 `import httpx`）。

---

## 2. 仓库与运行形态

### 2.1 文件与规模

```
src/mcqq_bridge/  __init__.py(3) hermes.py(95) mc_adapter.py(34) main.py(182)
                  models.py(54) onebot.py(136) personality.py(12) ports.py(25)
                  security.py(10) service.py(138) settings.py(76) state.py(60)
tests/            test_bridge.py(150) test_features.py(103) test_hardening.py(99)
                  test_hermes_admin.py(177) test_review_regressions.py(80)
                  test_security.py(17) test_websocket_safety.py(82)
scripts/          smoke.py(120)
docs/             DEPLOYMENT.md THREAT_MODEL.md VERIFICATION.md
根目录            README.md config.example.yaml .env.example pyproject.toml
                  requirements.txt Makefile Dockerfile docker-compose.yml
```

运行时Python：`.venv/bin/python` = 3.14.9（`pyproject.toml:10` 只要求 `>=3.11`，与容器基础镜像 `python:3.12-slim`（`Dockerfile:1`）不一致，属可接受但需注意的差异）。

### 2.2 依赖

- 直接依赖（`pyproject.toml:11-16`）：`fastapi==0.162.2`、`httpx==0.28.1`、`pydantic-settings==2.16.2`、`uvicorn[standard]==0.54.0`。全部锁死版本。
- 测试依赖（`pyproject.toml:19-23`）：`pytest==8.4.2`、`pytest-asyncio==1.4.1.post1`、**`httpx2==2.16.0`**。
  - `httpx2` 是真实存在的包（PyPI 元数据：`Home-page: https://github.com/pydantic/httpx2`，`Summary: The next generation HTTP client.`，2.16.0 上传时间 2026-10-21T07:09:46Z；来源 <https://pypi.org/pypi/httpx2/json>，获取时间 2026-11-10T03:54Z），**不是拼写劫持**。
  - 但仓库内没有任何代码 `import httpx2`（`grep -rn httpx2 src tests scripts` 无命中），它只额外拉入了第二套 HTTP 栈 `httpcore2`。属依赖冗余，不是安全漏洞；清理属后续改动，本轮不动。
- `requirements.txt` 只有一行 `-e .[test]`，与 `pyproject` 保持单一来源，符合 `docs/DEPLOYMENT.md` 的声明。

### 2.3 进程与部署形态

- 入口：`src/mcqq_bridge/main.py:182` 模块级 `app = create_app()`；`create_app()` 在 `main.py:32-39` 完成装配：两个 OneBot 适配器 + 一个 MC callback + Hermes executor + `BridgeService`。
- 传输二选一：`main.py:36` 按 `settings.onebot_transport`（`http|websocket`）选择适配器实例。
- 容器：`Dockerfile` 非 root（`Dockerfile:12-13`）、`--ws-max-size 65536`（`Dockerfile:18`）、HEALTHCHECK 打 `/healthz`（`Dockerfile:15-16`）。
- Compose：仅映射 `127.0.0.1:8000:8000`（`docker-compose.yml:9`）、`read_only: true`、`cap_drop: ALL`、`no-new-privileges`（`docker-compose.yml:10-14`）。
- 状态存放在进程内存（`state.py:9-18` 的 dataclass），单 worker 假设（`docs/DEPLOYMENT.md:58`）。
- 配置来源只有环境变量/`.env`：`settings.py:11` `SettingsConfigDict(env_file=".env")`。`config.example.yaml` **不会被加载**（README.md:136 已如实声明），它只是同概念的可读示例。

---

## 3. 协议与认证现状

### 3.1 OneBot v11 出站（本服务 → OneBot → QQ）

| 通道 | 实现 | 关键行 | 鉴权 | 成功判据 |
|---|---|---|---|---|
| HTTP | `OneBotHttpAdapter.send_group_message` | `onebot.py:27-48` | `Authorization: Bearer <ONEBOT_ACCESS_TOKEN>`（`onebot.py:30-32`） | `status=="ok" and retcode==0`（`onebot.py:41`） |
| 反向 WS | `OneBotReverseWebSocketHub.send_group_message` | `onebot.py:114-136` | 握手时 `Authorization: Bearer`（`main.py:126-128`） | 收到匹配 `echo` 且 `status=ok/retcode=0` 的响应帧（`onebot.py:98-112`） |

共同点：
- 群号二次校验 allowlist（`onebot.py:28`、`onebot.py:115`），空 allowlist 即不发送。
- 发送体固定 `{"group_id":…, "message":…, "auto_escape": True}`（`onebot.py:36`、`onebot.py:128`），只发纯文本，不构造 CQ 码。
- 失败只记录不含消息/URL/凭据的告警（`onebot.py:46-47`、`onebot.py:133`）。
- WS 侧有 in-flight 上限 32（`onebot.py:118`）、`echo` 关联、超时 fail-closed（`onebot.py:124-134`）、重复 role 拒连（`onebot.py:68-83`）、断线把 pending future 置 False（`onebot.py:85-91`）。

### 3.2 OneBot v11 入站（OneBot → 本服务）

| 通道 | 路由 | 关键行 | 鉴权 |
|---|---|---|---|
| HTTP POST | `/api/v1/onebot/events` | `main.py:100-122` | `ONEBOT_WEBHOOK_TOKEN`（Bearer）**或** `ONEBOT_WEBHOOK_SECRET`（`X-Signature: sha1=<40 hex>`，对原始 body 做 HMAC-SHA1）；两者都未配置 → 401（`main.py:103-117`） |
| 反向 WS | `ONEBOT_WS_PATH`（默认 `/api/v1/onebot/ws`） | `main.py:124-165` | 必须 `onebot_enabled=true` 且 Bearer 匹配 `ONEBOT_ACCESS_TOKEN`，否则 close 1008（`main.py:126-130`） |

- 已知限制（已在 `docs/THREAT_MODEL.md:27` 记录）：HMAC 无时间窗/nonce，重放不去重。
- 路由恒存在，即使 `onebot_enabled=false`：此时**入站命令仍会被处理**（只受凭据约束），只是出站被 `onebot.py:28`/`115` 拒绝。这是"入站开、出站关"的实际语义，迁移时需要复现或明确改掉。

### 3.3 认证矩阵（现状）

| 入口 | 凭据 | 算法/比较 | 关闭开关 | 失败表现 | 代码 |
|---|---|---|---|---|---|
| MC webhook 六类事件 | `MC_WEBHOOK_TOKEN` | `Bearer` 前缀 + `hmac.compare_digest` | `MC_WEBHOOK_REQUIRE_TOKEN=false` 即**完全无鉴权** | 401 | `security.py:6-10`、`main.py:68-70` |
| OneBot HTTP 上报 | token 或 secret（至少一个） | Bearer 常量时间 / HMAC-SHA1 + 正则格式校验 | 无（未配置即拒绝） | 401 | `main.py:103-117` |
| OneBot 反向 WS | `ONEBOT_ACCESS_TOKEN` | Bearer 常量时间 | `onebot_enabled=false` 即拒 | close 1008 | `main.py:126-130` |
| QQ→MC callback | `MC_CALLBACK_TOKEN` | Bearer（可选） | `QQ_TO_MC_ENABLED=false` | 仅日志告警并返回 False | `mc_adapter.py:22-34` |
| Hermes 管理员 | `HERMES_API_TOKEN` | `Bearer` 调上游 | `HERMES_ADMIN_ENABLED=false` | 返回未投递，不伪造 | `hermes.py:45-53` |
| `/healthz` `/readyz` `/api/v1/state/*` `/docs` `/openapi.json` | 无 | — | — | 公开 | `main.py:72-78`、`main.py:167-177`、`docs/THREAT_MODEL.md:26` |

---

## 4. 功能盘点

### 4.1 已实现

| 能力 | 行为 | 代码 |
|---|---|---|
| MC 事件 6 类模型 | `chat/death/join/leave/players/status`，Pydantic 长度上限 | `models.py:6-45`、`main.py:22-29` |
| MC 事件入站 | 统一路由 + `oneOf` OpenAPI schema，body 流式上限 64 KiB | `main.py:80-98`、`main.py:60-66` |
| MC→QQ 通知 | `chat/death/join/leave` 四个独立开关，向**所有** allowlist 群广播 | `service.py:20-45`、`settings.py:21-23` |
| 状态缓存 | 玩家名单/状态/`max_players`，TTL 判定 unknown/fresh/stale，offline 清空名单 | `state.py:20-60` |
| QQ→MC 命令 | 仅 `/mc` 完整首 token；`players`/`status` 查缓存；`say` 需双开关 | `service.py:60-95` |
| `say` 回调 | 固定 JSON POST 到运维配置的 `MC_CALLBACK_URL`，不跟随重定向（httpx 默认） | `service.py:84-91`、`mc_adapter.py:22-34`、`models.py:47-54` |
| OneBot 双传输 | HTTP 与反向 WS（Universal / API / Event 三种 role） | `onebot.py:17-136`、`main.py:124-165` |
| Hermes 管理员通道 | 默认关闭，OpenAI-compatible `/v1/chat/completions` | `hermes.py:26-95` |
| LLM 人设占位 | 永远返回 `None`，`enabled=False`，无 provider | `personality.py:6-12` |
| 自消息过滤 | `user_id == self_id` 丢弃（`ONEBOT_SELF_ID` 或事件内 `self_id`） | `service.py:57-59` |

### 4.2 明确未实现（现状即边界）

- QQ 账号登录、任何 OneBot 实现的内置、官方机器人接入：README.md:3、README.md:156。
- MC 侧插件/mod/日志 sidecar/RCON：`docs/DEPLOYMENT.md:5`。MC 必须自备 HTTP 事件源与 callback 消费者。
- 持久化：无数据库、无 outbox、无审计、无消息重试；重启丢状态（`docs/THREAT_MODEL.md:27`、`docs/DEPLOYMENT.md:58`）。
- 多实例/水平扩展、TLS 终止、限流、入站速率控制：交给部署层（`docs/THREAT_MODEL.md:25`）。
- 用户绑定/白名单、媒体处理、多服务器路由、频道（guild）场景。
- `personality.py` 的 LLM 随机回复（`settings.py:52-54` 有字段但无人读取）。

---

## 5. Hermes 管理员调用安全边界（重点）

调用链：群消息 `/mc hermes <任务>` → `BridgeService.handle_onebot_event` → `_handle_hermes` → `HermesApiExecutor.execute` → `POST {HERMES_API_URL}/chat/completions` → assistant 文本 → 回同一群。

四道闸（全部 fail-closed）：

| 闸 | 判据 | 代码 |
|---|---|---|
| 群 allowlist | 事件 `group_id ∈ ALLOWED_GROUP_IDS`，且必须是 group message | `service.py:51-56` |
| 功能开关 | `HERMES_ADMIN_ENABLED=true` | `service.py:100-101`、`hermes.py:46-47` |
| 操作者 allowlist | `user_id ∈ HERMES_ADMIN_IDS` | `service.py:102-103`、`hermes.py:48-49` |
| 输入/并发/超时 | prompt ≤ 2000 字符；并发 ≤ 1；超时 30s；输出截断 6000 字符 | `service.py:104-105`、`hermes.py:52-53`、`hermes.py:61-64`、`settings.py:48-51`、`hermes.py:95` |

其他边界：
- **不启动进程、不读 profile、不复制 profile 凭据**：只做 HTTP 调用（`hermes.py:26-33` 文档字符串，`hermes.py:55-70` 仅构造 JSON 与 Bearer 头）。
- 失败模式齐全：`disabled` / `unauthorized` / `not_configured` / `prompt_too_long` / `concurrency_limit` / `upstream_failure` / `invalid_response`（`hermes.py:45-84`）。上游异常只记 `"Hermes API request failed; no response delivered"`，不泄漏 URL 与错误体（`hermes.py:73-75`）。
- 上游返回只取 `choices[0].message.content`（`hermes.py:86-95`），不做工具调用解析、不执行返回内容。

需要注意的语义与风险（不改代码，仅登记）：
1. **`hermes` 绕过了 `MC_COMMANDS` 白名单**：`service.py:74-75` 在 `service.py:76` 的白名单检查之前直接分派。即 `MC_COMMANDS` 只约束 `players/status/say`，`hermes` 由 `HERMES_*` 三个开关独立控制。这是有意设计，但容易被误读为"命令总开关"。
2. **上游能力等于完整 agent**：Hermes API server 具备终端等工具（README.md:154、`docs/DEPLOYMENT.md:40`）。因此"管理员 QQ 号被冒用/群被盗用"等价于"可执行任意 agent 任务"，唯一缓解是 openid 级白名单 + 私网 + 独立 API key，桥接器本身不提供任务级策略（无命令沙箱、无审计、无二次确认）。
3. **`actor_id` 是 `int`**：`ports.py:14-15`、`hermes.py:45`、`service.py:106`。官方平台身份是字符串 `member_openid`，迁移时该协议签名必须改为字符串类型，否则 openid 会被 `_int()`（`service.py:122-130`）拒绝或误转。
4. 无对话上下文：每次请求只有一条 `user` 消息（`hermes.py:57`），不带历史、不带群/用户元数据（除 `actor_id` 在本地闸里用过一次），因此 Hermes 侧无法区分来源群，也无法做按群策略。
5. 回复直接回原群（`service.py:112`），未做内容脱敏/长度以外过滤；`hermes_max_response_chars` 截断处不提示"已截断"。

---

## 6. MC 侧依赖（外部前置条件）

- 本仓库**不产生 MC 事件、不消费 MC 指令**。`docs/DEPLOYMENT.md:5` 明确：原版 MC 不原生支持这些 HTTP 端点，必须有日志 sidecar、面板、插件或代理提供。
- 入站契约（MC → bridge）：`POST /api/v1/minecraft/events/{chat|death|join|leave|players|status}`，Bearer，202 表示"已接受"（`main.py:80-98`）。字段见 `models.py:6-45`。
- 出站契约（bridge → MC）：`{"event":"chat","message":…,"group_id":…,"user_id":…,"nickname":…}`（`service.py:87-90`、`models.py:47-54`），Bearer，2xx 视为投递成功（`mc_adapter.py:28-31`）。
- 语义约束：状态是"最近一次 webhook 的快照"，不是主动探测；`join/leave` 增量不能证明完整名单新鲜（`state.py:33-40` 注释、README.md:120）。
- 因此**迁移到官方 QQ Bot 不改变 MC 侧契约**：MC 侧依赖可原样复用，这降低了迁移风险的一半。

---

## 7. 与官方 QQ Bot（API v2）的接口形状差异

> 官方事实来源见第 12 节，获取时间均在 2026-11-10T03:54Z–03:56Z（UTC）。验证等级：**O1** = 官方文档页面直读；**U1** = 官方文档索引/摘要提及但细节未逐页核对，需专家文档确认；**U2** = 未在官方文档确认。

| 维度 | 现状（OneBot v11） | 官方 QQ Bot v2（文档记载） | 迁移影响 |
|---|---|---|---|
| 身份/鉴权头 | `Authorization: Bearer <token>`（`onebot.py:32`） | `Authorization: QQBot <ACCESS_TOKEN>`（O1） | 头前缀不同，`security.py` 不可直接复用 |
| 凭据获取 | 运维手填静态 token（`settings.py:29`） | `POST https://api.bot.qq.com/app/getAppAccessToken`，body `{appId, clientSecret}`，返回 `access_token` + `expires_in`（默认 7200s，过期前 60s 内可取新 token）（O1） | 需要新增 token 缓存/刷新器；当前无任何"取 token"代码 |
| API 基址 | 运维配置的 `ONEBOT_API_URL` + `/send_group_msg`（`onebot.py:35`） | 统一 `https://api.bot.qq.com`，发群消息 `POST /v2/groups/{group_openid}/messages`（O1） | 路径/参数整体替换 |
| 群标识 | 数字 `group_id`（`settings.py:25`） | `group_openid`（字符串，事件 `d.group_openid`）（O1） | allowlist 类型从 `list[int]` → 字符串集合；`_int()` 逻辑不能用于 openid |
| 用户标识 | 数字 `user_id`（`settings.py:44`） | `author.member_openid`（字符串）（O1） | `HERMES_ADMIN_IDS` 必须改为 openid 白名单 |
| 入站事件模型 | OneBot `post_type/message_type/group_id/user_id/raw_message/message`（`service.py:51-68`） | `payload{id,op,s,t,d}`；群 @ 事件 `GROUP_AT_MESSAGE_CREATE`，`d.content` 已剥离 @ 前缀，`d.group_openid`、`d.author.member_openid`、`d.id`（用于被动回复）（O1） | 需要新增事件规范化层（见第 8 节组件评估） |
| 入站验签 | HMAC-SHA1 over raw body，`X-Signature: sha1=<40hex>`（`main.py:109-117`） | Ed25519：`X-Signature-Ed25519` + `X-Signature-Timestamp`，签名体 = `timestamp + body`，公钥由 Bot Secret repeat 到 32 字节作 seed 派生（O1） | 验签实现完全不同，且需 `cryptography`/`pynacl` 类依赖（当前依赖树里没有 Ed25519 实现） |
| 回调地址验证 | 无 | `op=13`，请求 `d` 含 `plain_token`/`event_ts`，需按其算法返回 `signature`（O1） | 必须新增握手端点逻辑，否则平台不会启用回调 |
| 回调网络条件 | 任意私网 HTTP 即可（`docker-compose.yml:9`） | 需 HTTPS 回调地址；可配置端口仅 80/443/8080/8443（O1） | 现状 `127.0.0.1:8000` 不可用；需要 TLS 终止与公网可达（本机已有 Caddy 与 frp，但属部署决策，本轮不评估） |
| WS 方向 | **本服务是服务端**，OneBot 作为客户端反连（`main.py:124`、`onebot.py:51-83`） | 本服务作为**客户端**连 `wss://api.bot.qq.com/websocket`，先取 WSS 接入点，`op=2 Identify` 带 `intents`，心跳 `op=1`，`op=6 Resume`（O1） | `OneBotReverseWebSocketHub` 整体不可复用；方向反了，且要处理 intents 位图与 session/resume |
| 出站消息语义 | 无 `msg_id`，消息体只有 `group_id/message/auto_escape`（`onebot.py:36`） | 主动消息（无 `msg_id`）或被动回复（带 `msg_id`，5 分钟内有效，每消息最多回复 5 次；`msg_seq` 用于去重，重复 `msg_id+msg_seq` 会失败）（O1） | 现有代码**没有任何 msg_id/msg_seq 关联**，无法做被动回复；需要把入站事件 id 传进出站路径 |
| 主动推送配额 | 无限制（本服务自己不限速） | Bot 维度：企业/个人证件认证 60 qpm，未认证 30 qpm；单关系维度 20 qpm，每群每天最多接收 1000 条；接口 100 QPS（O1） | `service.py:29-34` 的"每事件×每群"广播在官方平台会超配额；需要聚合/队列/退避 |
| 用户可控开关 | 无 | 用户/群管理员可关闭"允许主动发送"（`GROUP_MSG_REJECT`/`C2C_MSG_REJECT` 等事件）（O1 事件表 + 主动消息说明） | 主动推送可能被静默拒绝，必须有失败分类与降级 |
| 群全量消息 | OneBot 下可收到群内全部消息（协议层允许） | 文档索引列有 `GROUP_MESSAGE_CREATE`（"群消息（全量模式）"）（U1）；其申请条件/资质/是否默认可用**未在本次查阅中确认** | **不得承诺普通群全文监听**；替代方案（@bot 查询、聚合、订阅）由专家文档评估 |
| 撤回/富媒体 | 无（只发纯文本） | 有 `DELETE /v2/groups/{group_openid}/messages/{message_id}`、`msg_type=7` 富媒体、Markdown、内嵌键盘（O1） | 属可选增强，非迁移必需 |
| 历史域名 | — | 官方当前文档写统一域名 `https://api.bot.qq.com`；历史 SDK/社区代码中常见 `api.sgroup.qq.com`（U2） | 实现时以控制台与最新文档为准，不要照抄旧 SDK |

---

## 8. 组件可复用性评估

| 模块 | 结论 | 理由（含行号） |
|---|---|---|
| `models.py` | **可复用（微调）** | MC 事件模型与协议无关（`models.py:6-45`）。`CallbackEvent`（`models.py:47-54`）字段名 `group_id/user_id` 是 OneBot 语义，官方迁移后语义变为 openid，需改名或加新模型。 |
| `state.py` | **可原样复用** | 纯内存快照 + TTL，无协议依赖（`state.py:9-60`）。 |
| `service.py` 编排与命令解析 | **可复用，需重写入口** | `handle_mc_event`（`service.py:20-34`）与 `_format_mc`（`service.py:36-45`）可直接用；`handle_onebot_event`（`service.py:47-113`）与 OneBot 事件形状强耦合，需要"事件规范化层"把官方事件转成内部统一结构后再复用命令逻辑。 |
| `mc_adapter.py` | **可原样复用** | 固定 JSON + Bearer + 超时，与 QQ 协议无关（`mc_adapter.py:14-34`）。 |
| `hermes.py` | **可复用，需改类型** | 只依赖 OpenAI-compatible HTTP（`hermes.py:26-95`）；`actor_id: int`（`hermes.py:45`、`ports.py:14-15`）需改 `str`。 |
| `security.py` | **部分复用** | `bearer_matches`（`security.py:6-10`）可继续用于 MC webhook / callback；**不能**用于 `QQBot <token>` 与 Ed25519 验签。 |
| `ports.py` | **接缝可复用，契约要扩** | `OutboundOneBot.send_group_message(group_id:int, message:str)->bool`（`ports.py:6-7`）方向正确，是可插拔的关键抽象；但缺 `msg_id/msg_seq`、缺失败分类（只返回 bool）、缺"目标类型"（群/单聊）。 |
| `main.py` 路由层 | **需重写 QQ 部分** | `/api/v1/onebot/events`（`main.py:100-122`）与 `/api/v1/onebot/ws`（`main.py:124-165`）是 OneBot 专属；`/api/v1/minecraft/*`（`main.py:80-98`）与 `/api/v1/state/*`（`main.py:167-177`）、`/healthz` `/readyz`（`main.py:72-78`）可保留。 |
| `onebot.py` | **不可复用（整体替换）** | 出站路径/参数（`onebot.py:27-48`）、WS 服务端方向（`onebot.py:51-136`）都是 OneBot v11 专属。可作为"参考实现"保留旁路，不建议在原文件里加分支。 |
| `settings.py` | **需重构字段** | OneBot 段（`settings.py:25-34`）整体替换为 appId/appSecret/webhook secret/port/env 等；`allowed_group_ids` 与 `hermes_admin_ids` 的 `int` 归一化（`settings.py:68-73`）要改成字符串集合。`mc_*`、`hermes_*` 段保留。 |
| `personality.py` | **可保留占位** | 无协议依赖（`personality.py:6-12`）。 |
| `tests/` | **部分复用** | MC 事件/状态/限长的测试（`test_bridge.py`、`test_features.py` 大部分、`test_security.py`、`test_review_regressions.py:54-66`）仍有效；OneBot HTTP/HMAC/WS/`self_id` 相关（`test_features.py:68-87`、`test_hardening.py:56-86`、`test_websocket_safety.py` 全部、`test_review_regressions.py:32-44`）在迁移后需替换。 |
| `scripts/smoke.py` | **需重写** | 断言 OneBot `send_group_msg` 体与 `auto_escape`（`smoke.py:27-31`），且用 `ONEBOT_*` 环境变量（`smoke.py:54-63`）。 |
| 部署工件 | **需新增** | `docker-compose.yml` 的 loopback 绑定与官方"HTTPS 回调 + 80/443/8080/8443"冲突（见第 7 节）；`Dockerfile` 的只读根文件系统与非 root 可保留。 |

---

## 9. OneBot 耦合点清单（按文件）

`grep -rniI onebot src scripts tests --include=*.py | wc -l` = 135 处：`src/` 65 处（`main.py` 29、`onebot.py` 17、`settings.py` 10、`service.py` 8、`ports.py` 1），`scripts/` 7 处，`tests/` 63 处。

| 位置 | 耦合性质 | 迁移动作 |
|---|---|---|
| `main.py:17`、`34-39` | 装配层硬编码两个 OneBot 适配器 | 改为按"平台适配器"工厂装配 |
| `main.py:36` | 传输开关 `http / websocket` | 官方平台下该二选一不再对应真实差异（HTTP 回调 vs 网关 WS），需重新建模 |
| `main.py:78` | `/readyz` 暴露 `onebot_enabled` | 改字段名，且要加"token 是否有效/是否在配额内"以外的真实就绪信息（避免虚假 ready） |
| `main.py:100-122` | OneBot HTTP 上报模型与 HMAC 验签 | 新增官方回调路由（`op=0/12/13` + Ed25519） |
| `main.py:124-165` | 反向 WS **服务端** + echo 关联 | 改为网关客户端（Identify/Heartbeat/Resume），保留"发帧≠成功"的确认思想 |
| `onebot.py:27-48` | `send_group_msg` + Bearer + retcode 判定 | 新增 `POST /v2/groups/{group_openid}/messages` + `QQBot` 头 + `msg_id/msg_seq` |
| `onebot.py:51-136` | WS hub（role 去重、pending、超时） | 不可复用；其"并发上限 + 超时 + 断线 fail-closed"的设计经验应保留 |
| `service.py:12-18`、`34`、`94`、`112` | 依赖 `OutboundOneBot` 抽象（唯一的协议接缝） | 扩契约：目标（`group_openid`）、`reply_to`（`msg_id`）、`msg_seq`、结构化失败原因 |
| `service.py:47-113` | 入站事件形状（`post_type/message_type/group_id/user_id/message` 数组段） | 抽出 `InboundEvent` 规范化数据类，命令处理复用 |
| `settings.py:25-34` | `onebot_*` 十个字段 | 替换为 `qqbot_app_id/qqbot_app_secret/qqbot_webhook_secret/qqbot_callback_port…` |
| `settings.py:68-73` | `int` 归一化（逗号分隔字符串） | openid 场景改为字符串集合，且保留"空集合即不激活任何群"的语义 |
| `ports.py:6-7` | `send_group_message(group_id:int, message:str)` | 签名升级（见上） |

---

## 10. 迁移风险登记

| 编号 | 风险 | 等级 | 依据 | 缓解方向（不含实现） |
|---|---|---|---|---|
| R1 | "MC→QQ 实时全量同步"语义在官方平台不成立 | **高** | 主动消息受 qpm/日配额约束，且可被关闭（第 7 节，O1）；本实现每事件每群一条（`service.py:29-34`） | 产品层改为聚合/摘流/订阅；不承诺全文监听 |
| R2 | 入站身份模型从数字 ID 换成 openid，权限白名单直接失效 | **高** | `settings.py:25`、`44`；`service.py:53-56`、`102-103` | 白名单改字符串 openid；迁移期双栈并存需防"旧 ID 仍生效" |
| R3 | 无 `msg_id`/`msg_seq`，无法被动回复，只能走主动消息 | **高** | `onebot.py:36`、`128`；官方被动回复规则（O1） | 事件规范化层必须把 `d.id` 贯穿到出站；`msg_seq` 递增与去重 |
| R4 | WS 方向反转，现有 hub 代码整体作废 | 中高 | `main.py:124`（服务端）vs 官方网关（客户端，O1） | 新写网关客户端；保留 echo/超时/并发上限经验 |
| R5 | 验签算法与依赖缺失 | 中高 | `main.py:109-117`（HMAC-SHA1）；官方 Ed25519（O1）；当前依赖无 Ed25519 实现 | 新增密码学依赖；先写向量测试再接线 |
| R6 | 回调需 HTTPS 与 80/443/8080/8443，现有 loopback 部署不可用 | 中高 | `docker-compose.yml:9`；官方回调端口限制（O1） | 部署层 TLS 终止 + 反向代理；本轮不评估具体方案 |
| R7 | `hermes` 通道绕过 `MC_COMMANDS`，且上游具备终端能力 | 中 | `service.py:74-76`；README.md:154 | 迁移时明确定义"管理员命令"与"业务命令"两套策略；管理员白名单用 openid |
| R8 | 主动消息被静默拒绝/去重，桥接器误判成功 | 中 | `onebot.py:41` 只判 `retcode==0`；官方有去重错误码（O1 错误码表：40054005 消息被去重） | 失败分类映射（配额/去重/非群成员/关闭主动推送），不要用单一 bool |
| R9 | 内存状态 + 单 worker，重启即丢，且迁移期双平台并存会放大不一致 | 中 | `state.py:9-18`、`docs/DEPLOYMENT.md:58` | 迁移期只启用一条链路；不宣称 exactly-once（`docs/THREAT_MODEL.md:27`） |
| R10 | 测试基线覆盖 OneBot 而非官方协议，绿灯会给出虚假信心 | 中 | `tests/` 中 OneBot 断言（第 8 节） | 新增官方协议向量测试；在 CI 里区分两套 adapter 测试 |
| R11 | 群/用户主动推送开关状态未知 | 中 | `GROUP_MSG_REJECT`/`GROUP_MSG_RECEIVE` 事件（O1） | 记录开关事件；被关闭时降级为"仅被动回复" |
| R12 | `GROUP_MESSAGE_CREATE`（全量模式）可得性未确认 | 中 | 官方索引提及（U1），资质/权限未核实 | 交由 `docs/research/QQ_BOT_PLATFORM_FINDINGS.md` 结论决定产品形态；在此之前不写承诺 |
| R13 | 依赖冗余/双 HTTP 栈带来审计面 | 低 | `pyproject.toml:22` `httpx2==2.16.0` 未使用，引入 `httpcore2` | 后续清理；不影响功能 |
| R14 | Python 版本矩阵不一致（3.12 镜像 / 3.14 本地 / >=3.11 声明） | 低 | `Dockerfile:1`、`pyproject.toml:10`、本地 3.14.9 | 统一目标版本后再加新依赖（Ed25519 库对版本敏感） |

---

## 11. 尚未实现项清单（供后续 backlog 使用，本文件不排期）

- F1 官方 access_token 获取与刷新（含 60s 提前刷新、失败退避、并发只刷新一次）。
- F2 回调端点：`op=13` 地址验证、`op=12` ACK、`op=0` 事件分发、Ed25519 验签 + 时间戳/重放防护。
- F3 事件规范化层：官方事件 → 内部统一结构（群 openid、成员 openid、消息 id、内容、时间戳）。
- F4 出站被动回复（`msg_id` + `msg_seq`）与主动消息通道分离。
- F5 配额/频控与队列：Bot 维度、单关系维度、每群每日上限、429/错误码退避。
- F6 网关客户端（Identify/Heartbeat/Resume/intents 位图、session 上限）。
- F7 openid 白名单模型（群、管理员）与迁移期兼容策略。
- F8 `hermes` 通道策略化（管理员范围、审计、内容过滤、响应截断提示）。
- F9 失败分类与可观测性（区分 accepted / delivered / 平台拒绝原因，不泄漏凭据）。
- F10 部署工件：HTTPS 回调、端口、TLS、反向代理与只读容器共存。
- F11 官方协议测试向量与 smoke（当前 smoke 只覆盖 OneBot）。
- F12 依赖清理（`httpx2`）与 Python 版本统一。

---

## 12. 官方来源与验证证据

### 12.1 官方来源（均为 2026-11-10T03:54Z–03:56Z UTC 直读）

| 编号 | URL | 用于本文的事实 |
|---|---|---|
| O1-a | <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html> | `POST https://api.bot.qq.com/app/getAppAccessToken`；`{appId, clientSecret}` → `access_token`/`expires_in`（7200s，过期前 60s 可取新）；统一基址 `https://api.bot.qq.com`；头 `Authorization: QQBot {ACCESS_TOKEN}` |
| O1-b | <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html> | `payload{id,op,s,t,d}`；opcode 0/1/2/6/7/9/10/11/12/13；Webhook 端口限 80/443/8080/8443；`op=13` 地址验证（`plain_token`/`event_ts` + Ed25519）；intents 位图与 `GROUP_AND_C2C_EVENT (1<<25)` |
| O1-c | <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html> | 验签头 `X-Signature-Ed25519`、`X-Signature-Timestamp`；签名体 `timestamp + body`；公钥由 Bot Secret repeat 至 32 字节作 seed 派生 |
| O1-d | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html> | `POST /v2/groups/{group_openid}/messages`；`msg_id` 被动回复 5 分钟有效、每消息最多 5 次；`msg_seq` 去重（相同 `msg_id+msg_seq` 重复发送失败，错误码 40054005）；主动消息频控：Bot 维度 60/30 qpm、单关系 20 qpm、每群每天 1000 条；接口 100 QPS；错误码 40054003 机器人非群成员 |
| O1-e | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html> | 事件名 `GROUP_AT_MESSAGE_CREATE`，Intent `GROUP_AND_C2C_EVENT (1<<25)`；`d.id`（被动回复用）、`d.group_openid`、`d.author.member_openid`、`d.content` 已剥离 @ 前缀 |
| O1-f | <https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html> | 主动/被动消息定义；用户/群可关闭"允许主动发送"；群聊接收事件含 `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE` |
| O1-g | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/index.html>（搜索摘要） | 接口索引列有 `GROUP_MESSAGE_CREATE`（"群消息（全量模式）"）——**U1：申请条件未核实** |
| O2 | <https://pypi.org/pypi/httpx2/json> | `httpx2` 为 pydantic 接管的真实包，2.16.0 上传 2026-10-21T07:09:46Z |

> 说明：`bot.q.qq.com` 的部分旧路径会回落到"启动接入"页；上表中 `autogen/*` 页面为当前有效页面。历史 SDK 中的 `api.sgroup.qq.com` 未在本次查阅的官方页面出现，标记为 **U2**，不作为实现依据。

### 12.2 本地验证证据（真实执行）

```
$ .venv/bin/python -V
Python 3.14.9

$ .venv/bin/python -m pytest -q -W error
.....................................................                    [100%]
53 passed in 1.17s

$ .venv/bin/python -m pytest -q --collect-only | tail -1
53 tests collected in 0.15s

$ .venv/bin/python scripts/smoke.py
...
SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only
(EXIT=0)
```

测试分布（按 pytest 收集数，参数化已展开）：`test_bridge.py` 6、`test_features.py` 10、`test_hardening.py` 11、`test_hermes_admin.py` 6、`test_review_regressions.py` 12、`test_security.py` 2、`test_websocket_safety.py` 6，合计 53。

验证边界（如实声明）：
- `smoke.py` 启动真实 uvicorn、真实 TCP/HTTP，但 OneBot 与 MC 都是**本地模拟 peer**（`scripts/smoke.py:1-5`），**没有真实 QQ 登录、没有真实 OneBot 实现、没有真实 Minecraft**。
- 本轮未执行 Docker 构建/Compose：`docs/VERIFICATION.md:13-15` 记录 czauw 不在 docker 组、裸 `sudo docker` 无法在无终端环境认证；本轮同样未尝试，故 Docker 路径仍为**未验证**。
- Hermes 真实端到端未验证：本机 9119 是 dashboard，8642 官方 API server 未启用（`docs/VERIFICATION.md:17-20`、`docs/DEPLOYMENT.md:44`）。
- 所有官方平台行为结论均来自文档阅读，**没有任何真实 AppID/AppSecret、没有调用过开放接口**。

---

## 13. 不确定项与交接

1. `GROUP_MESSAGE_CREATE`（群消息全量模式）的申请条件、资质要求、是否默认可用——**未核实**，直接影响"MC→QQ 全量群聊同步"能否合规成立。以 t_34b670ec 的 `docs/research/QQ_BOT_PLATFORM_FINDINGS.md` 为准。
2. 群机器人申请主体资质、测试沙箱、上线审核流程——未在本文核实，同上。
3. `group_openid`/`member_openid` 在同一 AppID 下是否稳定、是否跨群/跨 App 复用——未核实，影响白名单持久化设计。
4. 主动消息"每群每天 1000 条"的计量口径（按群还是按关系、是否含被动回复）——文档表述为"每个群 1 天最多接收 1000 条"，口径细节未逐字核对，标记 **U1**。
5. 官方推荐的生产部署形态（Webhook vs Gateway）当前取舍——文档同时描述两种方式，未给明确建议；迁移选型由专家文档与 Planner 决定。
6. 本仓库 `docs/VERIFICATION.md:24` 自述"修复后的版本未取得第二次独立审查通过结论"——即当前代码虽测试全绿，但缺乏独立复核结论，属流程风险而非代码缺陷。

**交接说明**：本文只做现状与差距盘点，不含实现计划。后续 `docs/QQ_BOT_INTEGRATION.md` 与 `docs/QQ_BOT_CONFIGURATION.md` 应基于本文第 7、8、10、11 节，并与 `docs/research/QQ_BOT_PLATFORM_FINDINGS.md` 对齐；凡本文标 **U1/U2** 的项，不得在后续文档中升级为确定事实。
