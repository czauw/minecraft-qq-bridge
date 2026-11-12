# 验证报告（首轮审查返工后：QQ 官方机器人 × QueQiao 多服务器）

执行目录：`/home/czauw/minecraft-qq-bridge`
执行时间：2026-11-11（UTC）
环境：Linux 7.0.0-42-generic，Python **3.14.9**（`.venv`），pytest 8.4.2，
websockets 13.1，PyYAML 6.0.3，httpx 0.28.1，pydantic 2.14.3，fastapi 0.162.2。

> 本文只记录**真实执行过的**结果。没有真实 QQ 机器人凭据、没有运行中的 Minecraft/QueQiao，
> 因此真实平台联调一律标为「未验证」，不写通过。
>
> **与首轮报告的差异（更正夸大）**：
> 首轮 §3.3 写「三服务器独立连接（集成测试断言 3 条连接）」，但当时配置里 3 台中 1 台禁用，
> 实际只有 **2 条连接**（断言 `connections` 为 1/1/0）。本轮已补齐为 **3 台启用 + 1 台禁用**，
> 现在的断言是 `1/1/1/0` 且 `manager.connected_count == 3`。
>
> **与上一版报告的差异（第二轮返工）**：上一版把 `_remember_id()` 放在 `put_nowait()` **之前**，
> 因此队列满时事件已进去重表；网关 Resume 重投同一事件会被当重复丢弃，且水位被推进到该 seq，
> 消息永久丢失——与 M2「溢出由网关重投、不丢消息」的说法矛盾。现在**只有成功入队的事件才记已见**，
> 溢出事件保持可重投；去重命中时若该 seq 的副本仍在队列中，也不提前完成水位。
> 新增 4 项回归（真实 WS 溢出→Resume→重投）后全量为 `228 passed`。

---

## 1. 结论速览

| 类别 | 状态 | 依据 |
|---|---|---|
| 代码 / 单元测试 | ✅ 通过 | `228 passed`（§6） |
| 协议级集成测试（真实本地 TCP WebSocket） | ✅ 通过 | `tests/test_integration_protocol.py` 6 项（§6.2） |
| 端到端 smoke（3 启用 + 1 禁用的模拟 QueQiao + 模拟 QQ 网关） | ✅ 通过 | `SMOKE PASS: 10/10`（§7） |
| 新入口真实启动 / 状态接口 / SIGTERM 优雅退出 / 日志脱敏 | ✅ 通过 | 真实子进程探测（§8） |
| 首轮 9 项 Major | ✅ 全部修复并有回归 | §3 逐项映射 |
| 真实 QQ 官方平台（token、全量接收、主动消息配额） | ❌ 未验证 | 无 AppID/Secret/群权限 |
| 真实 Minecraft + 真实 QueQiao 插件 | ❌ 未验证 | 本机未安装/未运行 MC 与 QueQiao |
| Docker 构建 / compose 启动 | ❌ 未执行 | 当前用户不在 docker 组，无法无交互 sudo |
| 上游权威“完整玩家名单 API” | ⚠️ 不存在 | 源码级核实见 §2 |

---

## 2. 上游协议核实（只读源码/官方文档）

### 2.1 QueQiao 已发布版本没有完整玩家名单 API

| 证据 | 内容 |
|---|---|
| `https://api.github.com/repos/17TheWord/QueQiao/releases/latest` | 最新 release `v0.5.0`（2026-04-06），**只挂 1 个资产** `QueQiao-vanilla+minecraft-0.5.0.jar` |
| `QueQiao` v0.5.0 源码 `tool_version.txt` | `0.6.8` → 该 release 内置 queqiao-core **0.6.8** |
| `queqiao-core` v0.6.8 `.../tool/handle/HandleProtocolMessage.java` | `switch (api)` 仅：`broadcast`/`send_msg`、`send_title`、`send_actionbar`、`send_private_msg`、`send_command`（返回 “not supported now”）、`send_rcon_command`、`get_status`；default → `Response.failed(404, UNKNOWN_API + api)` |
| 同上 `case "get_status"` | `Response.success(ServerStatusCollector.collectStatusSnapshot())` |
| `tool/utils/ServerStatusCollector.java` | 返回 `timestamp / server_type / server_version / server_list_ping / cpu_information / memory_information`；`server_list_ping` 由 `MinecraftPingClient` 执行 **Server List Ping**（socket 超时 3 秒）得到 |
| `tool/utils/MinecraftPingClient.java` | 直接读 MC status JSON（`players.online/max/sample`），即 ping 采样，不是服务端完整名单 |
| `queqiao-core` main（0.7.0-beta）`api/DefaultApis.java` | 仍只有 `GetStatusApi`，实现同样读取 `ServerStatusCollector`（ping 快照） |

**结论**：没有任何已发布/在研 API 返回“完整在线玩家名单”。按约束**没有伪造** `get_players`，
也没有把 ping 采样或 join/leave 缓存当作完整名单。

### 2.2 采用的权威替代：QueQiao 官方 `send_rcon_command`

* 官方文档 <https://queqiao-docs.pages.dev/api/v2/rcon-command.html>：
  请求 `{"api":"send_rcon_command","data":{"command":"list"}}`，
  响应示例 `{"code":200,"status":"SUCCESS","data":"There are 0 of a max of 20 players online: "}`。
