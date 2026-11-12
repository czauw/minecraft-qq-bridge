# 验证报告（QQ 官方机器人 × QueQiao 多服务器）

执行目录：`/home/czauw/minecraft-qq-bridge`。只记录**真实执行过**的结果：本机已用真实机器人凭据、真实 QQ
网关与四台真实 QueQiao 做过局部联调，已验/未验边界单列在 §6，别把「没做完整回路」读成「没连过平台」。
测试 2026-11-11 执行、2026-11-12 复跑确认（Python 3.14.9 / `.venv`）；平台资料查阅 2026-11-10，执行前须按
管理端复核。历史阶段见 `docs/archive/HISTORY.md`。

## 1. 结论速览

| 类别 | 状态 | 依据 |
|---|---|---|
| 单元测试 | ✅ | `pytest -q` → **344 passed**（2026-11-12，含排行榜新增 109 项） |
| 协议级集成（真实本地 TCP WebSocket） | ✅ | `tests/test_integration_protocol.py`，3 启用 + 1 禁用 |
| 端到端 smoke（模拟 QueQiao + 模拟 QQ 网关） | ✅ | `SMOKE PASS: 18/18`（2026-11-12，含排行榜全链路） |
| 新入口启动 / 状态接口 / SIGTERM / 日志脱敏 | ✅ | 真实子进程探测（§4） |
| 真实 QQ 平台（token / 网关 / Identify / `bot_state` / 群发送） | ⚠️ 已连通 | journal：`getAppAccessToken` 200、`GET /gateway` 200、`Identify`、`bot_state` 200、5 次 `POST /v2/groups/.../messages` 200 |
| 真实群消息双向回显、非零名单、游戏内可见性 | ❌ 未验证 | 真实边界见 §6 第 1、2 条 |
| Docker 构建 | ❌ 未执行 | 当前用户不在 docker 组 |
| 排行榜真实实例 `world/stats` | ❌ 未验证 | `/opt/mcsmanager` 为 `750 root:root`，开发会话无 root；本地夹具与 smoke 已覆盖格式与链路（§3.1） |
| 真实 Minecraft + QueQiao 全链路 | ⚠️ 仅局部 | 四实例插件启动/RCON/ACK 已实测（`docs/MCSMANAGER_DEPLOY.md`），无真实玩家与双向收发 |
| 上游「完整玩家名单 API」 | ⚠️ 不存在 | 源码级核实见 §2 |

## 2. 上游协议核实（只读源码/官方文档）

**QueQiao 没有完整名单 API**：最新 release `v0.5.0`（2026-04-06）只挂 1 个资产
`QueQiao-vanilla+minecraft-0.5.0.jar`，内置 queqiao-core **0.6.8**。core 0.6.8 的
`tool/handle/HandleProtocolMessage.java` 中 `switch (api)` 只有 `broadcast`/`send_msg`、`send_title`、
`send_actionbar`、`send_private_msg`、`send_command`（返回 “not supported now”）、`send_rcon_command`、
`get_status`，default → `Response.failed(404, UNKNOWN_API)`。`get_status` 走 `ServerStatusCollector`
→ `MinecraftPingClient`，即 Server List Ping 采样（socket 超时 3 秒，读 MC status JSON 的
`players.online/max/sample`），不是完整名单；在研 0.7.0-beta 的 `DefaultApis.java` 仍只有 `GetStatusApi`。

**采用的替代**：官方 `send_rcon_command`（`{"api":"send_rcon_command","data":{"command":"list"}}`，
响应 `{"code":200,"status":"SUCCESS","data":"There are 0 of a max of 20 players online: "}`）。
桥接器只发这条 JSON，由 QueQiao 执行 RCON，自己不直连、不开端口，命令来自本地配置。
`mode: status` 只在 ping `sample` 条数 == `players.online` 时接受，否则抛 `PlayerQueryError`。

