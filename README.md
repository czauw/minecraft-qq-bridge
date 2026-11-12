# Minecraft ↔ QQ 互通桥接器（QQ 官方机器人 × QueQiao 多服务器）

把**多台 Minecraft 服务器**（每台装一个 [QueQiao](https://queqiao-docs.pages.dev/) 插件/模组）
接到**一个 QQ 官方机器人**的**一个群**里：

* QQ 群消息 → 广播到所有在线的 MC 服务器；
* MC 的聊天 / 进入 / 离开 / 死亡 → 转发到 QQ 群；
* `/list` 查询所有**启用**服务器的在线玩家；
* 每台服务器一条独立 WebSocket，单台断线不影响其它服务器，自动退避重连并重新同步名单。

QQ 侧只用官方接口（OpenAPI + 官方 WebSocket 网关），不涉及 OneBot、NapCat 等非官方登录协议。

## 1. 能力与边界

| 能力 | 状态 | 说明 |
|---|---|---|
| QQ 群消息 → MC（全量或仅 @） | ✅ | 文本「昵称：内容」 |
| MC 聊天/进入/离开/死亡 → QQ | ✅ | 统一 `[服务器] [玩家] ： 内容` |
| `/list` 全部 / 单个 / 不存在 | ✅ | 格式固定，见 §7 |
| 多服独立连接、禁用项、唯一性校验 | ✅ | `name` 唯一 + `host:port` 全局唯一（禁用项也校验） |
| 断线重连 / 重连后重新同步名单 | ✅ | 指数退避；断线立即显示离线 |
| 图片/附件 | ✅ 占位符 | `[图片]` / `[附件]`，**不下载任何文件** |
| 玩家名单「完整查询」 | ⚠️ 依赖 RCON | QueQiao 已发布版本无完整名单 API，默认由 QueQiao 执行 `list`，见 §8 |
| 死亡事件 | ⚠️ 平台限制 | vanilla / Velocity 不支持 `PlayerDeathEvent` |
| 全量接收群消息 | ⚠️ 需群主授权 | 未开「接收所有消息」时只能收 @ 事件 |
| MC 事件主动推送 | ⚠️ 需授权与配额 | 受群主「主动推送消息」开关与平台频控约束 |
| 真实 QQ 平台 / 真实 MC 联调 | ⚠️ 部分已验 | 真实 token、网关 `Identify`、`bot_state`、群发送 HTTP 200，四实例 QueQiao 启动 / RCON `list` / `broadcast` ACK 均已实测；群消息双向回显、非零名单、游戏内可见性仍未验证，边界见 §11 与 `docs/VERIFICATION.md` §6 |

## 2. 环境要求

* Python **3.11+**（本机开发使用 3.14.9）；依赖按 `pyproject.toml` 固定版本安装
* 出网：能访问 `https://api.bot.qq.com`（或配置的 `bot.api_base`）与各 QueQiao 的 WebSocket 端口
* 已核对的上游版本：QueQiao v0.5.0（内置 queqiao-core 0.6.8，Protocol V2）；loader/MC 兼容范围见
  `docs/VERIFICATION.md` §5，属上游声明，本仓库未逐版本实测

```bash
cd /home/czauw/minecraft-qq-bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
```

## 3. 准备 QQ 官方机器人

以下由运营者在 QQ 开放平台完成，平台规则以管理端当场显示为准（本节数值取自 2026-11-10 官方文档）。

1. <https://q.qq.com/> 注册并完成主体认证，创建**机器人**应用。
2. 「开发 → 开发设置」取得 **AppID** 与 **ClientSecret**；Secret 只放服务端/`.env`，不进群、不进仓库。
3. 服务范围要含 **QQ 群**（本项目只用 `/v2/groups/...`，不用频道接口）；连接方式选 **WebSocket**
   （官方描述：不依赖公网服务器，适合单机长连接；Webhook 需公网 HTTPS，本项目未实现）。
4. 用最新版手机 QQ 把机器人拉进测试群（未认证主体只能加入自己是群主/管理员的群），
   再由**群主**在「设置权限」里按需开启：
   * 「接收所有消息」→ 才会收到 `GROUP_MESSAGE_CREATE`（`receive_mode: all` 需要）；
   * 「主动推送消息」→ 才允许非回复型消息（MC 事件推送需要）。
   关掉对应开关，桥接器立即失去该能力；配置改不了平台权限。
5. 平台限制写进预期（详细依据见 `docs/VERIFICATION.md` §2、§8）：
   被动回复 5 分钟内每条最多 5 次；主动消息 Bot 60/30 qpm、单关系 20 qpm、每群每天 1000 条；
   含 URL 或违规内容会整条发送失败。**MC 聊天全量镜像不可行**，只能低频推送，不能承诺「每条都到」。

### 3.1 获取 `group_openid`

`group_openid` 只能从该群的事件里读到，**不是 QQ 群号**。官方没有「列出机器人所在群」的接口
（`/users/@me/groups` 不存在），用仓库自带脚本读事件取得：

```bash
# 1) 先停桥接器：两处同时连网关会互相抢占会话
sudo systemctl stop mcqq-bridge.service
# 2) 运行脚本，然后在目标群里 @机器人 发一句话
.venv/bin/python scripts/discover_group_openid.py --app-id 你的AppID --env-file .env
# 3) 把打印的 group_openid 写进 config.yaml 的 bot.group_openid
# 4) 取完再启动桥接器
sudo systemctl start mcqq-bridge.service
```

脚本会打印 `group_openid`、群名、`member_openid` 与消息前 40 字，**不要整段粘到公开工单或仓库**。
也可在开放平台的 API 调试台查看该群事件的 `d.group_openid`。

`group_openid` 是「机器人 × 群」维度的标识：换机器人或换群都要重新取；不同 AppID 得到的 openid 互不相同。
本桥接器只支持**一个机器人 + 一个群**，不保存群号映射。

## 4. 准备 QueQiao

QueQiao 装在**每台 Minecraft 服务端**上，用 WebSocket 发出玩家事件、接收广播。
下载与文档：<https://queqiao-docs.pages.dev/>（源码：<https://github.com/17TheWord/QueQiao>）。
配置路径是 `config/QueQiao/config.yml`（**大小写敏感**），放在实例目录下。最小配置：

```yaml
server_name: "主服"          # 必须与桥接器 servers[].name（或 self_name）完全一致
access_token: ""            # 可选；填了就要在桥接器里填同样的值
websocket_server:
  enable: true
  host: "0.0.0.0"           # 桥接器能访问到的地址
  port: 8080                # 每台可相同也可不同，桥接器按 host+port 区分
subscribe_event:
  player_chat: true
  player_join: true
  player_quit: true
  player_death: true        # vanilla / Velocity 不支持，见 §8
rcon:
  enable: true              # /list 权威名单需要
  port: 25575
  password: "换成你自己的强口令"
```

* 握手头 `x-self-name` 由桥接器再做 URL 编码后发送（QueQiao 会 `URLDecoder` 解码），中文服务器名可用；
  名称不一致会被 QueQiao 以 `1008` 拒绝。
* QueQiao `config.yml` 的 `access_token` 非空时，握手头必须带 `Authorization: Bearer <access_token>`，
  且桥接器该服的 `access_token` 与之全等；缺失或写错会被 QueQiao 以 `1008 Authorization Header is wrong`
  直接关闭（该台隔离，不影响其它台）。QueQiao 侧留空则不校验该头。
* 同机多实例要给每台独立的 WS 端口与 RCON 端口。

### 4.1 `/list` 的 RCON 要两侧都配

`/list` 的权威名单由 QueQiao 用**它自己的** RCON 客户端执行 `list` 得到。桥接器不直连 RCON、不开端口，
只发一条 `{"api":"send_rcon_command","data":{"command":"list"}}`。

**① Minecraft 服务端 `server.properties`**

```properties
enable-rcon=true
rcon.port=25575              # 必须与 QueQiao 的 rcon.port 相同
rcon.password=换成你自己的强口令
broadcast-rcon-to-ops=false  # 可选
```

改完重启服务端，确认该端口只对 QueQiao 所在主机开放，不要暴露公网。
验证：`mcrcon -H 127.0.0.1 -P 25575 -p '口令' list` 返回 `There are N of a max of M players online...`。

**② QueQiao `config.yml` 的 `rcon` 段**（官方只有 `enable`/`port`/`password`，没有 host，
固定连本机 `127.0.0.1:port`；MC 不在同一台机器时用端口转发）

```yaml
rcon:
  enable: true
  port: 25575
  password: "换成你自己的强口令"
```

**③ 桥接器**：默认 `players.mode: auto`（先 RCON 后 `get_status` 兜底），无需填 RCON 地址或口令。

RCON 未开或两侧口令不一致时，QueQiao 执行 `list` 只会返回失败；默认 `players.mode: auto` 下这**不等于**
`/list` 离线：桥接器会继续用 `get_status`（Server List Ping 采样）兜底，采样条数等于 `online` 人数时照常
显示名单，**两种来源都失败**才报查询失败 → 显示「离线」。`players.mode: rcon` 才是 RCON 一失败就离线。
失败时如实显示离线/空名单，不会假装成功（与 §8 一致）。

## 5. 安装与启动

```bash
cp config.example.yaml config.yaml   # 至少填 bot.app_id、client_secret、group_openid、servers[]
cp .env.example .env                 # 填真实 ClientSecret（config 里用 ${QQ_BOT_CLIENT_SECRET} 引用）
python -m mcqq_bridge --config config.yaml --check-config --env-file .env   # 只校验结构，不联网
python -m mcqq_bridge --config config.yaml --env-file .env                 # 前台启动
```

`--check-config` 退出码：`0` 结构通过（可能仍提示运行前需补齐的密钥），`2` 配置错误（只打印字段路径与原因）。
它通过不代表能启动：密钥缺失、零启用服务器只在真正运行时被拒绝。

**`.env` 不会自动加载**：程序只读 `os.environ`。必须加 `--env-file .env`，或先 `set -a; . ./.env; set +a`。
`--env-file` 只支持 `KEY=VALUE`、可选 `export` 前缀与成对引号，已有环境变量不覆盖；文件缺失或某行非法时退出码 2。

### 5.1 本机 systemd 运行方式

本机以 `/etc/systemd/system/mcqq-bridge.service` 运行（不是 Docker）：**`User=root`**（需要读 MCSManager
实例目录下的 `world/stats`，`/opt/mcsmanager` 是 `750 root:root`，普通用户无法穿透），
`WorkingDirectory=/home/czauw/minecraft-qq-bridge`，
`ExecStart=.../.venv/bin/python -m mcqq_bridge --config .../config.yaml --env-file .../.env`（绝对路径），
`Restart=on-failure`、`RestartSec=10`、`StartLimitBurst=3 / 120s`（密钥缺失时进程退出 2，限次避免刷日志）。

以 root 运行时必须配的加固（缺一条就会出事或过度放权）：

```ini
ProtectSystem=strict                     # 根文件系统只读
ProtectHome=read-only
ReadWritePaths=/home/czauw/.local/share/mcqq-bridge   # 榜单 SQLite 落点（目录必须先存在）
ReadOnlyPaths=/opt/mcsmanager/daemon/data/InstanceData # 只读挂载实例数据，杜绝误写
CapabilityBoundingSet=CAP_DAC_READ_SEARCH             # 必须保留：venv 解释器在 700 的 .hermes 下
NoNewPrivileges=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
```

> 把 `CapabilityBoundingSet` 清空会让 root 连自己的 stdlib 都读不到（实测
> `Fatal Python error: Failed to import encodings module`，因为 `/home/czauw/.hermes` 是 `700`）。
> 只保留 `CAP_DAC_READ_SEARCH` 即可：读文件够用，写只在 `ReadWritePaths` 内。

```bash
sudo systemctl status mcqq-bridge.service
sudo systemctl restart mcqq-bridge.service
journalctl -u mcqq-bridge.service -f        # 密钥与消息正文不进日志
```

新主机照同样参数建单元（`ExecStart` 用绝对路径，`--env-file` 不能省）：

```ini
[Service]
Type=simple
User=<运行用户>
WorkingDirectory=<项目绝对路径>
ExecStart=<项目绝对路径>/.venv/bin/python -m mcqq_bridge --config <绝对路径>/config.yaml --env-file <绝对路径>/.env
KillSignal=SIGTERM
Restart=on-failure
RestartSec=10
```

`[Unit]` 建议加 `After=network-online.target docker.service`，`[Install]` 用 `WantedBy=multi-user.target`。

别用 `sudo cat .env` 检查密钥；只看 `systemctl status` 与 `/readyz`。
启动后日志打印每台服务器名称/host:port/启用状态/查询方式，不打印密钥或消息正文；
`SIGTERM`/`Ctrl+C` 会取消任务、关闭 WebSocket 与状态 HTTP 再退出。

## 6. 配置字段

模板见 `config.example.yaml`（逐字段中文注释）。字段全集以 `src/mcqq_bridge/config.py` 为准；
未知字段、类型不符（`enabled: "yes"`、`port: true`）直接拒绝。

### `bot`

| 字段 | 默认 | 说明 |
|---|---|---|
| `app_id` | 空 | AppID；运行前必填 |
| `client_secret` | 空 | ClientSecret；建议 `${QQ_BOT_CLIENT_SECRET}` |
| `group_openid` | 空 | 目标群 OpenID（不是群号） |
| `api_base` | `https://api.bot.qq.com` | OpenAPI 根地址（旧应用可用 `https://api.sgroup.qq.com`） |
| `gateway_url` | 空 | 留空则 `GET /gateway` 动态获取 |
| `intents` | `33554432` | `1<<25` 群与 C2C 事件 |
| `receive_mode` | `all` | `all`=群全量+@（需群权限，按 `message_id` 去重）、`mention`=只消费 @ 事件 |
| `proactive_enabled` | `true` | 关闭后不主动推送 MC 事件，只回复 |
| `send_rate_per_minute` | `20` | 本进程发送限速（平台另有频控） |
| `request_timeout_seconds` | `10` | 单次 HTTP 超时 |
| `token_refresh_skew_seconds` | `60` | token 提前刷新窗口 |
| `reconnect_initial_seconds` / `reconnect_max_seconds` | `1` / `60` | 网关重连指数退避 |
| `max_message_chars` | `1500` | 单条消息字符上限（超出按行切分） |
| `max_reply_parts` | `5` | 被动回复最多分片（官方上限 5 次/消息） |
| `heartbeat_ack_timeout_seconds` | `90` | 心跳 ACK 等待上限，超时重连（保留会话尝试 Resume） |
| `event_queue_size` | `64` | 事件有界队列；满时事件保持可重投并主动 Resume，未处理事件不推进恢复水位 |
| `event_workers` | `4` | 事件并发数（1 = 严格按序） |
| `bot_state_probe_enabled` / `bot_state_interval_seconds` | `true` / `300` | 启动与周期探测 `GET /v2/groups/{openid}/bot_state`；0 = 只探测一次 |
| `proactive_cooldown_initial_seconds` / `_max_seconds` | `30` / `600` | 主动消息被拒后的有界指数冷却 |

### `servers[]`

| 字段 | 默认 | 说明 |
|---|---|---|
| `name` | 必填 | 唯一；`/list` 与事件格式中显示；建议与 QueQiao `server_name` 一致 |
| `host` | 必填 | 主机名/IP，不能带协议、路径或端口；IPv6 直接写地址 |
| `port` | 必填 | 1–65535 |
| `enabled` | `true` | 禁用后：不连接、不广播、不查询，`/list` 完全不出现（按名字查回「未找到服务器」） |
| `path` | `/` | WebSocket 路径，须与 QueQiao 一致 |
| `tls` | `false` | QueQiao 前面有 TLS 时改 `true`（`wss://`） |
| `access_token` | 空 | 对应 QueQiao `access_token`；可写 `${QUEQIAO_XXX_TOKEN}` |
| `self_name` | 空 | 握手 `x-self-name`；留空用 `name` |
| `players.mode` | `auto` | `auto`=先 rcon 后 status，`rcon`=只用 `send_rcon_command`，`status`=只用 ping 采样 |
| `players.rcon_command` | `list` | 由 QueQiao 执行的命令 |
| `players.timeout_seconds` | `8` | 单台查询超时（只影响该台） |
| `connect_timeout_seconds` | `10` | 建连超时 |
| `reconnect_initial_seconds` / `reconnect_max_seconds` | `1` / `60` | 指数退避（含抖动） |

唯一性：`name` 唯一；规范化后的 `host:port` 全局唯一（相同 host 不同 port、不同 host 相同 port 都合法），
禁用项也参与校验。IP 会标准化（`::1` 与 `0:0:0:0:0:0:0:1` 视为同一台），DNS 只做大小写/末尾点规范化，不做解析。

### `relay` / `runtime` / `logging`

* `relay.qq_to_mc_enabled`：QQ→MC 总开关（`/list` 不受影响）；`relay.mc_to_qq.{chat,death,join,leave}`：四类事件独立开关
* `relay.list_command`：列表指令名，默认 `/list`（大小写不敏感，`/listing` 不算），可改成其它 `/xxx`
* `relay.dedupe_ttl_seconds` / `dedupe_max_entries`：去重窗口与上限（有界）；`loop_guard_seconds`：回环抑制窗口
* `relay.nickname_fallback`：缺 `author.username` 时的兜底昵称（**不保证是群名片**）
* `relay.image_placeholder` / `attachment_placeholder`：占位符；`max_nickname_chars` / `max_mc_message_chars`：显示长度上限
* `runtime.status_http.{host,port}`：只读状态接口，默认 `127.0.0.1:18080`；`runtime.shutdown_timeout_seconds`：优雅关闭上限
* `logging.level` / `logging.file`：级别与可选日志文件

## 7. 功能示例

假设配置三台：`主服`（启用）、`生存服`（启用）、`创造服`（禁用）。

### 7.1 QQ → MC

群里发 `大家好`，昵称「小明」→ 所有在线服务器玩家看到 `小明：大家好`
（QueQiao 会加它自己的 `message_prefix`，如 `[鹊桥]`）。

全量模式下 QQ 把 @ 写成 `<@member_openid>` 放在正文开头，桥接器**先剥掉开头连续的 `<@...>` 标签**再判断：

| 群里实际发送 | 行为 |
|---|---|
| `<@bot> /list` | 执行 `/list` |
| `<@A> <@B> /list 主服` | 执行 `/list 主服` |
| `<@A> 大家好` | 转发 MC，文本为 `小明：大家好`（不含 `<@A>`） |

**所有以 `/` 开头的聊天都不转发到 MC**，不管它是不是已实现的指令。已实现的只有列表指令（默认 `/list`）；
`/help`、`/listing` 等未知斜杠内容会被丢弃，**不保证有 QQ 回复**。

### 7.2 `/list`

| 输入 | 输出（严格格式） |
|---|---|
| `/list` | `主服 2人在线：`<br>`Alice`<br>`Bob`<br><br>`生存服 0人在线` |
| `/list 主服` | `主服 2人在线：`<br>`Alice`<br>`Bob` |
| `/list 不存在` | `未找到服务器：不存在` |
| `/list 创造服`（禁用） | `未找到服务器：创造服` |
| 某台断线 | `生存服 离线` |
| 全部禁用 | `没有启用的服务器`* |

\* 路由层空结果文案，实际运行中不会出现：零启用服务器在启动时即被拒绝（退出码 2）。

* `enabled: false` 的服务器对 `/list` 完全不可见：不查询、不占位、按名字查也回「未找到服务器」；
* 多服务器之间用空行分隔，顺序与 `servers` 顺序一致；
* 名单过长按 `max_message_chars`（默认 1500 字/片）、最多 `max_reply_parts`（默认 5）片发送，
  截断说明「（列表过长，已截断）」也计入单片上限。

### 7.3 MC → QQ

| MC 事件 | QQ 群消息 |
|---|---|
| Alice 聊天 `大家好` | `[主服] [Alice] ： 大家好` |
| Alice 进入 | `[主服] [Alice] ： 进入了服务器` |
| Alice 离开 | `[主服] [Alice] ： 离开了服务器` |
| Alice 死亡（was slain by Zombie） | `[主服] [Alice] ： was slain by Zombie` |

同名玩家在不同服务器带各自服务器名，因此可以区分。

### 7.4 多服 / 禁用

照抄一个 `servers` 条目改 `name`、`host`、`port` 即可；不想删配置就写 `enabled: false`，它不产生任何连接。

## 8. 玩家名单从哪里来

QueQiao v0.5.0 / core 0.6.8 只实现 `broadcast`、`send_title`、`send_actionbar`、`send_private_msg`、
`send_rcon_command`、`get_status`（`send_command` 不支持，未知 API 返回 404），**不存在**返回完整在线名单的
`get_players` 之类接口；`get_status` 只是 Server List Ping **采样**（通常最多 12 条，`hide-players` 时为空）。
源码级证据见 `docs/VERIFICATION.md` §2。因此：

1. **默认 `mode: auto`**：先发 `send_rcon_command` 执行 `list`（例：`There are 2 of a max of 20 players online: Alice, Bob`），
   解析出完整实时名单。RCON 由 QueQiao 执行，命令来自本地配置，不接受 QQ 消息传入。
2. **RCON 失败时 `auto` 仍会尝试 `get_status`**，但只有采样条数**等于** `online` 人数（可证明完整）才接受，
   否则报查询失败并显示离线——所以 RCON 不可用不等于一定离线，但也不保证可用。`mode: status` 只用该采样。
3. 断线立即清空名单；重连后自动重新同步；`join`/`leave` 只做增量修正，不替代权威查询。
   查询期间到达的 join/leave 按 UUID（无 UUID 按名字）重放合并，断线/重连后的旧结果直接作废。

## 9. 安全边界

* 只用 QQ 官方接口；不含 QQ 账号密码登录、OneBot、NapCat 等非官方协议。
* 群文本不能发起任意控制台/shell：唯一被执行的命令是 `players.rcon_command`（默认 `list`），
  来自本地配置，不接受 QQ 消息传入。
* 群 OpenID 精确匹配：非目标群、私聊、频道事件一律忽略；机器人自身消息（`author.bot=true`）过滤；
  同一条消息按 `(group_openid, message_id)` 去重（含 Resume 重投），去重表只在事件**成功入队后**写入，
  队列满的溢出事件保持可重投，不会因去重而丢失。
* 回环抑制：广播前按目标登记指纹，短时间内同文、同服的 MC 聊天丢弃（有界窗口，默认 5 秒，属缓解而非强一致）。
* 密钥只从 YAML 或环境变量读取；日志与错误只记受控错误种类与业务码，不打印上游原始 `message`、
  关闭原因、密钥、token、消息正文；状态接口不返回群 OpenID 与消息内容。
* 网关关闭码按官方表分流（不可重试直接终止）；单台 QueQiao 的坏包、慢查询、拒绝广播只影响该台。
  完整信任边界与部署层风险：`docs/THREAT_MODEL.md`。

## 10. 运行状态与维护

只读状态接口默认监听 `127.0.0.1:18080`：

| 端点 | 语义 |
|---|---|
| `/healthz` | 进程活着就返回 `{"status":"ok"}`，不反映依赖状态 |
| `/readyz` | `ready` 需同时满足：QQ 网关已连、≥1 台**启用**服务器已连、无 QQ 降级原因；否则 `degraded`，附 `servers_enabled` / `servers_connected` / `qq_degraded` |
| `/api/v1/status` | QQ 连接/队列/`bot_state` 与每台服务器的连接/人数/最近错误；**包含禁用项**，与 `/list` 的隐藏语义不同 |

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/api/v1/status
```

* HTTP 200 不等于健康：`/readyz` 也可能 200 且 `status=degraded`，要看字段。
* `proactive_state_unknown` / `recv_msg_setting_unknown` 属事实暴露：`bot_state` 仅白名单机器人可用（11253），
  普通机器人一直 unknown、`/readyz` 一直 degraded，不是进程故障。
* 状态端口只监听本机，不要映射公网；远程查看走 SSH 隧道或反代加鉴权。
* 密钥轮换：改 `.env`/`config.yaml` 引用后 `sudo systemctl restart mcqq-bridge.service`；
  QueQiao 侧 token 与桥接器必须同步改，否则该台一直 `1008`。
* Docker 工件（`Dockerfile`、`docker-compose.yml`）保留，**不是本机运行方式**，也未执行过构建验证。

## 11. 测试与验收

```bash
.venv/bin/python -B -m pytest -q -p no:cacheprovider                       # 全量测试（含旧 OneBot 回归）
.venv/bin/python -B scripts/smoke_official.py                              # 端到端协议 smoke
.venv/bin/python -B -m mcqq_bridge --config config.example.yaml --check-config
```

smoke 会真实建立 WebSocket、校验 `x-self-name` 握手头、走 `send_rcon_command`/`broadcast`/`get_status`
的请求-响应（含 `echo` 关联），并验证 `/list` 文本、禁用服务器零连接、QQ→MC 广播、MC→QQ 转发。
**它不连接真实 QQ 与真实 Minecraft**；历史执行计数与逐项证据见 `docs/VERIFICATION.md`。
本机部署另做过真实联调：真实 token、网关 `Identify`、`bot_state`、群发送 HTTP 200，四台真实 QueQiao 的
RCON `list` 与 `broadcast` ACK 均有记录；但下面 5 项里的双向回显、非零名单与游戏内可见性仍未验证，
真实边界见 `docs/VERIFICATION.md` §6。

真实环境验收（需真实机器人、群权限与运行中的 MC），逐项记录成功/失败：

1. 群里发普通话 → 每台在线服务器游戏内看到 `昵称：内容`；
2. 游戏内聊天 → 群里出现 `[服务器] [玩家] ： 内容`；
3. `/list` → 至少一台返回真实玩家名，人数与名单条数一致；
4. 停掉一台 QueQiao → 该台显示离线，其它台照常收发；恢复后自动重连并重新同步；
5. 群主关掉「主动推送消息」→ MC 事件发送失败被记录，不报成功。

## 12. FAQ

**Q：启动直接退出，提示 `bot.client_secret 未配置`？**
A：`.env` 不会自动加载。加 `--env-file .env`，或 `export QQ_BOT_CLIENT_SECRET=...`，或 `set -a; . ./.env; set +a`。
`--check-config` 不查密钥，通过不代表能启动。

**Q：`--check-config` 通过，启动却报 `servers 中没有启用的服务器`？**
A：所有 `enabled` 都是 `false`，至少启用一台。

**Q：只能收到 @，收不到普通群消息？**
A：`receive_mode: all` 需要群主开「接收所有消息」；没权限就改 `mention`。

**Q：`/list` 显示「离线」，但 QueQiao 日志显示已连接？**
A：多半是玩家名单两个来源都失败：RCON 没开 `enable-rcon`、`rcon.port` 不一致或口令不同，按 §4.1 两侧核对。
注意 `mode: auto` 下 RCON 失败本身不会直接导致离线（会退回 ping 采样），采样也不完整才会显示离线；
临时可设 `players.mode: status`（仅 ping 采样恰好完整时可用）。

**Q：MC 事件没发到 QQ，日志是 `proactive_disabled` 或 `reason=http_...code_...`？**
A：前者是 `proactive_enabled: false`；后者是平台拒绝（权限/配额/审核）或处于冷却窗口，按开放平台提示申请权限。

**Q：广播后 MC 里又冒出一条同样内容并转发回群？**
A：服务端把广播回显成玩家聊天。桥接器有 5 秒回环抑制（`relay.loop_guard_seconds`），延迟更大时调大该值。

## 附录：旧 OneBot 模块

* 旧实现（OneBot v11 + HTTP/反向 WS）保留为 `src/mcqq_bridge/main.py`、`onebot.py`、`service.py` 等，
  **仅供回归测试**；默认入口与 systemd/Makefile/Docker 都指向 `python -m mcqq_bridge`。
* 旧配置键（`.env` 里的 OneBot 键、`QQ_OFFICIAL_*` 草案键）当前**不生效**，唯一配置面是 `config.yaml` + §6。
* 数字 QQ 号/群号不能当 `group_openid`/`member_openid` 用，两种身份不通用。
* 没有自动 provider 回滚：切回 OneBot 必须改代码入口，且仍有第三方登录风险。
* 旧文档原文已删除，可从基线 commit `ae93e5a` 取回：`git show ae93e5a:docs/LEGACY_ONEBOT_README.md`。
* 三个阶段的历史与当时的测试结论：`docs/archive/HISTORY.md`。