* 实现：桥接器只发送该 JSON，由 QueQiao 自己执行 RCON；桥接器**不直连 RCON、不监听端口**，
  命令来自本地配置（默认固定 `list`），**不存在任何由 QQ 消息传入的任意命令入口**。
* `mode: status` 仅在 ping `sample` 条数 == `players.online`（可证明完整）时接受，否则报失败。
  见 `src/mcqq_bridge/queqiao.py::_query_via_status`。

### 2.3 握手与事件字段

| 事实 | 来源 |
|---|---|
| `x-self-name` 必填、由 QueQiao `URLDecoder(UTF-8)` 解码后与 `server_name` 全等比较 | `queqiao-core` v0.6.8 `tool/websocket/WsServer.java:92-130` |
| `Authorization` 必须等于 `Bearer <access_token>` | 同上 `:132-137` |
| `x-client-origin: minecraft` 会被拒绝 | 同上 `:100-106` |
| 请求/响应含 `api`/`data`/`echo`，响应 `code`/`status`/`message`/`data` | 官方文档 `api/v2/`、`broadcast.html`、`rcon-command.html` |
| 事件 `post_type` = `message`/`notice`，`sub_type` = `player_chat`/`player_join`/`player_quit`/`player_death`；`player.nickname/uuid`；死亡 `death.text`/`death.key`/`death.args` | 官方文档 `events/v2/*` |
| QueQiao 官方 `config.yml` 的 `rcon` 段只有 `enable`/`port`/`password`，**没有 host**（连接本机 `127.0.0.1:port`） | <https://queqiao-docs.pages.dev/config> |
| vanilla / Velocity 不支持死亡事件 | 官方文档 `events/v2/notice/player-death-event.html` |

**实现期间因此修正的真实协议缺陷**：中文 `server_name` 直接放进 HTTP 头会按 latin-1 变成乱码，
现已在客户端做 `quote()` 且集成测试按 QueQiao 的解码逻辑断言（§6.2）。

### 2.4 QQ 官方接口（官方文档）