| 事实 | 来源 |
|---|---|
| `x-self-name` 必填、`URLDecoder(UTF-8)` 解码后与 `server_name` 全等比较；`Authorization` 必须等于 `Bearer <token>`（仅当该实例配置了非空 `access_token` 时校验）；`x-client-origin: minecraft` 被拒 | core 0.6.8 `tool/websocket/WsServer.java` |
| 请求/响应含 `api`/`data`/`echo`，响应 `code`/`status`/`message`/`data` | 官方 `api/v2/` |
| 事件 `post_type`=`message`/`notice`，`sub_type`=`player_chat`/`player_join`/`player_quit`/`player_death` | 官方 `events/v2/*` |
| QueQiao `config.yml` 的 `rcon` 只有 `enable`/`port`/`password`，无 host（连本机） | <https://queqiao-docs.pages.dev/config> |
| vanilla / Velocity 不支持死亡事件 | 官方 `events/v2/notice/player-death-event.html` |

实现期修正的真实缺陷：中文 `server_name` 直接放进 HTTP 头会按 latin-1 变乱码，现已在客户端 `quote()`
并按 QueQiao 的解码逻辑断言。

**QQ 官方接口要点**（链接见 §8）：

| 事实 | 来源 |
|---|---|
| `POST /app/getAppAccessToken`（`appId`/`clientSecret`）；失败常为 HTTP 200 + 业务 `code`；业务接口鉴权头 `Authorization: QQBot <access_token>`，仅 HTTP 401 时作废缓存 token 并刷新重试一次 | `dev-prepare/access-token.html` |
| `GET /gateway` → `{url}`；`op=10` Hello、`op=2` Identify、`op=1` 心跳 / `op=11` ACK、`op=6` Resume、`op=7` 重连、`op=9` 会话失效 | `event-emit/websocket.html` |
| 关闭码：4008/4009 可 Resume；4006/4007/4900~4913 重新 Identify；4001/4002/4010~4014/4914/4915 终止 | 同上错误码表 |
| 群事件 `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`，intent `1<<25` | `autogen/event/*` |
| 发群消息 `POST /v2/groups/{openid}/messages`：`msg_type=0`、`msg_id`、`msg_seq`；被动 5 分钟/最多 5 次 | `..._messages.post.html` |
| `GET /v2/groups/{openid}/bot_state` → `allow_proactive_msg`/`recv_msg_setting`/`member_role`；`11253` = 仅白名单机器人 | `..._bot_state.get.html` |
| 主动消息：Bot 60 qpm（已认证）/30 qpm、单关系 20 qpm、每群每天 1000 条、接口 100 QPS | `server-inter/message/overview.html` |
| 不同 AppID 得到的 `openid` 互不相同；`openid` 不是 QQ 号/群号 | `dev-prepare/api-call-guide.html` |

## 3. 自动化测试

```console
$ .venv/bin/python -B -m pytest -q -p no:cacheprovider
344 passed in 5.85s          # 2026-11-12，含排行榜新增 109 项
$ .venv/bin/python -B scripts/smoke_official.py
SMOKE PASS: 18/18 项通过      # 原 10 项 + 排行榜 8 项
```

覆盖重点（回归名称见测试文件；源码注释引用的 M1–M9 是首轮审查编号，对应下列条目）：

* **关闭码分流**：18 个参数覆盖整张官方表；真实 WS 4006→两次 `op=2`、4009→`op=6` 带水位、4915→终止不重连。
* **队列与水位**：乱序完成不推进水位；队列满不推进并主动 Resume；溢出不进去重表、Resume 重投后恰好投递一次；
  已完成事件不重复投递；all 模式双投去重；处理阻塞时心跳继续。
* **查询与回环**：查询期间 join/leave 重放合并、UUID 优先、断线代次作废、并发查询串行；ACK 前回显即抑制、
  慢服不影响其它服、失败/异常撤销登记、并发同文引用计数。
