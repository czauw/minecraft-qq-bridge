# Minecraft ↔ QQ 互通桥接器（QQ 官方机器人 × QueQiao 多服务器）

把 **多个 Minecraft 服务器**（每个服务器装一个 [QueQiao](https://queqiao-docs.pages.dev/) 插件/模组）
接到 **一个 QQ 官方机器人** 的 **一个群** 里：

* QQ 群消息 → 广播到所有在线 MC 服务器；
* MC 的聊天 / 进入 / 离开 / 死亡 → 转发到 QQ 群；
* `/list` 实时查询所有服务器的在线玩家（含禁用/离线状态）；
* 每个服务器一条独立 WebSocket，单台断线不影响其它服务器，自动退避重连并重新同步名单。

> QQ 侧**只用官方接口**（OpenAPI + 官方 WebSocket 网关），不涉及 OneBot、NapCat 等非官方登录协议。
> 官方机器人需要在 QQ 开放平台注册并通过审核，详见下文。

---

## 1. 它能做什么 / 不能做什么

| 能力 | 状态 | 说明 |
|---|---|---|
| 官方 access_token 获取与提前刷新 | ✅ | `POST /app/getAppAccessToken`，并发刷新加锁；网关/发送遇到 401 会作废 token 并刷新一次 |
| 官方 WebSocket 网关（Hello/Identify/心跳/Resume/重连） | ✅ | 支持 `op=6/7/9/11`，心跳无 ACK 主动重连 |
| 官方关闭码分流 | ✅ | 按官方表区分「Resume（4008/4009）／重新 Identify（4006/4007/4900~4913）／终止（4001/4002/4010~4014/4914/4915）」，不再无休止重连；传输层断链保留会话尝试 Resume |
| 群全量消息 / 仅 @机器人 | ✅ | `receive_mode: all` 消费全量 + @ 并按 `message_id` 统一去重；`mention` 只消费 @ 事件，普通群消息不广播 |
| 群文本发送（被动回复、主动通知） | ✅ | `msg_type=0`；被动回复带 `msg_id` + 递增 `msg_seq`（最多 5 片）；**HTTP 200 但响应不是合法成功结构（坏 JSON/数组/空 body/缺消息 id）不算投递成功** |
| 群内状态探测（bot_state） | ✅ | 启动 + 周期 `GET /v2/groups/{group_openid}/bot_state`；`allow_proactive_msg=false` 禁止主动发送，`11253`/探测失败按 unknown 降级并在 `status`/`readyz` 暴露 degraded |
| 权限/配额拒绝的冷却 | ✅ | HTTP 429 与业务码 304003/40034100/40034105 触发有界指数冷却，避免每条事件重复打被拒接口，冷却后自动恢复 |
| 图片/附件 | ✅ 占位符 | 图片显示为 `[图片]`，其它附件 `[附件]`，**不下载任何文件** |
| 多台 QueQiao 独立连接 / 禁用项 / 唯一性校验 | ✅ | `name` 唯一 + `host:port` 全局唯一（禁用项也校验） |
| `/list` 全部 / 单个 / 不存在 | ✅ | 文本格式严格固定，见 §7 |
| 玩家名单“权威完整查询” | ⚠️ 见 §8 | QueQiao 已发布版本没有完整名单 API；默认走官方 `send_rcon_command` 执行 `list` |
| 查询与 join/leave 合并 | ✅ | 快照返回后重放「查询期间发生的 join/leave」（UUID 优先），断线/重连后的旧查询结果直接作废 |
| 死亡事件 | ⚠️ 平台限制 | QueQiao 官方文档说明 vanilla / Velocity **不支持** `PlayerDeathEvent` |
| 真实 QQ / 真实 Minecraft 联调 | ❌ 未验证 | 本轮没有真实机器人与 MC 环境，只做了协议级模拟（见 `docs/VERIFICATION.md`） |

---

## 2. 环境要求

* Python **3.11+**（开发验证使用 3.14）
* 依赖已固定：`fastapi==0.162.2`、`httpx==0.28.1`、`pydantic-settings==2.16.2`、
  `uvicorn[standard]==0.54.0`、`PyYAML==6.0.3`、`websockets==13.1`（见 `pyproject.toml`）
* 出网：能访问 `https://api.bot.qq.com`（或你配置的 `bot.api_base`）与 QueQiao 的 WebSocket 端口

### 2.1 本轮确切的支持版本（不是「所有平台都验证过」）

| 组件 | 本轮核对/验证到的版本 | 依据 | 状态 |
|---|---|---|---|
| 桥接器 | Python 3.14.9 + 上列依赖 | `pyproject.toml` / `docs/VERIFICATION.md` | ✅ 本机实测 |
| QueQiao | GitHub release **v0.5.0**（资产 `QueQiao-vanilla+minecraft-0.5.0.jar`），内置 queqiao-core **0.6.8** | GitHub Releases API（2026-04-06 发布） | ⚠️ 只核对源码与文档，**未装真实插件** |
| QueQiao 分发 | Modrinth 项目 `queqiao` 共 983 个版本文件，按 loader 分别发布；核对时最新为 `0.5.3+fabric-26.3` | Modrinth API | ⚠️ 未逐个验证 |
| 协议 | QueQiao Protocol V2（`api`/`data`/`echo`，`post_type=message/notice`） | 官方文档 `api/v2/*`、`events/v2/*` | ✅ 本地 WS 服务端按该形状模拟 |
| QQ 官方接口 | OpenAPI v2 + 官方 WebSocket 网关（`op=0/1/2/6/7/9/10/11`） | 官方文档 | ⚠️ 只按文档模拟，**无真实 AppID/群权限** |
| Minecraft 服务端 | 未运行任何真实服务端 | — | ❌ 未验证 |

> 一句话：**「桥接器代码 + 本地协议模拟」验证通过；「真实 QueQiao 插件 + 真实 Minecraft + 真实 QQ」未验证。**
> 具体 loader / MC 版本范围见 §4 的矩阵（来自上游 README 徽章，不代表本仓库实测兼容）。


```bash
cd /home/czauw/minecraft-qq-bridge
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
```

---

## 3. 准备 QQ 官方机器人

1. 打开 <https://q.qq.com/>（QQ 开放平台），创建**机器人**应用，提交审核。
2. 在「开发 → 开发设置」拿到 **AppID** 与 **ClientSecret**。
3. 在「开发 → 沙箱配置 / 发布」把机器人加入你的测试群（或正式发布后加群）。
4. 在群内给机器人必要的消息权限：
   * 只想被 @ 时工作：使用 `receive_mode: "mention"`，无需「接收所有消息」权限；
   * 要转发群里的**所有**消息：需要在群里开启机器人「接收所有消息」（官方称全量模式，
     事件 `GROUP_MESSAGE_CREATE`），否则只能收到 `@机器人` 的事件。
5. 主动消息（MC → QQ 推送）受平台频控与权限限制：
   官方文档给出主动消息频控（Bot 维度 60 qpm / 20 qpm、单群每天 1000 条等，随认证状态变化）。
   若平台未授予主动消息权限，桥接器会记录失败原因并停止，不会把失败报成成功。

> 机器人加入群后，**群 OpenID 与群号不同**，必须按下节获取。

### 3.1 获取 `group_openid`

`group_openid` 只能在收到该群的事件后从事件体里读到，桥接器不会猜、也不会把群号当 OpenID：

1. 最简单的办法：用仓库自带脚本连官方网关，收到一条群事件就会打印出来（官方**没有**「列出机器人所在群」的接口，`/users/@me/groups` 不存在，所以只能从事件里读）：

   ```bash
   # 先在开放平台把机器人配进沙箱群；运行期间不要同时跑桥接器（网关会话会互相抢占）
   .venv/bin/python scripts/discover_group_openid.py --app-id 你的AppID --env-file .env
   # 然后在该群里 @机器人 发一句话
   ```

2. 或者用官方开发者工具 / API 调试台查看该群事件的 `d.group_openid` 字段
   （形如 32 位十六进制串）。
3. 拿到后写入 `config.yaml` 的 `bot.group_openid`。

> `group_openid` 是「机器人 × 群」维度的标识，不是群号；换机器人或换群都要重新取。

> 本桥接器不保存群号→OpenID 映射，也不做多群支持：**一个机器人 + 一个目标群**。

---

## 4. 准备 QueQiao

QueQiao（鹊桥）是装在 **每个 Minecraft 服务端** 上的插件/模组，把玩家事件用 WebSocket 发出来，
并接收 JSON 消息广播给玩家。

* 下载与文档：<https://queqiao-docs.pages.dev/>（GitHub：<https://github.com/17TheWord/QueQiao>）
* 本轮核对的发布版本：**QueQiao v0.5.0**（GitHub release 资产 `QueQiao-vanilla+minecraft-0.5.0.jar`，
  内置 queqiao-core/tool **0.6.8**），Protocol V2（`api`/`data`/`echo`）。
* 版本分发的准确事实：GitHub Releases 每个 tag **只挂 1 个 jar**；按 loader 区分的产物在
  Modrinth（项目 `queqiao`，核对时共 983 个版本文件，最新 `0.5.3+fabric-26.3`）与 CurseForge 发布。
  因此「GitHub 最新 release = v0.5.0」不等于「最新插件版本是 0.5.0」。
* loader / MC 版本范围（**上游 README 徽章口径，本仓库未实测**，且非「已验证兼容」）：

  | loader | 上游声明范围 | 本仓库验证状态 |
  |---|---|---|
  | Spigot | 1.12.2 – latest | ❌ 未装真实插件 |
  | Paper | 1.17.1 – latest | ❌ 未装真实插件 |
  | Folia | 1.21.4 – latest | ❌ 未装真实插件 |
  | Velocity | 3.3.0 | ❌ 未装真实插件（且**不支持死亡事件**） |
  | Forge | 1.7.10 – 1.21 | ❌ 未装真实插件 |
  | Fabric | 1.16.5 – 1.21.8 | ❌ 未装真实插件 |
  | NeoForge | 1.21.1 | ❌ 未装真实插件 |
  | vanilla | 官方文档说明**不支持死亡事件** | ❌ 未装真实插件 |

  桥接器只依赖 Protocol V2 的 `api`/`data`/`echo` 与四类事件，理论上与具体 loader 无关，
  但**没有任何一个 loader 被真实联调过**；死亡事件在 vanilla / Velocity 上不可用。

每台服务器 `config.yml` 至少需要：

```yaml
server_name: "主服"          # 必须与桥接器 config.yaml 的 name（或 self_name）完全一致
access_token: ""            # 可选；填了就要在桥接器里填同样的值
websocket_server:
  enable: true
  host: "0.0.0.0"           # 桥接器能访问到的地址
  port: 8080                # 每台服务器端口可相同也可不同，桥接器按 host+port 区分
subscribe_event:
  player_chat: true
  player_join: true
  player_quit: true
  player_death: true
rcon:
  enable: true              # /list 权威玩家名单需要（桥接器只是调用 QueQiao 的官方 RCON 接口）
  port: 25575
  password: "你的RCON密码"
```

> **关于 `x-self-name`**：桥接器会把名称做 URL 编码后放进握手头（QueQiao 会 `URLDecoder` 解码），
> 因此中文服务器名可以正常工作；名称必须与 QueQiao 的 `server_name` 完全一致，否则会被 1008 拒绝。

### 4.1 `/list` 需要 RCON：**两边都要配**（缺一不可）

`/list` 的权威名单由 QueQiao 通过 **它自己的** RCON 客户端执行 `list` 得到；桥接器不直连 RCON、
也不监听任何端口。所以必须同时配置 **Minecraft 服务端** 与 **QueQiao 插件** 两侧：

**① Minecraft 服务端（`server.properties`，或 Paper 的 `server.properties`）**

```properties
enable-rcon=true
rcon.port=25575              # 端口可自定义，必须与 QueQiao 的 rcon.port 相同
rcon.password=换成你自己的强口令
broadcast-rcon-to-ops=false  # 可选
```

改完**重启**服务端，并确认 25575 只对 QueQiao 所在主机开放（不要暴露到公网）。
验证：`mcrcon -H 127.0.0.1 -P 25575 -p '口令' list` 能返回 `There are N of a max of M players online...`。

**② QueQiao 插件（每台服务器的 `config.yml`）**

```yaml
rcon:
  enable: true               # 必须 true，否则 /list 会显示「离线」
  port: 25575                # 必须与 server.properties 的 rcon.port 一致
  password: "换成你自己的强口令"   # 必须与 rcon.password 完全一致
```

> 官方配置里 `rcon` 只有 `enable` / `port` / `password` 三个字段（没有 host），
> QueQiao 以 `127.0.0.1` 连接本机 RCON；如果 MC 不在这台机器上，请用端口转发让
> `127.0.0.1:rcon.port` 指向真实的 MC 服务端。

**③ 桥接器**：默认 `players.mode: auto`（先 `send_rcon_command` 再 `get_status` 兜底），
不需要填任何 RCON 地址或密码——桥接器只发一条 JSON `{"api":"send_rcon_command","data":{"command":"list"}}`。

常见失败：只配了 QueQiao 没开 MC 的 `enable-rcon` → QueQiao 返回 400/失败 → `/list` 显示「离线」；
两侧口令不一致 → 同样失败。两者都属于**有意的安全失败**，不会假装成功。

---

## 5. 安装与启动

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml：至少填 bot.app_id、bot.client_secret（或用 ${QQ_BOT_CLIENT_SECRET}）、bot.group_openid、servers
cp .env.example .env
# 编辑 .env：填真实 ClientSecret

# 1) 只校验配置：不联网、不需要真实密钥
python -m mcqq_bridge --config config.yaml --check-config --env-file .env

# 2) 启动（.env 需要显式 --env-file，程序不会隐式读取）
python -m mcqq_bridge --config config.yaml --env-file .env
# 或者交给 shell，然后不传 --env-file：
#   set -a; . ./.env; set +a
#   python -m mcqq_bridge --config config.yaml
# 或者
make run
```

> **`.env` 不会自动加载**：`Settings` 只读 `os.environ`，本程序没有隐式 dotenv。
> 想让 `.env` 生效，必须 `--env-file .env`（或 `export`/`set -a`）。
> `--env-file` 只支持 `KEY=VALUE`、可选 `export` 前缀与成对引号，已有环境变量不会被覆盖；
> 文件不存在或某行非法会以退出码 2 报错。

启动后：

* 日志会打印加载的每台服务器（名称/host:port/启用状态/查询方式），**不会打印密钥或消息正文**；
* 只读状态接口（默认 `127.0.0.1:18080`）：`/healthz`、`/readyz`、`/api/v1/status`；
* `Ctrl+C` / `SIGTERM` 会优雅关闭：取消任务、关闭 WebSocket 与 HTTP 连接。

`--check-config` 的返回码：`0` 结构通过（可能仍提示运行前需补齐的密钥），`2` 配置错误并只打印字段路径 + 原因。

---

## 6. 配置字段逐项说明

模板见 `config.example.yaml`（每个字段都有中文注释）。摘要：

### `bot`（QQ 官方机器人）

| 字段 | 默认 | 说明 |
|---|---|---|
| `app_id` | 空 | 开放平台 AppID；运行前必填 |
| `client_secret` | 空 | ClientSecret；建议 `${QQ_BOT_CLIENT_SECRET}`；运行前必填 |
| `group_openid` | 空 | 目标群 OpenID（不是群号）；运行前必填 |
| `api_base` | `https://api.bot.qq.com` | 官方 OpenAPI 根地址（旧应用可用 `https://api.sgroup.qq.com`） |
| `gateway_url` | 空 | 留空则 `GET /gateway` 动态获取；调试时可写死 `wss://...` |
| `intents` | `33554432`（1<<25） | 群与 C2C 事件 `GROUP_AND_C2C_EVENT` |
| `receive_mode` | `all` | `all`=群全量+@（需群权限，按 `message_id` 统一去重）、`mention`=只消费 @机器人 事件 |
| `proactive_enabled` | `true` | 是否允许 MC 事件主动推送；关闭后只回复 |
| `send_rate_per_minute` | `20` | 本地限速（全部发送共用）；平台另有频控 |
| `request_timeout_seconds` | `10` | 单次 HTTP 超时 |
| `token_refresh_skew_seconds` | `60` | token 提前刷新窗口 |
| `reconnect_max_seconds` | `60` | 网关重连最大退避 |
| `max_message_chars` | `1500` | 单条消息字符上限（超出按行切分） |
| `max_reply_parts` | `5` | 被动回复最多分片（官方限制 5 次/消息） |
| `heartbeat_ack_timeout_seconds` | `90` | 心跳 ACK 等待上限，超时重连（保留会话尝试 Resume） |
| `reconnect_initial_seconds` | `1` | 网关重连初始退避（指数增长到 `reconnect_max_seconds`） |
| `event_queue_size` | `64` | 群事件有界队列容量；满时事件保持可重投（不进去重表）并主动 Resume 由网关重投，未处理事件不推进恢复水位 |
| `event_workers` | `4` | 事件并发处理数（1 = 严格按序） |
| `bot_state_probe_enabled` | `true` | 是否启动/周期探测 `GET /v2/groups/{group_openid}/bot_state` |
| `bot_state_interval_seconds` | `300` | 探测间隔；0 = 只启动时探测一次 |
| `proactive_cooldown_initial_seconds` | `30` | 主动消息被拒后的初始冷却（指数退避） |
| `proactive_cooldown_max_seconds` | `600` | 冷却上限 |

### `servers[]`（每台 QueQiao）

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | 唯一；`/list`、事件格式中显示；建议与 QueQiao `server_name` 一致 |
| `host` | 必填 | 主机名/IP，**不能**带 `http://`、路径或端口；IPv6 直接写地址 |
| `port` | 必填 | 1–65535 |
| `enabled` | `true` | 禁用后：不连接、不广播、不查询；`/list` 里完全不出现（按名字查回「未找到服务器」） |
| `path` | `/` | WebSocket 路径，需与 QueQiao 一致（不要猜 `/minecraft/ws`） |
| `tls` | `false` | QueQiao 前面有 TLS 时改为 `true`（`wss://`） |
| `access_token` | 空 | 对应 QueQiao `access_token`；可写 `${QUEQIAO_XXX_TOKEN}` |
| `self_name` | 空 | 握手 `x-self-name`；留空用 `name` |
| `players.mode` | `auto` | `auto`=先 rcon 后 status，`rcon`=只走 `send_rcon_command list`，`status`=只走 `get_status` 采样 |
| `players.rcon_command` | `list` | 由 QueQiao 执行的命令 |
| `players.timeout_seconds` | `8` | 单台查询超时（超时只影响该台） |
| `connect_timeout_seconds` | `10` | 建连超时 |
| `reconnect_initial_seconds` / `reconnect_max_seconds` | `1` / `60` | 指数退避（含抖动） |

**唯一性规则**：`name` 唯一；规范化后的 `host:port` 全局唯一。
* 相同 host 不同 port：合法；不同 host 相同 port：合法；
* 禁用项也参与校验（防止哪天开启后解封冲突）；
* IP 会标准化（`::1` 与 `0:0:0:0:0:0:0:1` 视为同一台），DNS 只做大小写/末尾点规范化，**不做 DNS 解析**。

### `relay` / `runtime` / `logging`

* `relay.qq_to_mc_enabled`：QQ→MC 总开关（`/list` 不受影响）
* `relay.mc_to_qq.{chat,death,join,leave}`：四类事件独立开关
* `relay.list_command`：命令前缀，默认 `/list`（`/listing` 不算）
* `relay.dedupe_ttl_seconds` / `dedupe_max_entries`：去重窗口与上限（有界）
* `relay.loop_guard_seconds`：QQ→MC→QQ 回环抑制窗口
* `relay.nickname_fallback`：官方事件缺 `author.username` 时的兜底昵称（**不能保证是真群名片**）
* `relay.image_placeholder` / `attachment_placeholder`：占位符
* `runtime.status_http.*`：只读状态接口（建议只监听 `127.0.0.1`）
* `logging.level` / `logging.file`：日志级别与可选文件

---

## 7. 功能示例

假设配置了三台：`主服`(启用)、`生存服`(启用)、`创造服`(禁用)。

### 7.1 QQ 群普通消息 → MC

群里发 `大家好`，昵称为「小明」，所有启用且在线的服务器玩家都会看到：

```
小明：大家好
```

（QueQiao 会在前缀里加它自己的 `message_prefix`，如 `[鹊桥]`。）

### 7.2 `/list`

| 输入 | 输出（严格格式） |
|---|---|
| `/list` | `主服 2人在线：`<br>`Alice`<br>`Bob`<br><br>`生存服 0人在线` |
| `/list 主服` | `主服 2人在线：`<br>`Alice`<br>`Bob` |
| `/list 生存服` | `生存服 0人在线` |
| `/list 不存在` | `未找到服务器：不存在` |
| `/list 创造服`（禁用） | `未找到服务器：创造服` |
| 某台断线 | `生存服 离线` |

* **`enabled: false` 的服务器对 `/list` 完全不可见**：不查询、不在列表里占位、按名字查也回「未找到服务器」；
* 多服务器之间用**空行**分隔；
* 顺序与 `config.yaml` 中 `servers` 顺序一致；
* **所有以 `/` 开头的聊天都不会被转发到 MC**（不论它是不是已实现的指令：`/list`、`/help`、`/listing` 都只留在 QQ 侧）；
* 已实现的指令只有 `/list`（`/listing` 不算），匹配前会先剥掉全量模式带的 `<@member_openid>` 前缀；
* 名单过长时按 `max_message_chars`（默认 1500 字/片）、最多 `max_reply_parts`（默认 5，官方上限）片发送；
  截断说明「（列表过长，已截断）」**也计入单片上限**，每片都不会超过 1500 字。

### 7.3 MC → QQ

| MC 事件 | QQ 群消息 |
|---|---|
| Alice 聊天 `大家好` | `[主服] [Alice] ： 大家好` |
| Alice 进入 | `[主服] [Alice] ： 进入了服务器` |
| Alice 离开 | `[主服] [Alice] ： 离开了服务器` |
| Alice 死亡（was slain by Zombie） | `[主服] [Alice] ： was slain by Zombie` |

同名玩家在不同服务器会带各自的服务器名，因此可以区分。

### 7.4 多服 / 禁用

照抄一个 `servers` 条目改 `name`、`host`、`port` 即可；不想删配置就 `enabled: false`。
禁用项不会产生任何连接（测试里验证了连接数 = 0）。

---

## 8. 玩家名单为什么默认走 QueQiao 的 `list`（重要）

**已核实的上游事实**（源码级，见 `docs/VERIFICATION.md`）：

* QueQiao 最新 release `v0.5.0` 内置 queqiao-core **0.6.8**；
* 其协议处理只实现了 `broadcast`/`send_msg`、`send_title`、`send_actionbar`、
  `send_private_msg`、`send_rcon_command`、`get_status`（`send_command` 未实现，未知 API 返回 404）；
* **不存在** `get_players` / `get_player_list` 这类返回完整在线名单的 API；
* `get_status` 返回的是 `server_list_ping`（**Server List Ping 采样**，通常最多 12 条、
  `hide-players` 时为空）+ CPU/内存信息，**不是完整名单**。

因此本桥接器：

1. **默认（`mode: auto`/`rcon`）**通过 QueQiao 官方文档中的
   `{"api":"send_rcon_command","data":{"command":"list"}}` 获取 `list` 输出（例如
   `There are 2 of a max of 20 players online: Alice, Bob`），解析出**完整、实时**名单。
   RCON 由 QueQiao 自己执行，桥接器不直连 RCON、不开端口。
2. **`mode: status`** 只作后备：只有 ping 样例条数 **等于** `online` 人数（可证明完整）时才接受；
   否则**报查询失败并显示离线**，绝不用采样冒充完整名单。
3. 断线立即清空名单（显示离线）；连接/重连成功后自动重新同步一次；`join`/`leave` 事件只做增量修正，
   不替代权威查询。

> 如果你的服务端无法开启 RCON，`/list` 会显示离线 —— 这是**有意的安全失败**，不是 bug。
> 想要原生 API 可以向上游提需求；本仓库不伪造该接口。

---

## 9. 安全边界

* 只使用 QQ 官方接口；不包含任何 QQ 账号密码登录、OneBot、NapCat 等非官方协议。
* 不执行 shell/RCON/任意控制台命令：唯一的命令执行是让 QueQiao 跑 `players.rcon_command`（默认 `list`），
  且该命令来自本地配置文件、不接受 QQ 消息传入。
* 群 OpenID 必须精确匹配：非目标群、私聊、频道事件一律忽略。
* 机器人自身消息（`author.bot=true`）过滤；同一条消息按 `(group_openid, message_id)` 去重（含 Resume 重投）。
  去重表只在事件**成功入队后**写入：队列满的溢出事件不进表，Resume 重投时仍能入队并投递一次。
* 回环抑制：QQ→MC 广播后短时间内同文、同服的 MC 聊天事件会被丢弃（有界窗口）。
* 密钥只从 YAML 或环境变量读取；日志/错误信息只记受控错误种类与业务码，不打印上游原始 `message`、
  关闭原因、密钥、token、消息正文；状态接口不返回群 OpenID。
* 网关关闭码按官方表分流：不可重试的错误（4013/4014/4914/4915 等）直接终止重连，不会无休止打接口。
* QueQiao 连接与查询全部隔离：单台坏包、慢查询、拒绝广播只影响该台。

---

## 10. Docker

```bash
# config.yaml 由宿主机挂载（不要把密钥打进镜像）
docker compose up -d --build
docker compose logs -f bridge
curl -fsS http://127.0.0.1:18080/healthz
```

说明：

* compose 使用 `network_mode: host`，因为 QueQiao 通常监听在宿主机的 `127.0.0.1`；
  容器内的 `127.0.0.1` 是容器自己，会连不上。若 QueQiao 也在同一 Docker 网络里，
  可去掉 `network_mode` 并把 `host` 改成服务名。
* 容器以非 root、只读根文件系统、`cap_drop: ALL`、`no-new-privileges` 运行，`/tmp` 为 tmpfs。
* **没有在本机执行 Docker 构建**（当前用户不在 docker 组且无交互 sudo），见 `docs/VERIFICATION.md`。

---

## 11. 测试与验收

```bash
# 完整测试（旧 OneBot MVP 回归 + 本版新增）
.venv/bin/python -B -m pytest -q

# 端到端协议 smoke：3 台启用 + 1 台禁用的真实本地 TCP QueQiao + 真实本地 WebSocket 网关 + Mock HTTP
.venv/bin/python scripts/smoke_official.py
```

`scripts/smoke_official.py` 会真实建立 WebSocket 连接、校验 `x-self-name` 握手头、
走 `send_rcon_command`/`broadcast`/`get_status` 的请求-响应（含 `echo` 关联），
并验证 `/list` 文本、禁用服务器零连接、QQ→MC 广播、MC→QQ 转发。

真实 QQ 机器人与真实 Minecraft 服务器**尚未联调**（缺平台权限与运行中的 MC 服务），
边界与逐项证据见 `docs/VERIFICATION.md`。

---

## 12. FAQ

**Q：启动直接退出，提示 `bot.client_secret 未配置`？**
A：配置里写了 `${QQ_BOT_CLIENT_SECRET}` 但环境变量为空。三种办法任选：
`export QQ_BOT_CLIENT_SECRET=...`；`set -a; . ./.env; set +a`；或启动时加 `--env-file .env`。
注意本程序**不会自动读取 `.env`**，只写一个 `.env` 文件是不够的。
`--check-config` 不做密钥检查，所以它通过不代表能启动。

**Q：`配置校验通过` 但启动报 `servers 中没有启用的服务器`？**
A：所有 `enabled` 都是 `false`。至少启用一台，或先保持模板里的启用项。

**Q：机器人收不到群里的普通消息，只能收到 @？**
A：`receive_mode: all` 需要群里开启机器人「接收所有消息」权限；没有该权限时改成 `mention`。

**Q：`/list` 一直显示「离线」，但 QueQiao 日志显示已连接？**
A：多半是 RCON 没开或密码不对（QueQiao 返回 400 "Rcon is not enabled"/命令失败）。
在 QueQiao `config.yml` 打开 `rcon.enable` 并填 `password`，或临时把 `players.mode` 设为 `status`
（只有 ping 采样恰好完整时才可用）。

**Q：`/list` 显示「未找到服务器：xxx」？**
A：名称必须与 `config.yaml` 的 `name` **完全一致**（区分大小写、不能多空格）。

**Q：服务器名是中文，QueQiao 一直 1008 断开？**
A：桥接器已自动 URL 编码 `x-self-name`；若仍失败，检查 QueQiao 的 `server_name` 与配置里的
`name`/`self_name` 是否完全一致（包括不可见空格）。

**Q：死亡消息不出现？**
A：QueQiao 官方文档明确 vanilla / Velocity 不支持死亡事件；部分平台 `death.text` 为空时，
桥接器会退化为显示翻译 key/参数，不会编造原因。

**Q：MC 事件没发到 QQ，日志写 `proactive_disabled` / `reason=http_...code_...`？**
A：前者是 `bot.proactive_enabled: false`；后者是平台拒绝（权限/配额/审核）。桥接器不会重试成灾，
按官方提示去开放平台申请权限即可。

**Q：QQ 群消息广播后，MC 里又来一遍同样的内容？**
A：说明该服务端把广播当成了玩家聊天事件回显。桥接器有 5 秒同文回环抑制（`relay.loop_guard_seconds`），
若你的服务端延迟更大，适当调大该值。

**Q：同一个群里能接两个机器人 / 两个群吗？**
A：本版本按「一个机器人 + 一个群」设计；多群需要自行扩展（当前刻意不支持，避免权限与配额混乱）。

**Q：旧版 OneBot / `/mc` 命令还能用吗？**
A：旧代码 `mcqq_bridge.main` 作为历史兼容模块保留（仅回归测试使用），**默认入口不再是它**；
`make run`、Docker、README 全部指向 `python -m mcqq_bridge`。旧版说明归档在
`docs/LEGACY_ONEBOT_README.md`。