| 事实 | 来源 |
|---|---|
| `POST {api_base}/app/getAppAccessToken`，body `{appId, clientSecret}`，返回 `access_token`/`expires_in`；失败 HTTP 仍 200，看 `code` | `dev-prepare/access-token.html` |
| 鉴权头 `Authorization: QQBot *** | 同上 |
| 网关 `GET /gateway` → `{url}`；连接后 `op=10 Hello`，`op=2 Identify{token,intents,shard,properties}`；`op=1` 心跳（`d` = 已收到的最新 `s`，首次 null）、`op=11` ACK；`op=6 Resume`、`op=7` 重连、`op=9` 会话失效 | `dev-prepare/event-emit/websocket.html` |
| **关闭码表**：4001/4002 不可 Resume 不可 Identify；4006/4007 不可 Resume、可 Identify；4008/4009 两者都可；4010~4014 都不可；4900~4913 不可 Resume、可 Identify；4914/4915 都不可 | 同上「WebSocket 错误码」表 |
| 群事件 `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`，intent `1<<25`；`author.username`、`d.id`、`msg_elements`、`attachments` | `autogen/event/group_at_message_create.html`、`group_message_create.html` |
| 发群消息 `POST /v2/groups/{group_openid}/messages`，`msg_type=0`、`content`、`msg_id`、`msg_seq`；被动回复 5 分钟内每条消息最多 5 次；主动消息有频控 | `autogen/api/v2_groups_group_openid_messages.post.html` |
| 群内状态 `GET /v2/groups/{group_openid}/bot_state` → `member_openid`/`joined_at`/`allow_proactive_msg`/`recv_msg_setting`(`all`/`only_mention`/`mention_and_context`)/`member_role`；错误码 `11253` = 仅白名单机器人可用 | `autogen/api/v2_groups_group_openid_bot_state.get.html` |

---

## 3. 首轮审查 9 项 Major：逐项修复与证据

> 原缺陷的失败证据来自 reviewer 的独立探针（t_f1e4ca47 评论 #13 逐项给出实测值/行号）；
> 本轮把这些失败路径固化为自动化回归测试，再改实现并确认转绿。表中「回归测试」均在全量 `228 passed` 内真实执行。

### M1｜官方 Gateway 关闭码未按表分流（qq_official.py）

* **原缺陷**：任何关闭都走同一条重连路径；4006/4007 会带旧 session 去 Resume（真实 WS 实测第二轮收到 `op=6`，
  官方要求 `op=2`），4013/4014/4915 等不可重试错误会无限重连。
* **修复**：新增 `classify_close_code()`，严格按官方表映射：
  * `4008/4009` → Resume（保留 session）；
  * `4006/4007/4900~4913` → 清空 session/seq 后重新 Identify；
  * `4001/4002/4010~4014/4914/4915` → **终止重连**（`fatal=True`，错误只记 `gateway_close_<code>`）；
  * 传输层断链（无关闭帧 / 1000/1001/1005/1006）→ 保留 session 尝试 Resume；
  * 未知码 → 按官方建议 Identify。
  本端主动关闭（心跳 ACK 超时、队列满）用 `_close_intent` 标记意图，不会被误判成未知码而清空会话。
  鉴权失效：`GET /gateway` 收到 401 时作废 token 并刷新一次后重试。
* **回归**：
  * `test_close_code_classification_follows_official_table`（18 个参数覆盖整张表）
  * `test_close_code_4006_reidentifies_instead_of_resuming` — 真实本地 TCP WS，断言 `identify_ops == [2, 2]`
  * `test_close_code_4009_resumes_with_processed_watermark` — 断言第二轮 `op=6, seq=1`
  * `test_fatal_close_code_4915_stops_reconnecting` — 断言 `fatal is True`、`connections == 1`、0.15s 后仍为 1
  * `test_gateway_401_refreshes_token_once` — 断言 token 请求 2 次、gateway 请求 2 次

### M2｜事件先推 `_seq` 再处理，队列满直接丢弃（qq_official.py）

* **原缺陷**：读循环里收到 `s` 立刻写 `_seq`；事件任务上限 64，满了直接丢，Resume 不会补发。
* **修复**：
  * 两个独立指针：`_seq_received`（心跳用「最新收到」）与 `_resume_seq`（Resume 用「连续处理完成的水位」）。
  * 水位推进：`_track_received()` 入堆 + inflight 集合；`_track_completed()` 只在任务真正结束后出堆，
    且必须**连续**才推进（乱序完成后到的事件不会把水位跳过缺口）。
  * 有界队列：`asyncio.Queue(maxsize=bot.event_queue_size)` + `Semaphore(bot.event_workers)`。
  * 队列满：事件保持 inflight（水位不推进），记录 `queue_overflow`，并主动 `Resume` 让网关重投；
    **绝不把未处理事件标成已完成**。
  * 去重表只在**事件成功入队之后**写入（`_remember_id()` 移到 `put_nowait()` 之后）。
    队列满的事件不进 `_seen_ids`，网关 Resume 重投同一 `(group_openid, message_id)` 时能重新入队并投递；
    已成功入队/已完成的事件仍然去重（all 模式双投、Resume 重投不重复回调）。
  * 去重命中时若该 `seq` 的副本还在队列里（`_queued_seqs`），由那个副本负责推进水位，
    去重分支不会提前 `_track_completed()`。
  * 心跳在独立 task 内，不受事件处理阻塞。
* **回归**：
  * `test_watermark_advances_only_after_contiguous_completion` — 先完成 s=2、s=1 被阻塞时 `_resume_seq is None`，
    放行后才变 2。
  * `test_queue_overflow_does_not_advance_watermark_and_triggers_resume` — 真实 WS：`event_queue_size=1`、
    `event_workers=1`，灌 s=2/3/4 后 `overflow_events >= 1`、`_resume_seq is None`、重连帧 `op=6 seq=0`。
  * `test_queue_overflow_keeps_id_out_of_dedupe_table` — 溢出事件不在 `_seen_ids` 里、仍在 `_inflight`
    （已入队的 s=3 在去重表内）。
  * `test_queue_overflow_replay_delivers_event_exactly_once` — 真实 WS 全链路：队列满 → `op=6`（seq=0）→
    RESUMED 后重投 s=2/3/4（含溢出的 s=4）→ 回调按 message_id 恰好各一次（`M4` 计数 1），
    `_resume_seq == 4`。用旧实现（先记已见再入队）跑该项会失败：`assert 0 == 1`（M4 丢失）。
  * `test_resume_after_completed_event_does_not_redeliver` — 真实 WS：已处理的 s=2 被 4009 Resume 重投，
    回调仍是 `["M-1", "M-2"]`，`_resume_seq == 3`。
  * `test_all_mode_duplicate_dispatch_dedupes_while_copy_still_pending` — all 模式双投：副本还在队列中时
    `@` 事件去重（s=3 出 inflight 但水位仍为 `None`），放行后只回调一次、水位到 4。
  * `test_heartbeat_keeps_running_while_event_handler_blocked` — 处理被阻塞时仍收到 ≥2 次心跳。

### M3｜回环防护在 ACK 之后才登记（router.py）

* **原缺陷**：`await manager.broadcast()` 返回后才 `_loop_guard.add()`；服务端在 ACK 前回显就会漏抑制，
  且任一服务器慢 ACK 会延长所有服务器的空窗。
* **修复**：广播**前**用 `manager.online_targets()` 取目标集合，逐目标登记（key = `mc:<server>:<归一化文本>`）；
  广播失败的目标撤销登记；用引用计数区分并发同文广播，避免「一个失败把另一个仍在途的登记删掉」。
  manager 没有该能力时退回「所有启用服务器」（宁可多登记，不漏回环）。
* **回归**：
  * `test_echo_before_ack_is_suppressed` — ACK 被挂起时回显同文，`qq.sent == []`
  * `test_slow_server_does_not_delay_guard_for_other_servers` — 一台慢 ACK，另一台回显仍被抑制
  * `test_failed_broadcast_releases_loop_guard` — 广播失败的服务器不保留指纹，后续真实聊天照常转发
  * `test_broadcast_exception_releases_loop_guard` — 异常路径也撤销
  * `test_concurrent_identical_broadcasts_keep_guard_until_last_release` — 引用计数 2 → 归零

### M4｜查询期间的 join/leave 被旧快照覆盖、无 UUID 合并（queqiao.py）

* **修复**：
  * 玩家名单改为 `_PlayerEntry(name, uuid)`；join/leave 按 **UUID 优先**、无 UUID 时按名字（大小写不敏感）匹配；
    快照只有名字时，事件带来的 UUID 会补进条目。
  * 每条 join/leave 同时写入有界增量日志 `_player_log`（maxlen=512）并递增 `_player_epoch`；
    `query_players()` 记录查询开始时的 epoch，快照返回后用 `_apply_snapshot()` 重放「查询开始之后」的增量，
    因此旧快照不会抹掉查询期间的新事件。
  * 并发保护：`_query_lock` 串行化同连接的查询；连接**代次** `_generation` 在 teardown 时 +1，
    查询返回时代次变了就作废（`查询期间连接已重建，本次结果作废`）。
  * 断线清空名单时同时清空增量日志并递增 epoch。
* **回归**：
  * `test_query_merges_join_that_arrived_during_query` / `test_query_merges_leave_that_arrived_during_query`
  * `test_uuid_priority_prevents_duplicate_entry_and_removes_by_name`
  * `test_query_result_discarded_when_connection_rebuilt_during_query`
  * `test_concurrent_queries_are_serialized_and_last_snapshot_wins`（峰值并发 == 1，最后一次快照胜出）

### M5｜`receive_mode` 未参与过滤（qq_official.py / config.py）

* **修复**：
  * `mention` 模式只消费 `GROUP_AT_MESSAGE_CREATE`，普通 `GROUP_MESSAGE_CREATE` 不派发（直接完成该 seq）。
  * `all` 模式消费两者，并按 `(group_openid, message_id)` 统一去重（全量+@ 双投、Resume 重投都只处理一次），
    去重表有界（512）。
* **回归**：`test_mention_mode_only_consumes_at_events`、`test_all_mode_consumes_both_and_dedupes_by_message_id`。

### M6｜缺群状态/权限控制与拒绝后的冷却（qq_official.py）

* **修复**：
  * 新增 `refresh_bot_state()`：`GET /v2/groups/{group_openid}/bot_state`，解析
    `allow_proactive_msg` / `recv_msg_setting` / `member_role`；401 刷新 token 重试一次。
  * **启动探测 + 周期探测**：`start()` 起 `_bot_state_loop()`（间隔 `bot_state_interval_seconds`，默认 300s）。
  * 降级策略：`allow_proactive_msg=false` → 主动发送直接拒绝（`proactive_not_allowed`），被动回复不受影响；
    `11253`（仅白名单）/网络失败/字段缺失 → `unknown`，**不谎称已确认**，并在 `status.degraded` 与
    `/readyz.qq_degraded` 暴露 `proactive_state_unknown`、`recv_msg_setting_unknown`、`recv_msg_setting:<value>`。
  * 冷却/熔断：HTTP 429 与业务码 `304003/40034100/40034105` 触发指数冷却（默认 30s→…→600s，上限可配），
    冷却期内主动发送直接返回 `proactive_cooldown`（**不发请求**）；冷却到期恢复一次尝试，成功清零；
    `bot_state` 确认 `allow_proactive_msg=true` 也会立刻清零。
  * 事实更正：首轮 §6 第 12 条「官方文档没有可依赖的 bot_state 端点」是错的 —— 端点确实存在，
    本轮已实现；README §3 第 5 条同步更新。
* **回归**：
  * `test_bot_state_false_blocks_proactive_but_not_replies`
  * `test_bot_state_11253_is_unknown_but_not_fatal`
  * `test_bot_state_true_clears_cooldown`
  * `test_permission_error_sets_bounded_cooldown_then_recovers`（冷却期 3 次调用 0 请求；到期后恢复）
  * `test_429_sets_cooldown_after_bounded_retry`
  * 集成测试断言启动时 `bot_state_calls >= 1`、`allow_proactive_msg is True`、`degraded_reasons() == []`

### M7｜HTTP 200 坏 JSON/空 body 被当投递成功（qq_official.py）

* **修复**：成功判定改为「HTTP 200 + `code ∈ {None,0}` + 响应体是 JSON 对象 + 能取到非空字符串消息 id」
  （`data.id` 或 `id`）。坏 JSON / 数组 / 空 body / 缺 id → `SendResult(accepted=False, delivered=False,
  reason="invalid_response")`。业务错误日志只记 `http_<status>_code_<code>`。
* **回归**：`test_invalid_success_response_is_not_delivered`（`not json`、空 body、`[]`、`{}`、
  `{"code":0,"data":{}}`、`{"id":""}` 共 6 例）、`test_valid_nested_success_response_is_delivered`（`data.id` 正例）。

### M8｜上游原文/凭据进入日志与 status（qq_official.py / queqiao.py / redact.py）

* **修复**：
  * 新增 `src/mcqq_bridge/redact.py`：`error_kind()` 只返回异常类型名；`scrub()` 抹掉 `Bearer/QQBot ***`、
    URL userinfo、`token=/password=` 等形态，并压成单行。
  * QueQiao `_check_response()` 不再拼接上游 `message`；`_run()` 的 `last_error` 改为
    `connect_failed:<ExcType>[:http_<status>]`；广播/事件/同步失败日志只用异常类型名。
  * QQ 侧网关异常、发送失败、token 失败同理；`status_payload()` 里的 `error`/`bot_state.reason` 均为受控文本；
    状态接口不返回群 OpenID、token、消息正文（新增 `bot_state`、队列水位等字段也不含敏感值）。
* **回归**：
  * `test_upstream_message_never_reaches_logs_or_status`（QQ：响应 message 与 bot_state message 都塞 canary，
    断言 `caplog.text` 与 `status_payload` JSON 均不含）
  * `test_network_error_text_with_credentials_is_scrubbed`（异常文本含 `user:pw@` 与 `access_token=CANARY_TOKEN`）
  * `test_upstream_error_message_never_logged_or_in_status`（QueQiao broadcast 失败）
  * `test_connect_error_is_controlled`（`connect_failed:Boom:http_403`，不含 URL/canary）
  * 真实进程探测：配置里的 `client_secret` canary 与 `group_openid` 均未出现在日志/status（§8）

### M9｜配置未开 strict，bool/int/float 隐式转换（config.py）

* **修复**：`_Model.model_config` 增加 `strict=True`（保留 `extra="forbid"`、`str_strip_whitespace`）。
  于是 `enabled: "yes"`、`enabled: 1`、`tls: 1`、`port: true`、`port: 18080.0`、
  `request_timeout_seconds: "10"`、`intents: 33554432.0` 全部拒绝；
  `float` 字段按契约接受整数（`10` 合法），但 `bool` 不能当数字、数字不能当 bool。
  错误信息仍只输出字段路径 + pydantic 的 `msg`（不含 `input_value`），因此不会回显配置值。
  新增字段也纳入同一模型：`event_queue_size`、`event_workers`、`bot_state_probe_enabled`、
  `bot_state_interval_seconds`、`proactive_cooldown_initial/max_seconds`、`reconnect_initial_seconds`，
  并校验 `initial <= max`。
* **回归**：
  * `test_server_fields_reject_wrong_types`（11 组）
  * `test_bot_fields_reject_wrong_types_or_ranges`（10 组）
  * `test_int_accepted_where_float_expected`
  * `test_type_errors_do_not_echo_values`（canary 值不出现在错误里）
  * `test_bot_cooldown_bounds_rejected`
  * `--check-config` 仍可通过 `config.example.yaml`（§8）

---

## 4. 评论中的额外缺口

| # | 缺口 | 修复 | 证据 |
|---|---|---|---|
| E1 | 分片截断说明使单片超过 `max_chars`（实测 50/2 → [50, 61]） | `split_message()` 为截断说明预留空间，说明本身也受 `max_chars` 约束（上限比说明还短时只截说明） | `test_split_message_truncation_never_exceeds_max_chars`（50/2 与 5/1 都 ≤ 上限）、`test_split_message_exact_boundary` |
| E2 | README 只写 QueQiao RCON 客户端，未写 MC `server.properties` | 新增 §4.1「两边都要配」：MC 侧 `enable-rcon/rcon.port/rcon.password` + 重启 + `mcrcon list` 验证；QueQiao 侧 `rcon.enable/port/password`（官方只有这三个字段，无 host）；桥接器侧无需配置 | `README.md` §4.1；官方配置页 <https://queqiao-docs.pages.dev/config> |
| E3 | 普通本地 Python 入口不加载 `.env`，FAQ 却称 `.env` 可用 | 真正实现 `--env-file`（极简 KEY=VALUE 解析、不覆盖已有变量、非法行/缺失文件退出码 2）；README §5/FAQ 与 `.env.example` 改为「不会自动加载，需 `--env-file` 或 shell export」 | `test_env_file_is_actually_loaded`（不传 → 运行前校验失败；传 → 解析成功）、`test_env_file_problems_return_nonzero` |
| E4 | 缺精确 loader/MC 版本产物矩阵，把泛平台列表当已验证 | README §2.1 + §4 给出实测/核对/未验证三态矩阵：GitHub release v0.5.0 单资产 + core 0.6.8；Modrinth 983 个版本文件（最新 `0.5.3+fabric-26.3`）；loader 范围为上游 README 徽章口径（Spigot 1.12.2+/Paper 1.17.1+/Folia 1.21.4+/Velocity 3.3.0/Forge 1.7.10–1.21/Fabric 1.16.5–1.21.8/NeoForge 1.21.1），**逐行标注「未装真实插件」** | `README.md` §2.1、§4；GitHub/Modrinth API 结果见 §9 |
| E5 | 现有集成测试只有 2 台真正连接，却宣称 3 台 | `World` 扩为 3 启用（主服/生存服/创造服）+ 1 禁用（资源服），断言 `connections == 1/1/1/0`、`connected_count == 3`、禁用项零连接；`/list`、广播、断线隔离断言同步更新 | `test_three_enabled_servers_connect_independently_plus_one_disabled`、`test_list_reflects_online_zero_and_disabled`、`test_qq_message_broadcast_to_all_online_servers` |
| E6 | 原验证报告夸大「3 条连接」 | 本文件顶部「与首轮报告的差异」+ §6.2 明确记录更正 | 本文 |

---

## 5. 关键实现取舍（可审查）

* **unknown 状态的安全策略**：`bot_state` 不可用（11253/网络失败）时**仍尝试**主动发送，但在
  `status.degraded`/`readyz.qq_degraded` 明确标记 `proactive_state_unknown`；只有官方明确返回
  `allow_proactive_msg=false` 才硬性禁止。理由：11253 是权限问题而非「不允许推送」，
  直接把所有主动消息禁掉会让大多数部署不可用；而 degraded 暴露让人知道「未确认」。
* **队列满的策略**：丢弃新事件 + 主动 Resume。因为水位不推进，网关会重投包括被丢弃事件在内的所有未确认事件；
  代价是一次重连，收益是「不丢消息」。
* **冷却错误码集合**：`{304003, 40034100, 40034105}` 是当前已知的权限/配额类业务码，写成常量便于按官方新增扩展；
  未命中的错误码只记录失败、不进入冷却（不会误熔断）。
* **回环防护兜底**：manager 没有 `online_targets()` 时按「所有启用服务器」登记，宁可多抑制一次也不放任自激。

---

## 6. 自动化测试（真实执行）

### 6.1 全量

```console
$ .venv/bin/python -B -m pytest -q -p no:cacheprovider
........................................................................ [ 31%]
........................................................................ [ 63%]
........................................................................ [ 94%]
............                                                             [100%]
228 passed in 3.90s
```

按文件统计（`pytest --collect-only` 实测）：

```
  6 tests/test_bridge.py            44 tests/test_config.py
 10 tests/test_features.py          11 tests/test_hardening.py
  6 tests/test_hermes_admin.py       6 tests/test_integration_protocol.py
 64 tests/test_qq_official.py       29 tests/test_queqiao.py
 12 tests/test_review_regressions.py 21 tests/test_router.py
 11 tests/test_runtime.py            2 tests/test_security.py
  6 tests/test_websocket_safety.py