* **接收模式与权限**：`mention` 只消费 @、`all` 双消费并去重；`bot_state` false 禁主动不禁回复、
  `11253`/网络失败降级 unknown、权限码与 429 触发有界冷却后恢复。
* **投递判定、脱敏与配置**：坏 JSON / 空 body / 数组 / 缺 id 不算成功（`data.id` 为合法正例）；上游 `message`、
  含凭据的异常文本不进日志与 `status`；`enabled:"yes"`、`port:true` 被拒且不回显值；
  分片截断说明也受 `max_message_chars` 约束。

集成测试的真实性边界：QueQiao 侧是**真实 TCP WebSocket 服务端**（校验 `x-self-name`/`Authorization`、
按 Protocol V2 应答 `api`/`echo`、推送事件），QQ 侧是**真实 TCP WebSocket 网关**（Hello/Identify/READY/心跳 ACK）
加 `httpx.MockTransport`。**没有**连接真实 QQ 或真实 Minecraft。

### 3.1 排行榜（`/death`、`/playtime`）

自动化覆盖（`tests/test_leaderboard_*.py`、`tests/test_stats_*.py`，`pytest --collect-only -q` 共 88 项）：

* **命令口径**：无参输出所有启用服 × 每服全部玩家（含 0 值）、玩家名参数回「未找到服务器」、
  禁用服整段不出现且按名查询回「未找到服务器」、`<@...>` 剥离、多余参数忽略、`/deathx` 不算命令；
* **数值口径**：`ticks // 20` 秒级、同服同名双 UUID 求和且会话只计一次、跨服不合并、
  在线加时与落盘后重基线不双计、`ticks` 下降按新值重置（换世界）、平局按名字 Unicode 升序；
* **可靠性**：mtime/size/inode 未变不读文件、写失败不推进扫描缓存、半写 JSON 保留上次值且只 WARNING 一次、
  usercache 改名生效、缺映射回退 `uuid[:8]?`、缺目录「暂无数据」与权限错误保留旧值区分、
  数据库损坏隔离重建、重启不清零且不把停机时间算成在线、在途写在关闭连接前跑完；
* **会话**：重复 join/leave 幂等、关闭进服转发仍统计、单台 RCON 失败只收敛该服（按「最后确证在线 + 60 秒」
  封顶把会话关闭并落盘，展示与库同一口径）、RCON 恢复后不补加崩溃期间的时间、
  对账不覆盖并发 join、崩溃会话下一轮对账即关闭、对账持续失败时加时封顶 60 秒；
* **路径约束**：`stats.db_path` 必须在 `stats.root` 内（`/etc/x`、`..` 穿越、指向外部的软链接均拒绝，
  默认路径通过），`StatsStore` 对越界路径不创建任何父目录；
* **渲染与接口**：整服优先分片、不切断玩家行、超 5 片标注截断、JSON 与文本同源、
  `schema_version=1`、404/503 语义、无 UUID/路径/OpenID 泄漏。

smoke 额外用**临时目录里的真实 `world/stats/*.json` + `usercache.json`**（不是 mock 数据对象）
走完 `stats → 统计服务 → 路由 → QQ HTTP`：3 片回复共用同一 `msg_id`、`msg_seq` 1..3、
分片不切行、禁用服 404、JSON 与文本一致。

**仍未验证**：真实实例目录（`/opt/mcsmanager` 为 `750 root:root`，本会话无 root 权限）下的
`world/stats` 键名/数值、真实在线玩家的加时与落盘一致性、`26.2` 实例无 `world/stats` 的降级表现。
这些需要在有 root 的部署机上按 README §11 第 6–10 项执行。

## 4. 真实进程验证（2026-11-11）

临时配置：`api_base=http://127.0.0.1:9`（必失败）、1 台启用 + 1 台禁用、状态端口 18099、
`client_secret` 与 `group_openid` 填 canary。真实子进程启动后 SIGTERM：

