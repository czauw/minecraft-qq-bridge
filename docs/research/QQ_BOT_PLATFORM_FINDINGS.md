# QQ 官方机器人能力与接入约束核实

> 调研范围：仅核实 QQ 开放平台官方 QQ Bot，不包含 OneBot、NapCat、NTQQ 登录实现或任何第三方 QQ 客户端。  
> 官方文档获取时间：**2026-11-10T03:54:36Z**。本文中的“官方已确认”仅指所列官方页面在该时间点的公开内容；管理端按主体、机器人、群权限和平台审核实际状态可能另有限制。

## 1. 结论摘要

1. **官方 QQ Bot 可以承载“MC 状态查询/受控通知”**：机器人加入群后，可接收 `GROUP_AT_MESSAGE_CREATE`（群成员 @机器人）并以携带 `msg_id` 的群消息被动回复；群消息发送接口公开了被动回复时效/次数及主动消息频控。[7][9][10]
2. **MC→QQ 出站通知与 QQ→MC 入站监听是两个独立权限面**：前者取决于群主动发言许可、发送频控/配额及内容政策，不要求订阅 QQ 全量消息；后者若要把任意群消息镜像到 MC，才依赖 `GROUP_MESSAGE_CREATE` 全量接收能力和群主设置，且不是默认能力，不能无条件承诺。[4][8][9][10][11]
3. **最稳妥的官方方案是 @bot 查询 + 低频、经允许的通知**：例如 `/mc players`、`/mc status` 由群成员 @机器人触发；MC 事件只做聚合、去抖后的通知，并在群管理员开启主动推送且平台权限/审核允许时发送。不能把“加入群”推导成普通群消息全量监听或无限主动推送。[unverified]
4. **本仓库当前实现不是官方 QQ Bot 适配器**。它明确使用 OneBot v11 HTTP/反向 WebSocket，并把真实 QQ 登录、风控和事件上报交给外部 OneBot；代码与 README 均如此声明。[代码：`README.md:1-5,97-131`；`src/mcqq_bridge/onebot.py:17-48,51-66`]
5. **官方 API 当前公开使用 `https://api.bot.qq.com`**。以 AppID 与 ClientSecret 换取 access_token，默认有效期不超过 7200 秒；临近到期 60 秒内重新获取可得到新 token。[2][3]
6. **Webhook 与 Gateway WebSocket 均在当前官方文档中支持，未发现“WebSocket 已弃用”的官方证据**。Webhook 需要 HTTPS 回调和平台验证；WebSocket 需要 Gateway、Identify、心跳、断线 Resume。官方介绍页把二者描述为可切换接入方式，实际选择会立即生效。[5][6]

## 2. 主体资质、创建、测试与上线

### 2.1 申请前置条件

官方“启动接入”要求注册/创建机器人后取得 AppID、AppSecret；旧 Bot Token 鉴权已标注为弃用，应使用 Access Token。[1]官方 FAQ 说明，创建机器人失败首先检查主体是否完成认证，以及登录账号是否为开放平台超级管理员；个人主体和企业主体的机器人创建名额不同，页面当前列出个人主体 5 个、企业主体账号 10 个且同一企业主体上限 50 个。[11]

官方接入指南说明主体认证包括个人或企业/个体户等主体资料和相应资质；企业/个体户需要工商信息等材料，具体以管理端当时表单和审核要求为准。此处不把搜索摘要中的材料清单扩大解释成所有主体的固定清单。[11][unverified]

### 2.2 测试/体验与上线

官方 FAQ 的当前表述是：新版机器人管理端中，管理员账号可直接测试使用，**无需另行添加旧版“沙箱环境”**；旧版管理端已经不支持添加沙箱群/频道配置。[11]这意味着本项目不应假设存在一个可由代码自动申请、自动加入的公开沙箱群。