```

首轮 140 项全部保留（旧 OneBot MVP 回归 53 项 + 首轮新增 87 项）。其中 **1 项改名、3 项断言随实现更新**：
`test_resume_identify_uses_session` → `test_resume_identify_uses_resume_watermark`（Resume 现在用处理水位而非最新 s）、
`test_status_endpoints_report_readiness`（`/readyz` 新增 `qq_degraded` 字段）、
`test_three_servers_connect_independently_with_handshake_headers` → `..._plus_one_disabled`（原断言夸大，见顶部更正）。
本轮新增 84 项 → **224**；第二轮返工再新增 4 项（队列满重投恰好一次、溢出不进去重表、已完成 Resume 不重复、
all 双投去重）→ **228**。

### 6.2 新增覆盖点（本轮）

| 文件 | 覆盖 |
|---|---|
| `tests/test_qq_official.py` | M1 关闭码表（18 参数）+ 真实 WS 4006→[2,2] / 4009→op6 seq1 / 4915→终止；gateway 401 刷新；M2 乱序完成水位、队列满不推进水位且 Resume、溢出事件不进去重表且 Resume 重投后恰好投递一次、已完成事件 Resume 不重复、all 双投在副本未完成时去重、事件阻塞时心跳继续；M5 mention/all 过滤与统一去重；M6 bot_state false/11253/true、权限码冷却与恢复、429 冷却；M7 6 类非法成功响应 + 合法嵌套 id；M8 canary 日志/状态、URL 凭据脱敏 |
| `tests/test_queqiao.py` | M4 查询期间 join/leave 合并、UUID 优先、断线代次作废、并发查询串行；M8 上游 message 不进日志/状态、连接错误受控 |
| `tests/test_router.py` | M3 ACK 前回显抑制、慢服不影响其它服、失败/异常撤销登记、并发引用计数、成功路径不漏计数；E1 分片上限（含极短上限） |
| `tests/test_config.py` | M9 strict bool/int/float（21 组负例 + 整数写 float 正例 + 不回显值 + 冷却边界） |
| `tests/test_integration_protocol.py` | E5 3 启用 + 1 禁用的真实 TCP 场景；启动 bot_state 探测成功且无 degraded |
| `tests/test_runtime.py` | E3 `--env-file` 真实加载/错误退出；`/readyz.qq_degraded` 暴露 degraded |

### 6.3 集成测试的真实性边界

`tests/test_integration_protocol.py` 与 `scripts/smoke_official.py`：
* QueQiao 侧是**真实 TCP WebSocket 服务端**（校验 `x-self-name`/`Authorization`、按 Protocol V2 应答
  `api`/`echo`、回 `code/status`、推送事件）；
* QQ 侧是**真实 TCP WebSocket 网关**（Hello/Identify/READY/心跳 ACK）+ `httpx.MockTransport` 记录真实 HTTP 请求；
* **没有**连接真实 QQ 或真实 Minecraft。

---

## 7. 端到端协议 smoke（真实执行）

```console
$ .venv/bin/python -B scripts/smoke_official.py
```

真实输出：

```text
[PASS] QQ 网关与本机 3 台启用 QueQiao 均已连接
[PASS] 连接后玩家名单同步（send_rcon_command → list） - ['Alice', 'Bob']
[PASS] x-self-name 握手头按 QueQiao 规则 URL 解码后匹配 - ['主服'] / ['生存服'] / ['创造服']
[PASS] 3 台启用各自一条独立连接，禁用服务器零连接 - connections=[1, 1, 1, 0]
[PASS] /list 返回在线/零人/未启用且命令不下发 MC - '主服 2人在线：\nAlice\nBob\n\n生存服 0人在线\n\n创造服 1人在线：\nCarol\n\n资源服 未启用'
[PASS] /list 未广播到任何 MC 服务器
[PASS] QQ 消息按「昵称：内容」广播到所有在线服务器
[PASS] MC 聊天按「[服务器] [玩家] ： 内容」转发 QQ - '[主服] [Alice] ： 大家好'
[PASS] manager.query 按配置顺序返回 4 台（3 启用 + 1 禁用） - 主服 2人在线： / Alice / Bob /  / 生存服 0人在线 /  / 创造服 1人在线： / Carol /  / 资源服 未启用
[PASS] 启动时真实探测 bot_state（allow_proactive_msg=true，无 degraded） - calls=1 degraded=[]