```text
exit code: 0
/healthz  200 {"status":"ok"}
/readyz   200 {"status":"degraded","qq_connected":false,"servers_enabled":1,
               "servers_connected":0,"qq_degraded":["proactive_state_unknown","recv_msg_setting_unknown"]}
/api/v1/status 200：qq.error="获取 access_token 失败：ConnectError"，
               servers[0].error="connect_failed:ConnectionRefusedError"，禁用项仍在列表内
canary in log: False    canary in status: False    group_openid in status: False
```

日志可见启动配置行、受控错误、退避重连、停止/已停止，没有密钥与消息正文。该探测确认：
**HTTP 200 不等于 ready**，`readyz` 用 `status` 字段表达降级。生产实例（systemd）的只读探测见
`docs/MCSMANAGER_DEPLOY.md` §4，是即时快照，不是固定状态。

## 5. 支持版本矩阵（真实数值 + 来源）

| 项目 | 数值 | 来源 | 状态 |
|---|---|---|---|
| GitHub release latest | `v0.5.0`（2026-04-06），资产仅 `QueQiao-vanilla+minecraft-0.5.0.jar` | `api.github.com/repos/17TheWord/QueQiao/releases/latest` | 已核对 |
| 内置 core | queqiao-core/tool **0.6.8** | 仓库 `tool_version.txt` | 已核对 |
| Modrinth | 983 个版本文件，最新 `0.5.3+fabric-26.3` | `api.modrinth.com/v2/project/queqiao/version` | 已核对 |
| loader 范围 | Spigot 1.12.2–latest；Paper 1.17.1–latest；Folia 1.21.4–latest；Velocity 3.3.0；Forge 1.7.10–1.21；Fabric 1.16.5–1.21.8；NeoForge 1.21.1 | 上游 README 徽章 | 上游声明，**未实测** |

## 6. 未验证与限制

1. **真实 QQ 平台：已连通，消息回路仍未验证**：真实 `access_token` 获取、真实网关连接与 `Identify`、真实
   `bot_state` 取值（`allow_proactive_msg=true`、`recv_msg_setting=all`）、向真实群发送（journal 中 5 次
   `POST /v2/groups/{openid}/messages` 均 HTTP 200）、真实群事件到达并转发到在线 QueQiao
   （`QQ 消息广播到 2/2 个在线服务器`）均已实测。**仍未验证**：群内普通消息端到端回显（含 `receive_mode: all`
   下 `<@...>` 前缀的真实到达形态）、`msg_seq` 5 次上限与配额、主动消息在群里的真实呈现。
2. **真实 MC：插件侧已实测，玩家侧未验证**：四台 QueQiao 启动（`WebSocket Server 在 0.0.0.0:809x`）、
   RCON `list` 往返（0 人）、`broadcast` ACK、错误/缺失 `Authorization` → 关闭码
   `1008 Authorization Header is wrong` 均已实测（`docs/MCSMANAGER_DEPLOY.md` §3）。**仍未验证**：非零在线
   名单的实际输出、游戏内是否真的看到广播/聊天、各服务端本地化格式、`player_death` 字段差异；
   `list` 解析覆盖常见中英文格式，不保证覆盖所有本地化/插件改写。
3. **loader/版本兼容**：只有上游声明范围，没有 loader 被真实联调；只按 v0.5.0/core 0.6.8 的协议形状实现。
4. **RCON 前提**：`/list` 需 MC `enable-rcon` 与 QueQiao `rcon.enable` 两侧都配且口令一致。默认
   `players.mode: auto` 下 RCON 失败**不等于**离线：桥接器继续用 `get_status` 采样兜底，仅当采样条数等于
   `online` 人数才接受；两种来源都失败才抛 `PlayerQueryError` → `/list` 显示离线。`players.mode: rcon`
   才是 RCON 一失败即离线（`queqiao.py::query_players`，`manager.py::_query_one`；
   `tests/test_queqiao.py::test_query_auto_falls_back_to_status_when_rcon_missing`）。