官方 FAQ 还说明：企业主体公开使用已开放，个人主体暂不支持公开使用；个人主体机器人可由创建者直接添加到自己是群主/管理员的群，私聊体验用户可在开发设置添加。[11]因此“创建成功”不等于“任意 QQ 群可公开添加、可上线全量运营”。最终上线范围、公开服务开关、审核/权限仍以管理端显示和平台审核为准。[unverified]

### 2.3 运营合规

官方运营规范要求机器人功能与简介一致、提供功能指引、设置侵权投诉机制，并对用户数据收集、使用、披露和删除承担约束；平台可因违规限制接口、封禁或下架。[12]MC 聊天桥接应明确告知用途和范围，避免把群成员消息无必要地长期存储或转发到第三方；应提供关闭同步、群白名单、内容过滤、审计与删除策略。

## 3. 群事件与身份标识

### 3.1 @机器人事件

`GROUP_AT_MESSAGE_CREATE` 是用户在群内 @机器人时触发的事件；官方说明 `content` 已自动去除 @机器人前缀，事件中包含消息 `id`、发送者、`group_openid`、时间、附件等。相同 `msg_id` 可能重复推送，开发者须结合 `msg_seq` 去重。[7]

群发送接口将事件的 `d.id` 作为被动回复的 `msg_id`；被动回复必须在 5 分钟内完成，每条消息最多回复 5 次。[9]因此处理链路应先做幂等去重，再在短超时内生成可发送的简短结果；不能把同步调用 MC/LLM 的长任务直接当成可无限延迟的被动回复。[unverified]

### 3.2 全量群消息事件

官方 `GROUP_MESSAGE_CREATE` 页面明确写的是：机器人开启“接收所有消息”功能后，群内每一条消息（不限于 @机器人）才会推送；其字段与 @事件基本一致，仍使用 `group_openid`、发送者 `member_openid` 等标识。[8]官方通用 Intents 页面同时注明群全量消息能力受 Intents/权限控制，群消息事件不能按频道公域消息的默认规则类推。[4]

官方 FAQ 对群权限的解释是：群主可设置“接收群内全部消息”和“主动在群聊内发言”；机器人资料页可查询到 `recv_msg_setting`（`all`、`only_mention`、`mention_and_context`）和 `allow_proactive_msg` 等状态。[11][13]故本项目的能力判断应为：

- 默认可规划：仅 @bot 触发的查询/命令；
- 只有在平台给该机器人相应能力、且群主明确开启全量接收时，才可评估全量消息；
- 即便全量接收开启，也不能据此承诺永久、无延迟、无丢失的全文镜像；仍须受事件连接、权限、风控、限频和运营规范约束。[unverified]
- `group_openid`/`member_openid` 是 Bot 作用域的 OpenID，不是可直接替代 QQ 群号/QQ 号的全局公开身份。

### 3.3 group_openid/member_openid 的作用域

官方 API 调用指南说明，不同 Bot（AppID）得到的用户、群、频道 OpenID 不相同：群内群标识称 `group_openid`，群内成员标识称 `member_openid`；跨 AppID 关联需使用平台后续提供的绑定/统一标识机制，不能自行把不同 Bot 的 OpenID 当作同一身份。[3]事件示例也将群发送路径设计为 `/v2/groups/{group_openid}/messages`，将群成员身份放在 `member_openid`。[7][9]

工程含义：配置和持久化应以“AppID + group_openid”作为作用域；不要把用户 `member_openid` 映射成可跨机器人复用的 QQ 号，不要在日志或跨业务数据集中暴露不必要的 OpenID。[unverified]

## 4. Access Token、OpenAPI 域名与 Gateway

### 4.1 Token 获取和刷新

请求：`POST https://api.bot.qq.com/app/getAppAccessToken`，JSON 参数为 `appId`、`clientSecret`。成功返回 `access_token`、`expires_in`；官方当前页面说明生命周期默认 7200 秒以内。有效期内重复请求不会每次生成新 token；上一个 token 接近过期 60 秒内重新获取时，会返回新 token，旧 token 在该 60 秒窗口仍有效。[2]