SMOKE PASS: 10/10 项通过
```

---

## 8. 真实进程验证（新入口 / 状态接口 / SIGTERM / 脱敏）

临时配置：`bot.api_base=http://127.0.0.1:9`（指向关闭端口，必然失败）、1 台启用服务器（127.0.0.1:9）、
1 台禁用、`status_http` 监听 127.0.0.1:18099、`client_secret` 填 canary 字符串
`CANARY_SECRET_ONLY_TEST`、`group_openid=GROUP_OPENID_CANARY`。用真实子进程启动并 SIGTERM：

```console
$ .venv/bin/python -m mcqq_bridge --config <tmp>/config.yaml   # 真实子进程
=== exit code: 0
=== /healthz: 200
{"status":"ok"}
=== /readyz: 200
{"status":"degraded","qq_connected":false,"servers_enabled":1,"servers_connected":0,
 "qq_degraded":["proactive_state_unknown","recv_msg_setting_unknown"]}
=== /api/v1/status: 200
{"qq":{"connected":false,"receive_mode":"all","proactive_enabled":true,"reconnects":1,
 "fatal":false,"error":"获取 access_token 失败：ConnectError","queue_depth":0,"queue_capacity":64,
 "queue_overflow":0,"event_workers":4,"resume_seq":null,"seen_seq":null,
 "bot_state":{"checked":false,"unknown":true,"allow_proactive_msg":null,"recv_msg_setting":"",
              "member_role":"","reason":"获取 access_token 失败：ConnectError","checked_at":0.0},
 "degraded":["proactive_state_unknown","recv_msg_setting_unknown"]},
 "servers":[{"name":"主服","enabled":true,"host":"127.0.0.1","port":9,"connected":false,"players":0,
             "player_source":"","last_sync_ok":false,"reconnects":1,
             "error":"connect_failed:ConnectionRefusedError"},
            {"name":"备用服","enabled":false,...,"connected":false,"reconnects":0,"error":""}]}
=== canary in log: False
=== canary in status: False
=== group_openid in status: False
```