5. **全量接收依赖群权限**：`receive_mode: all` 需群主开「接收所有消息」；`bot_state.recv_msg_setting`
   只能暴露不符，不能改变权限。`bot_state` 仅白名单机器人可用，普通机器人一直 unknown、
   `/readyz` 一直 degraded 属事实暴露，不是故障。
6. **死亡事件**：vanilla / Velocity 不支持；`death.text` 为空时显示翻译 key/参数。
7. **Docker 未构建**；单机器人单群，多群/私聊/频道不支持；非目标群事件忽略。
8. **昵称与附件**：昵称缺 `author.username` 时用 fallback，不保证群名片；只有 `content`+`attachments` 时
   占位符统一追加在文本后，无法还原原始交错顺序。
9. **投递与顺序**：回环抑制是有界窗口（默认 5 秒）属缓解而非强一致；队列满主动 Resume 不丢消息，
   但进程重启与超时不保证不丢、也不保证恰好一次；`event_workers` 默认 4，同时到达的多条群消息顺序不保证。
10. **`status` 模式**只在 ping 采样完整时可用；冷却业务码集合（304003/40034100/40034105）未穷举。

## 7. 复现步骤

```bash
cd /home/czauw/minecraft-qq-bridge
.venv/bin/python -B -m pytest -q -p no:cacheprovider          # 235 passed
.venv/bin/python -B scripts/smoke_official.py                 # SMOKE PASS: 10/10
.venv/bin/python -B -m mcqq_bridge --config config.example.yaml --check-config   # exit 0 + 待补齐项
# 真实进程（自备临时配置：状态端口 18099、服务器指向关闭端口）
.venv/bin/python -B -m mcqq_bridge --config /tmp/probe-config.yaml &
curl -fsS http://127.0.0.1:18099/healthz && curl -fsS http://127.0.0.1:18099/readyz
curl -fsS http://127.0.0.1:18099/api/v1/status
kill -TERM %1
```

`config.example.yaml` 的 `--check-config` 实测（exit 0）：3 个服务器（2 启用），随后列出
`bot.app_id` / `bot.client_secret` / `bot.group_openid` 未配置——结构合法 ≠ 可启动。

历史数字说明：旧报告写 `228 passed`，smoke 文本里含 `资源服 未启用`。前者是当时计数（现为 235）；
后者对应 `/list` 曾展示禁用项的旧行为，**与当前实现冲突**（现在禁用项完全不出现），已按代码更新。

## 8. 来源

| 结论 | 来源 | 查阅 |
|---|---|---|
| 群事件 / 全量接收 / 身份 openid | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html> | 2026-11-10 |
| @ 事件正文去前缀 | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html> | 2026-11-10 |
| token 语义、Gateway 与关闭码 | <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html>、`.../event-emit/websocket.html` | 2026-11-10 |
| 发群消息、主动配额、bot_state | <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html>、`.../server-inter/message/overview.html`、`.../api/v2_groups_group_openid_bot_state.get.html` | 2026-11-10 |
| openid 作用域、申请与权限 | <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html>、<https://bot.q.qq.com/wiki/bot_FAQ/>（保留尾斜杠） | 2026-11-10 |
| QueQiao 仓库与发布 | <https://github.com/17TheWord/QueQiao>、`api.github.com/.../releases/latest`、`api.modrinth.com/v2/project/queqiao/version` | 2026-11-10 |
| QueQiao RCON / 配置 / 死亡限制 | <https://queqiao-docs.pages.dev/api/v2/rcon-command.html>、`/config`、`/events/v2/notice/player-death-event.html` | 2026-11-10 |

Webhook 未实现；若将来改用 Webhook，必须另验 Ed25519 验签、事件去重、公网 HTTPS 入口与平台规则，
本报告不提供 Webhook 的实施依据。