OpenAPI 调用统一地址是 `https://api.bot.qq.com`，请求头格式为 `Authorization: QQBot {AccessToken}`；凭证必须只在服务端使用，不应放入前端。[2][3]官方文档提醒：部分业务错误仍可能返回 HTTP 200，必须检查 JSON 的业务 `code`，不能只看 HTTP 状态。[2]

建议实现：依据响应中的实际 `expires_in` 记录到期时间；可在剩余约 120 秒时做预检查/安排任务，但此时仍在官方所述换新窗口之外，重复获取可能返回原 token，不能视为续期成功。将实际换新请求安排在到期前 60 秒窗口内并预留网络余量，收到新响应后再按新 `expires_in` 重算；并发刷新加锁；认证失败仅做一次受控刷新重试；日志不记录 AppSecret、access_token 或完整请求头。[2][unverified]

### 4.2 Gateway WebSocket 当前状态

官方 WebSocket 文档当前仍定义：先调用 WSS 接入点，连接示例为 `wss://api.bot.qq.com/websocket/`；收到 OpCode 10 Hello 后，以 OpCode 2 Identify 鉴权，token 格式为 `QQBot {AccessToken}`，按 heartbeat_interval 发送心跳，并可使用 OpCode 6 Resume 恢复短时断线会话。[6]官方通用结构列出 WebSocket 的 Dispatch、Heartbeat、Identify、Resume、Reconnect、Invalid Session 等行为。[4]

官方介绍页明确将 WebSocket 与 Webhook 都列为可选接入方式，切换后立即生效；没有找到当前官方页面声明“Gateway WebSocket 弃用”的证据。[5][6]此结论不等于承诺永远不变：应在实施前再次查看官方变更记录和管理端状态。

## 5. Webhook 验签、challenge 与防重放

### 5.1 官方回调验证

Webhook 需要 HTTPS 回调地址；官方当前列出的可配置端口为 80、443、8080、8443。配置后平台发送 `op=13` 回调验证，请求数据 `d` 包含 `plain_token` 和 `event_ts`，服务端返回同一个 `plain_token` 与计算得到的 `signature`。[5]

Webhook challenge 的算法为：以 Bot Secret 派生 Ed25519 私钥，对 `event_ts || plain_token` 签名，并返回十六进制 signature。[5][14]

业务回调与 challenge 不同。官方签名说明要求读取 `X-Signature-Ed25519`（十六进制签名）和 `X-Signature-Timestamp`，按 Bot Secret 重复/截取规则派生 Ed25519 公钥，并验证 `timestamp || 原始 HTTP Body`。必须使用接收的原始 body 字节，不可先解析再 JSON 重序列化；验签成功后才处理事件。不能将 challenge 的 `event_ts || plain_token` 规则误用于业务回调，也不能将 OneBot 的 HMAC-SHA1 `X-Signature` 移植过来。[14]

### 5.2 防重放边界

官方资料要求业务回调签名验证；本次查阅的签名页明确了签名材料，但**未找到足够明确的官方事件时间窗、nonce 或服务端去重键规范**，因此不能声称“验签即完成防重放”。工程上应校验时间戳合理窗口（窗口值需由业务风险决定并记录）、按事件 `id`/平台序列字段建立有 TTL 的幂等去重、对重复事件返回平台要求的 ACK、持久化必要处理状态；若平台后续文档规定不同窗口或字段，应以其为准。[4][5][14][unverified]

## 6. 群消息发送：被动回复、主动推送与配额

### 6.1 被动回复

群消息 POST 路径为 `/v2/groups/{group_openid}/messages`，官方当前页面给出接口级 100 QPS；被动消息有效期 5 分钟、每个消息最多回复 5 次。请求体携带 `msg_id`（来源于 `GROUP_AT_MESSAGE_CREATE` 等事件）才是被动回复；相同 `msg_id + msg_seq` 重复发送会失败，`msg_seq` 可递增实现同一事件的多次回复。[9]

### 6.2 主动消息