日志（真实输出，节选）：

```
INFO mcqq_bridge.runtime: 启动 mcqq-bridge：2 个服务器（1 启用），QQ 接收模式=all，主动消息=启用
INFO mcqq_bridge.runtime: 服务器配置：主服 -> 127.0.0.1:9 enabled=True query=rcon
INFO mcqq_bridge.runtime: 服务器配置：备用服 -> 127.0.0.1:10 enabled=False query=auto
INFO mcqq_bridge.manager: 启动服务器连接：主服
INFO mcqq_bridge.runtime: 状态 HTTP 监听 127.0.0.1:18099（/healthz、/readyz、/api/v1/status）
WARNING mcqq_bridge.queqiao.主服: 连接/读取失败：connect_failed:ConnectionRefusedError
INFO mcqq_bridge.queqiao.主服: 将在 0.6 秒后重连（第 1 次）
WARNING mcqq_bridge.qq: QQ 网关错误：获取 access_token 失败：ConnectError
INFO mcqq_bridge.qq: 0.4 秒后重连 QQ 网关（第 1 次）
INFO mcqq_bridge.runtime: 正在停止 mcqq-bridge
INFO mcqq_bridge.runtime: 已停止
```

`--check-config` 实测：

```console
$ .venv/bin/python -B -m mcqq_bridge --config config.example.yaml --check-config
配置校验通过：3 个服务器（2 个启用）
  - 主服: 127.0.0.1:8080/ [启用] 查询=auto tls=False
  - 生存服: 127.0.0.1:8081/ [启用] 查询=auto tls=False
  - 创造服: 192.168.10.30:8080/ [禁用] 查询=auto tls=False
真正运行前还需要补齐：
  - bot.app_id 未配置
  - bot.client_secret 未配置（可用 ${QQ_BOT_CLIENT_SECRET} 引用环境变量）
  - bot.group_openid 未配置（群 OpenID，不是群号）
exit=0
```