不携带 `msg_id`/适用事件标识的发送属于主动消息。官方消息概述说明，用户可以在客户端关闭“允许主动发送”，关闭后主动消息会发送失败。[10]群主动消息当前公开频控为：企业认证/个人身份证认证 Bot 维度 60 QPM，未认证 30 QPM；单关系维度 20 QPM，每个群每天最多 1000 条。[9][10]

“主动推送许可”不是单纯的 API 速率问题：群管理员需要允许机器人主动在群内发言，机器人群状态还会反映 `allow_proactive_msg`；未获许可时应视为不可推送，而不是反复重试。[11][13]平台审核、内容安全、群权限和用户/群侧开关都可能导致主动发送失败；官方错误码中也区分主动消息超频、无权限、群非成员、消息内容违规等情况。[9]

### 6.3 对 MC 同步的限制

因此不应实现“MC 每条聊天都无条件发 QQ 群”的默认路径；这是 MC→QQ 主动出站发送的许可、配额和内容政策约束，与是否接收 QQ 群全量消息无关。[9][10][11][unverified] 若确需通知，应：

- 只对明确授权群发送；
- 聚合/去抖/限长，优先状态摘要、上线/下线摘要和管理员配置的告警；
- 维护 Bot 维度、群维度和每日配额计数；
- 读取并记录群机器人状态，`allow_proactive_msg=false` 时停止主动发送；
- 被动 @回复只回原始问题，不把长时间任务塞进 5 分钟窗口；
- 不把发送 API 返回“成功”解释为用户已读或游戏状态已同步。

## 7. 对本仓库的适配评估

### 7.1 当前代码事实

当前仓库 README 明确称其为 OneBot v11 HTTP API/反向 WebSocket 适配器，不绑定 QQ 登录实现；OneBot 外部组件负责登录、风控、事件上报。[代码：`README.md:1-5,122-131`]出站实现调用 `${ONEBOT_API_URL}/send_group_msg`，并用整数 `group_id` allowlist；入站实现接受 OneBot group message，识别 `/mc` 前缀。[代码：`src/mcqq_bridge/onebot.py:27-48`；`src/mcqq_bridge/main.py:100-122`][unverified]

MC→QQ 目前按配置开关将 chat/death/join/leave 文本发送到 allowlist 群；QQ→MC 默认只处理固定 `/mc players`、`/mc status`，`say` 需显式开启并通过固定 HTTP callback，不执行 shell/RCON。[代码：`src/mcqq_bridge/service.py:20-45`；`src/mcqq_bridge/service.py:47-113`；`README.md:111-120,141-156`]

### 7.2 结论与替代路径

**不能把当前 OneBot 适配器直接宣称为官方 QQ Bot 适配器。** 两者事件名、身份类型、鉴权、消息 API 和风险边界不同。若改接官方 QQ Bot，应新增独立 adapter，而不是把 `group_id:int`、OneBot HMAC 或 `send_group_msg` 改名伪装成 `group_openid`/官方 API。

推荐分层：[unverified]

1. **MVP（推荐）**：保留当前 OneBot 方案作为实验/内网适配；官方方案另做明确的 `QQBotAdapter`，使用 `group_openid`、Access Token、官方事件 ACK/幂等和官方群消息 API。[unverified]
2. **官方低风险能力**：实现 `GROUP_AT_MESSAGE_CREATE` → 解析 `group_openid/member_openid` → 查询本地 MC 快照 → 5 分钟内被动回复。对应当前仓库的 `/mc players`、`/mc status` 语义，但不能复用 OneBot payload。[unverified]
3. **受控通知**：仅在管理员打开主动发言、平台权限可用、配额预算允许时，将 MC 状态聚合为主动消息；失败时记录不可投递，不无限重试。[unverified]
4. **QQ→MC 任意群消息全量镜像（条件方案）**：只有平台为该 Bot 开放 `GROUP_MESSAGE_CREATE` 全量接收能力且群主明确设置 `recv_msg_setting=all` 时才试验；上线前要做平台审核/权限确认、内容过滤、隐私告知、去重、限流和可关闭开关。此入站权限不决定 MC→QQ 出站推送许可。不能以此作为普通群的默认产品承诺。[8][11][unverified]
5. **若必须双向透明聊天**：优先评估 QQ 频道/私域等官方明确支持的场景，或让群成员显式 @bot/使用按钮触发；不以普通群“抓取全部消息”作为合规替代。订阅/主动推送能力只有在管理端许可、平台权限和实际审核均确认后才可采用，本文不把“订阅”解释成无限广播。[unverified]