`--env-file` 行为（单测覆盖；命令形态）：

```bash
# 不传 --env-file：.env 不生效，require_secrets=True 的加载会报 bot.client_secret 未配置
python -m mcqq_bridge --config config.yaml --check-config
# 传 --env-file：解析 KEY=VALUE 并注入 os.environ（不覆盖已有值），随后校验通过
python -m mcqq_bridge --config config.yaml --check-config --env-file .env
```

---

## 9. 支持版本矩阵（真实数值 + 来源）

| 项目 | 数值 | 来源（本轮实际请求） | 验证状态 |
|---|---|---|---|
| GitHub release latest | `v0.5.0`（2026-04-06），资产仅 `QueQiao-vanilla+minecraft-0.5.0.jar` | `GET api.github.com/repos/17TheWord/QueQiao/releases/latest` | 已核对 |
| 历史 release | v0.3.8 / v0.3.7 / v0.3.6 / v0.2.13 / v0.2.11 / v0.2.7 / v0.2.6（每个 tag 同样只 1 个 jar） | `GET .../releases?per_page=8` | 已核对 |
| 内置 core | queqiao-core/tool **0.6.8**（`tool_version.txt`） | 仓库源码 | 已核对 |
| Modrinth 版本文件数 | 983；最新 `0.5.3+fabric-26.3`；存在 0.5.1(paper/spigot/neoforge/forge)、0.5.2/0.5.3(fabric) | `GET api.modrinth.com/v2/project/queqiao/version` | 已核对 |
| loader 范围 | Spigot 1.12.2–latest；Paper 1.17.1–latest；Folia 1.21.4–latest；Velocity 3.3.0；Forge 1.7.10–1.21；Fabric 1.16.5–1.21.8；NeoForge 1.21.1 | 上游 README 徽章 | 上游声明，**未实测** |
| Minecraft 服务端 | 未运行 | — | ❌ 未验证 |
| QQ 官方平台 | 无 AppID/Secret/群权限 | — | ❌ 未验证 |

---

## 10. 未验证 / 限制（必须如实告知）

1. **真实 QQ 官方平台未验证**：没有 AppID/ClientSecret、没有可用测试群，因此 token 获取、`/gateway`、
   真实 `GROUP_MESSAGE_CREATE` 全量推送、群消息发送、`msg_seq` 5 次上限、主动消息频控/配额、
   `bot_state` 真实返回（尤其 `allow_proactive_msg` 实际取值与 11253 白名单行为）都**只按官方文档实现并用本地模拟验证**。
2. **真实 Minecraft + QueQiao 未验证**：本机没有运行中的 MC 服务与 QueQiao 插件，`send_rcon_command list`
   的真实输出、各服务端本地化格式、`player_death` 字段差异均未实测；`list` 解析对英文/中文常见格式做了单测，
   但**不保证覆盖所有本地化/插件改写输出**。
3. **哪些 loader/MC 版本能用没有实测**：见 §9，只有上游声明范围，没有任何 loader 被真实联调。
4. **RCON 前提**：`/list` 需要 MC `enable-rcon` 与 QueQiao `rcon.enable` **两侧都配**且口令一致
   （README §4.1）。未配置时显示离线（有意失败，不冒充成功）。
5. **全量接收依赖群权限**：`receive_mode: all` 需群内开启「接收所有消息」；无权限时只能收到 @ 消息。
   本轮新增的 `bot_state.recv_msg_setting` 只能**暴露**这一事实（不符时 degraded），不能改变平台权限。
6. **死亡事件平台限制**：vanilla / Velocity 不支持；部分平台 `death.text` 为空，此时显示翻译 key/参数。
7. **Docker 未构建**：当前用户不在 `docker` 组且无交互 sudo，未执行 `docker build`/`compose up`。
   Dockerfile/compose 已指向新入口，但**未实测**（compose 用 `environment` 透传密钥，不依赖 `.env` 自动加载）。
8. **单机器人单群**：不支持多群、私聊、频道；非目标群事件直接忽略。
9. **昵称**：优先官方 `author.username`；缺失时用 `relay.nickname_fallback`，**不保证是群名片**。
10. **图片顺序**：有 `msg_elements` 时按元素顺序；只有 `content`+`attachments` 时占位符统一追加在文本后，
    无法还原原始交错顺序。
11. **回环抑制**是有界时间窗口近似（默认 5 秒）＋广播前逐目标登记；若服务端回显延迟超过窗口，
    仍可能漏一次，属缓解而非强一致。
12. **`status` 模式**只在 ping 采样完整（条数==online）时可用；这是刻意的安全失败。
13. **`proactive_cooldown` 的业务码集合**（304003/40034100/40034105）来自已知错误码，未穷举；
    未命中的拒绝码只记录失败、不进冷却。
14. **`bot_state` 接口仅白名单机器人可用**（官方 11253）。普通机器人会一直 `unknown`，
    此时 `/readyz` 会一直是 `degraded` 且带 `proactive_state_unknown`——这是事实暴露，不是故障。
15. **事件处理并发度**默认 4（`event_workers`），因此同一时间到达的多条群消息在 QQ 侧的**发送顺序不保证**
    （内容与去重不受影响）；需要严格顺序可设为 1。
16. **未验证 QueQiao 0.5.1~0.5.3** 的行为差异：本仓库只按 v0.5.0/core 0.6.8 的协议形状实现。

---

## 11. 复现步骤

```bash
cd /home/czauw/minecraft-qq-bridge
.venv/bin/python -B -m pytest -q -p no:cacheprovider          # 228 passed
.venv/bin/python -B scripts/smoke_official.py                 # SMOKE PASS 10/10
.venv/bin/python -B -m mcqq_bridge --config config.example.yaml --check-config
# 真实进程（需自行准备临时配置；关键点：status_http 监听 18099、服务器指向关闭端口）
.venv/bin/python -B -m mcqq_bridge --config /tmp/probe-config.yaml &
curl -fsS http://127.0.0.1:18099/healthz
curl -fsS http://127.0.0.1:18099/readyz
curl -fsS http://127.0.0.1:18099/api/v1/status
kill -TERM %1
```

---

## 12. 历史归档

旧版（OneBot v11 / HTTP webhook MVP）的验证记录见
`docs/LEGACY_ONEBOT_VERIFICATION.md`，旧 README 见 `docs/LEGACY_ONEBOT_README.md`。
那些结果只证明旧模块的回归测试，不代表本轮官方 QQ/QueQiao 能力。