## 8. 未确定项与实施前检查清单

以下事项本轮没有真实登录管理端或调用真实 API，故明确标记为**未验证**：

- 当前具体账号主体能否认证、能创建几个机器人、能否公开服务；
- 目标机器人是否获得全量群消息、Markdown/富媒体、群成员信息等逐项权限；
- 目标群群主是否开启 `all` 接收和 `allow_proactive_msg`；
- 主动消息是否触发人工审核、内容安全或账号级风控；
- 目标部署区域到 `api.bot.qq.com`/Gateway 的实际网络连通性与稳定性；
- 官方未在本轮公开页面中明确的 Webhook 事件时间窗/重复投递窗口；
- 个人主体在目标群/目标运营范围的最终上线资格。

上线前必须由运维/产品逐项留存管理端截图或平台返回证据（不得把 Secret/token 放入截图）：

- [ ] 主体认证完成，机器人简介与实际 MC 功能一致；
- [ ] 机器人发布/体验范围和目标群添加路径可用；
- [ ] 事件订阅方式明确选 Webhook 或 WebSocket，且只订阅有权限的 Intent；
- [ ] @事件收发和被动 5 分钟/5 次规则通过测试；
- [ ] 如需全量消息，取得平台能力与群主设置双重确认；
- [ ] 如需主动通知，确认 `allow_proactive_msg`、群权限、审核结果和 Bot/群/日配额；
- [ ] token 刷新、事件幂等、断线恢复、Webhook 验签和时间窗策略通过测试；
- [ ] 运营规范、隐私告知、内容过滤、删除和关闭同步流程已落实；
- [ ] 代码路径仍与 OneBot adapter 隔离，不混用协议字段或鉴权。

## 9. 本轮仓库证据

- `README.md:1-5`：项目自述为 OneBot v11/外部 QQ 实现，不等于官方 QQ Bot 合规方案。
- `README.md:97-120`：当前入站是 OneBot group message、整数 `group_id` 与 `/mc` 命令，状态来自 MC webhook 内存快照。
- `README.md:122-131`：明确外部 OneBot 负责登录/风控/事件上报，且不同 OneBot 实现的反向 WS 细节不能直接套用。
- `README.md:141-156`：当前安全边界、默认关闭能力和“不在 MVP 范围：官方机器人资质”。
- `src/mcqq_bridge/main.py:100-122`：当前 HTTP 入口验证 OneBot Bearer/HMAC-SHA1，并解析 OneBot JSON。
- `src/mcqq_bridge/onebot.py:17-48`：当前出站调用 `/send_group_msg`，使用 `group_id` 和 `status/retcode`。
- `src/mcqq_bridge/settings.py:25-34`：当前配置是 OneBot `group_id`、API URL、token 和 WS 路径，不是官方 `group_openid`。
- `src/mcqq_bridge/service.py:47-113`：当前只处理固定 OneBot group message 与 `/mc` 命令，未实现官方 QQ Bot 事件、Access Token 或 Gateway。

## Sources

[1] <https://bot.q.qq.com/wiki/develop/api-v2/>  
[2] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html>  
[3] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html>  
[4] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html>  
[5] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html>  
[6] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html>  
[7] <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html>  
[8] <https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html>  
[9] <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html>  
[10] <https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html>  
[11] <https://bot.q.qq.com/wiki/bot_FAQ/>  
[12] <https://bot.q.qq.com/wiki/business/>  
[13] <https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_bot_state.get.html>
[14] <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html>
