> **⚠️ 已过时（2026-11-11）**：本轮已实现官方 Bot adapter 与 QueQiao 多服务器运行时。
> 本文下方仍写「没有官方 Bot adapter」，仅保留为当时的设计与人工申请流程参考；
> 现行实现、配置与验证请看根目录 `README.md`、`config.example.yaml`、`docs/VERIFICATION.md`。

# 官方 QQ 群机器人接入文档（MCQQ Bridge）

> **文档状态：设计 + 人工操作指南。本仓库当前没有官方 Bot adapter，本文描述的接入能力一行都还没有实现。**
> 平台资料查阅日期：**2026-11-10（UTC）**。仓库快照：`/home/czauw/minecraft-qq-bridge`（`master` 无提交、全部文件 untracked）。
> 配套文档：`docs/QQ_BOT_CONFIGURATION.md`（配置键逐项说明）、`docs/research/official-qq-current-state.md`（as-is 实测基线，下称 [R1]）、`docs/research/official-qq-platform-and-architecture.md`（平台边界与最小迁移架构，下称 [R2]）。
> 本文**不是**联调报告：没有任何真实 QQ 账号、AppID、AppSecret 或群权限参与过验证，也没有调用过 `api.bot.qq.com`。凡未实测的内容在文中标为「未验证」。

**读者**：负责申请/配置机器人账号的运营者，以及后续实现 adapter 的开发者。
**本文不做什么**：不代你注册主体、不代你创建/发布机器人、不代你发消息、不写入任何真实凭据、不修改代码或部署。

---

## 1. 现状摘要（as-is）

判定：**当前未联通**。缺的不是业务逻辑，而是三侧的对端（QQ 侧无官方 Bot、MC 侧无事件源/消费者、桥接器未运行）。

| 项目 | 现状 | 证据 |
|---|---|---|
| 桥接器进程 | 未运行：`127.0.0.1:8000` 无监听、无 uvicorn 进程、无 systemd unit | [R1] E9 |
| 运行时配置 | 仓库根**无 `.env`**，实际生效的是代码默认值（QQ 双向关闭、群白名单为空） | [R1] E5/E18 |
| 代码能力 | FastAPI 单进程网关：MC 侧 HTTP webhook（Bearer 入站）+ OneBot v11（HTTP 出站、HTTP/反向 WS 入站） | `src/mcqq_bridge/main.py:32-39` |
| 官方协议字段 | 全仓库 `group_openid` / `member_openid` / `msg_id` / `msg_seq` / `intents` / `QQBot` / `Ed25519` 命中 **0** | [R1] E7 |
| 测试基线 | `.venv/bin/python -m pytest -q -W error` → **53 passed**（仅覆盖 OneBot/本地模拟 peer） | [R1] E1，本轮复跑一致 |
| MC 侧 | 真实在跑的 Forge `1.20.1-47.4.16` 与 `server.jar` 实例（25565/25567）+ MCSManager 面板；但**没有任何组件**向本仓库推事件或消费 callback | [R1] E12–E15 |
| QQ 侧 | 无 OneBot 实现、无官方 Bot 凭据 | [R1] U3/U7 |
| 依赖 | 无 `cryptography` / `pynacl`；Webhook 的 Ed25519 验签需要新增依赖 | [R1] E10 |

由此得出本文的措辞纪律：**「官方文档支持」≠「你的应用已获权限」≠「本项目已实现」≠「联调通过」**。四者必须分开写。

---

## 2. 为什么选官方 Bot，以及它的能力上限

### 2.1 与 OneBot 个人账号路线的对比

| 维度 | 官方 QQ Bot（本文方案） | OneBot v11 + 个人账号（现状实现） |
|---|---|---|
| 登录主体 | 开放平台应用（AppID）+ AccessToken | 第三方实现登录个人 QQ 账号 |
| 稳定性/风控 | 走官方 OpenAPI，无个人号风控；但受平台内容审核与权限开关约束 | 依赖第三方协议实现，存在账号被风控/封禁风险 |
| 协议稳定性 | 官方文档化的 v2 接口与事件（[S3]） | 社区协议，实现之间字段/方向不一致 |
| 能力上限 | 群 @ 消息、全量群消息（需授权）、主动群消息（需授权+频控）、富媒体（需上传） | 可向所在群发任意消息、可收全量群消息（无平台授权流程） |
| 部署依赖 | WebSocket 出站即可；Webhook 需公网 HTTPS | 需另一个 OneBot 实现进程/容器 |
| 合规与责任 | 需主体认证、遵守运营规范；机器人被封禁**不支持解封**（[S2]） | 无平台侧合规路径，风险转嫁到账号 |

结论：**官方路线是面向 QQ 群长期运行的合规接入路径**（OneBot 个人号路线无法提供平台侧合规与稳定性保证）；OneBot 路线保留为「运维显式选择」的回滚手段（见 §7 阶段 4），两者**不得同时出站**，否则同一事件会被双发（[R2] §11）。

### 2.2 官方能力的硬限制（必须写进产品预期）

| 限制 | 官方原文/数值 | 来源 | 对本项目的影响 |
|---|---|---|---|
| 被动回复窗口 | 群消息「被动消息有效时间 **5 分钟**，每个消息最多回复 **5 次**」 | [S15] | `/mc status` 必须秒回；慢查询（如 Hermes 长任务）会过期 |
| 回复序号 | `msg_seq` 与 `msg_id` 联合使用，「相同的 `msg_id` + `msg_seq` 重复发送会失败」，不填默认 1 | [S15] | 同一消息多次回复必须递增序号，且不能靠改序号绕过 5 次上限 |
| 主动消息配额 | Bot 维度：企业认证/个人身份证认证 **60/qpm**，未认证 **30/qpm**；单关系 **20/qpm**；**每群 1 天最多 1000 条** | [S15] | MC 聊天全量镜像**不可行**；通知必须合并/摘要/限额 |
| 主动消息授权 | 用户/群可关闭「允许主动发送」，关闭后主动消息发送失败 | [S16] | 群主关开关即失能，不能承诺「永远能推送」 |
| 全量群消息 | `GROUP_MESSAGE_CREATE` 仅在「机器人开启了『接收所有消息』功能后」推送；该开关由**群主**在添加机器人时（或之后）通过「设置权限」控制 | [S14] [S2] | 默认只能收 @；全量接收是需申请的增量能力 |
| 接口频率 | 发送群消息接口 **100 QPS** | [S15] | 该值不覆盖更严格的主动消息配额，不能拿来承诺吞吐 |
| 身份标识 | 「不同的 `bot(AppID)` 获取到的用户 `openid`，群 `openid`，频道 `openid` 均不相同」 | [S6] | 无数字群号；白名单必须按 AppID 作用域的 openid 维护 |
| 事件回复限制 | `event_id` 仅支持 `INTERACTION_CREATE`、`GROUP_ADD_ROBOT`、`GROUP_MSG_RECEIVE` | [S15] | MC 上线/死亡等事件**不能**伪造 `event_id` 做被动回复 |
| 内容政策 | 「不允许发送 URL」`40054010`、「消息内容违规」`40034006` | [S15] | 玩家聊天里的链接可能导致整条发送失败，需要过滤与降级 |
| 富媒体 | `msg_type=7` 需先上传拿 `file_info`，且单聊/群聊上传接口不互通 | [S16] | 首期只做纯文本（与现状一致） |
| 主体资格 | 未认证：仅能添加自己是群主/管理员的群；个人认证：**暂不支持公开使用**、进群上限 500；企业认证：可设公开服务范围 | [S1] [S2] | 自管群可用；公开发布需企业主体，且不承诺审核通过 |

**明确不写的承诺**：不存在「保证主动通知无限发送」的配置或步骤。任何主动通知都同时受群主开关、Bot 配额（60/30 qpm）、单群 20 qpm、每群每天 1000 条、内容审核五重约束；生产上只能按低于上限的运维值工作（见配置文档 §7）。

---

## 3. 资格、应用、群权限、测试与发布（人工步骤）

> 以下每一步都由**运营者本人**在 QQ 开放平台/手机 QQ 上完成。本文只描述动作与判据，不提供代操作、代申请、代发布、代发消息的任何自动化。
> 平台规则随时可能调整；执行前请以管理端当场显示为准，并把结果记录到你自己的非敏感清单。

### 3.1 主体与账号

1. 用 QQ 账号或邮箱在 QQ 开放平台注册（[S1] §1、[S17]）。
2. 完成主体认证：个人主体或企业（含个体户）主体。**个人主体无法直接升级为企业主体**，需要新邮箱重新注册认证（[S2]）。
3. 公开服务还要求管理员完成 QQ 钱包实名；新注册账号审核约 3 天（[S2]「打开公开服务按钮失败」条目）。**这是平台提示，不是承诺的时限**。
4. 记录（非敏感）：主体类型、认证状态、超级管理员账号归属。

### 3.2 创建机器人应用

5. 在机器人管理端「创建机器人」，填写头像/昵称/简介；原创机器人可在「设置 AI 服务」处选择「稍后连接」跳过（[S1]）。
6. 名额上限：个人主体 5 个、企业主体 10 个（同一企业主体所有账号共用，上限 50）（[S1] [S2]）。
7. 在「开发设置」取得 `AppID` 与 `AppSecret`（[S4]）。`AppSecret` 是密钥，**只保存在服务端**，不进群、不进仓库、不进截图。
8. 确认服务范围包含 **QQ 群**（不是频道）。本项目只使用群接口 `/v2/groups/...` 与 `GROUP_AND_C2C_EVENT` 事件，不使用 `/channels/...`。

### 3.3 开发设置（连接方式、白名单、体验号）

9. 选择连接方式：WebSocket 或 Webhook（管理端可切换，**切换后立刻生效**，需要维护窗口）（[S1] §8.1）。
   - `WebSocket`：官方描述「不依赖公网服务器部署，适合个人单机使用，需维护长连接」——本项目首期推荐。
   - `Webhook`：「事件推送至你配置的 HTTPS 服务上，适合提供公开服务的机器人使用」——需要公网 HTTPS（见 §4.4）。
10. 服务器 IP 白名单：仅支持**单个 IPv4**，不支持 CIDR，最多 50 条；**未设置时所有来源 IP 都会放行**（[S1] §8.2）。填的是**实际 API 出口 IP**，不是内网地址。
11. 内部体验号码（可选）：最多 20 个，配置后不受服务范围限制，可用于内测对话（[S1] §8.3）。

### 3.4 让机器人进测试群

12. 用**最新版手机 QQ** 把机器人加入测试群（未认证主体只能加入自己是群主/管理员身份的群；个人认证进群上限 500）（[S1] [S2]）。
13. 若提示「机器人暂不支持加入」，按 FAQ 指引升级手机客户端后重试扫码（[S2]）。

### 3.5 群主授权（决定能力边界，不是可选项）

14. 由**群主**在手机 QQ 添加机器人时点「设置权限」，按需开启（[S2]）：
    - 「接收全量消息」→ 才会收到 `GROUP_MESSAGE_CREATE`；
    - 「主动推送消息」→ 才允许非回复型群消息（MC 通知依赖此项）；
    - 「撤回成员消息」→ 仅在需要时开启，本项目首期不使用。
15. 群主关闭上述开关后，能力立即消失。**本项目不得把「能发」当作默认事实**，必须在每次上线/变更后复核开关状态。

### 3.6 测试

16. 「管理员账号可直接测试使用，无需沙箱环境」，旧版管理端的沙箱群/频道配置入口已不可用（[S2]）。不要照抄旧教程找沙箱入口。
17. 测试阶段建议只开一个群、只开 @ 查询；主动推送与全量接收逐项开启、逐项验证（见 §8 验收矩阵）。

### 3.7 发布与公开服务

18. 服务范围可在管理端切换（[S1] 称「灵活切换免审核」），但**这不等于免除主体资质、资料规范与运营责任**；个人主体目前不支持公开使用（[S1] [S2]）。
19. 企业公开服务的审批路径、时长、特殊权限结果本仓库**未核实**，本文不给天数、不给通过率。

### 3.8 需要留档的非敏感清单（建议模板）

| 项目 | 填写示例（占位） | 敏感性 |
|---|---|---|
| 主体类型 / 认证状态 | `<个人认证 / 企业认证 / 未认证>` | 低（不含证件信息） |
| AppID | `<APP_ID>` | 中（可公开程度自行判断，勿当秘密传播） |
| 连接方式 | `<websocket / webhook>` | 低 |
| 测试群 `group_openid` | `<GROUP_OPENID_PLACEHOLDER>` | 中（Bot 作用域标识） |
| 群主授权：全量消息 / 主动推送 | `<是 / 否> / <是 / 否>`，含核验日期 | 低 |
| 出口 IP（已加入白名单） | `<PUBLIC_EGRESS_IPV4>` | 低 |
| AppSecret / Bot Secret | **不记录在此清单**，只登记「已注入到哪个 secret 存储」 | 高 |

---

## 4. 接收方式与鉴权（官方证据）

### 4.1 选型建议

| 方案 | 官方支持 | 适用 | 本项目取舍 |
|---|---|---|---|
| **WebSocket（Gateway）** | 官方在接入指南列为可切换连接方式（[S1] §8.1），并有完整网关协议（[S9]） | 单机/内网部署，不想暴露公网入口 | **首期采用**：桥接器作为客户端外出连 `wss`，本机监听保持 `127.0.0.1` |
| HTTP Webhook | 官方支持（[S8]），需 HTTPS 且回调端口限 80/443/8080/8443 | 公开服务、需要无状态水平扩展 | 备选：仅当确有公网 HTTPS 入口时启用；不与 WS 同时消费同一事件 |

两者事件体相同（`payload` 结构一致，[S7]），差别只在传输与鉴权。管理端切换**立刻生效**，切换时必须考虑重复/漏事件窗口（[S1]）。

### 4.2 凭证（access_token）

- 接口：`POST https://api.bot.qq.com/app/getAppAccessToken`，JSON 体 `{"appId": "...", "clientSecret": "..."}`（[S5]）。
- 返回：`access_token`、`expires_in`；「目前是 7200 秒之内的值」（[S5]）。
- **失败也用 HTTP 200 返回**：官方明示「即使调用失败，HTTP 返回码仍为 `200`」，必须检查业务 `code`（`100001` 过频、`100007` appid 无效/机器人状态异常、`100016` appid 或 secret 不正确、`10004` 机器人不存在）（[S5]）。
- 刷新语义：有效期内重复获取返回同一个 token；「在上一个 `access_token` 接近过期时间 `60` 秒内」可拿到新 token，旧 token 在这 60 秒内仍有效（[S5]）。
- 使用方式：请求头 `Authorization: QQBot {ACCESS_TOKEN}`（[S5] [S6]）。**不是 `Bearer`**，也不接受 OneBot 的 token。
- 文档内部小矛盾（已记录，不擅自统一）：返回参数表写 `expires_in` 为 `number`，示例 JSON 是字符串 `"7200"`（[S5]）。实现必须同时接受数字与纯数字字符串。

### 4.3 WebSocket 流程（首期方案）

1. `GET /gateway` 获取接入点，返回形如 `{"url": "wss://api.bot.qq.com/websocket/"}`（[S11]）；需要分片信息时用 `GET /gateway/bot`，额外返回 `shards` 与 `session_start_limit{total, remaining, reset_after, max_concurrency}`（[S9] [S12]）。
2. 建连后收到 `op=10` Hello，`d.heartbeat_interval` 为毫秒（示例 45000）（[S9]）。
3. 发送 `op=2` Identify：`d.token` 格式为 `"QQBot {AccessToken}"`，`d.intents` 为位掩码，`d.shard` 示例 `[0,1]`（不分片）（[S9]）。
4. 鉴权成功收到 `op=0 t=READY`，其中 `d.session_id` 必须保存（[S9]）。
5. 周期发送 `op=1` 心跳，`d` 为最近一次收到的 `s`（首次为 null），收到 `op=11` Heartbeat ACK（[S9]）。
6. 断线重连后发 `op=6` Resume：`session_id` + 最后处理的 `seq`（即事件的 `s`），成功后补发遗漏事件并收到 `RESUMED`（[S9]）。
7. 连接数不得超过 `/gateway/bot` 返回的 `remaining`（[S9]）。
8. 关闭码必须按表处理（[S9]）：`4006/4007` 需重新 Identify；`4008/4009` 可 Resume；`4013` 无效 intent、`4014` **intent 无权限**（不可重试，需改订阅或申请权限）；`4914` 仅允许沙箱环境、`4915` 机器人已封禁（不可重连）。

**intents 限制**：群 @ 消息与全量群消息都属 `GROUP_AND_C2C_EVENT (1 << 25)`（[S13] [S14] [S7]）；订阅当前应用无权限的 intent 会连接被关（[S9] `4014`）。首期只订阅业务必需项。

### 4.4 HTTP Webhook 流程（备选）

1. 管理端配置 HTTPS 回调地址；**允许端口仅 80、443、8080、8443**（[S8]）。这是公网 URL 的端口，不是反代后端监听端口。
2. 地址验证（`op=13`）：请求头带 `X-Bot-Appid`、`User-Agent: QQBot-Callback`，体为 `{"d":{"plain_token":"...","event_ts":"..."},"op":13}`；服务端用 Bot Secret 派生种子签名后返回 `{"plain_token": "...", "signature": "<hex>"}`（[S8]）。
3. 签名种子算法（官方 Go 示例）：`seed = botSecret`，`for len(seed) < 32 { seed = repeat(seed, 2) }`，取前 32 字节；`ed25519.GenerateKey(strings.NewReader(seed))`；对 `event_ts + plain_token` 签名并 hex 编码（[S8]）。业务事件同理：签名体为 **`X-Signature-Timestamp` 字符串 + 原始请求体字节**（[S10]）。
4. 每个回调请求都要验签：`X-Signature-Ed25519`（hex，64 字节）、`X-Signature-Timestamp`；官方示例还校验 `sig[63]&224 == 0`（[S10]）。**必须先取原始 body 再验签**，不能先 JSON 反序列化再重排。
5. 回包：纯 ACK 用 `op=12`（HTTP Callback ACK），表示「机器人收到了平台推送的数据」（[S7]）。
6. 去重是**应用责任**：签名只证明来源与完整性，不阻止重投。官方本次页面**未给出**重试次数、重试间隔、ACK 截止时间与强制时间戳容差，这三项在配置文档中标注为待实测，不编造数值。
7. 反代要求：保留原始请求体、转发 `X-Signature-*` 与 `X-Bot-Appid` 头、限制 body 大小、只暴露回调路径，不把健康检查/MC webhook/管理接口一并公开。

### 4.5 事件与身份字段

`payload` 统一结构 `{id, op, d, s, t}`：`id` 事件 id、`s` 下行序列号（心跳/Resume 用）、`t` 事件类型、`d` 事件体（[S7]）。

`GROUP_AT_MESSAGE_CREATE`（群 @ 机器人）关键字段（[S13]）：

| 字段 | 含义 | 本项目的用途 |
|---|---|---|
| `d.id` | 消息 ID，可用于被动回复和撤回 | 作为 `msg_id` 传给发送接口 |
| `d.author.member_openid` | 群成员 OpenID（群聊场景） | 授权主体键；**不是 QQ 号** |
| `d.author.member_role` | `member` / `admin` / `owner` | 可作提示，**不等于**本项目管理员授权 |
| `d.content` | 文本内容，「已自动去除@机器人的前缀」 | 命令解析入口 |
| `d.group_openid` | 群 OpenID | 群白名单键；**不是数字群号** |
| `d.timestamp` | RFC3339 时间 | 被动回复窗口参考 |
| `d.message_scene.ext` | `msg_idx` / `ref_msg_idx` / `auth_token` 等 `key=value` | 只允许取 `msg_idx`，**`auth_token` 属凭据，禁止落日志** |
| `d.attachments[].url` | 附件下载地址（含临时鉴权参数） | 首期不下载、不转存、不记录 |

补充事实：
- 官方提示「为确保消息可达，相同 `msg_id` 可能重复推送」（[S13]）—— 收到重复事件必须幂等。注意官方原文建议「结合 `msg_seq` 做去重」，而 `msg_seq` 在发送接口里是**出站序号**（[S15]）；该措辞存在歧义，本项目按 `(AppID, group_openid, msg_id)` 去重，这一点列为待确认（§10）。
- 全量模式 `GROUP_MESSAGE_CREATE` 字段与 @ 消息一致，但 @ 某人会被替换为 `<@member_openid>`、@全体为 `<@all>`（[S14]）；文本预处理必须容忍这种替换，不要把它当命令参数。

### 4.6 出站消息语义

`POST /v2/groups/{group_openid}/messages`（[S15]）：

| 字段 | 用法 |
|---|---|
| `msg_type` | 首期固定 `0`（纯文本），内容放 `content` |
| `msg_id` | 被动回复：取事件的 `d.id`，5 分钟内有效 |
| `msg_seq` | 与 `msg_id` 联合使用，默认 1；相同 `msg_id` + `msg_seq` 重复发送会失败 |
| `event_id` | 与 `msg_id` **二选一**，且只支持 `INTERACTION_CREATE` / `GROUP_ADD_ROBOT` / `GROUP_MSG_RECEIVE` |
| 主动消息 | 不传 `msg_id`/`event_id`，仅当群主开启主动推送且配额可用时成立 |
| `message_reference` | 引用回复，首期不用 |

响应成功返回 `id`、`timestamp`、`ext_info.ref_idx`；失败返回 `err_code` 与 `trace_id`，**不要用 `message` 文案判错**（[S6] [S15]）。

### 4.7 与现有代码的接口形状差异

| 维度 | 现状（OneBot） | 官方 v2 | 差异性质 |
|---|---|---|---|
| 鉴权头 | `Authorization: Bearer <token>`（`onebot.py:31-32`） | `Authorization: QQBot <access_token>`（[S5]） | 不可复用 |
| 凭据 | 运维手填静态 token（`settings.py:29`） | AppID+Secret 换短期 token（[S5]） | 需 token manager |
| 群标识 | `group_id: int`（`settings.py:25`） | `group_openid: str`（[S6]） | 类型与语义都变 |
| 用户标识 | `user_id: int`（`service.py:54`） | `author.member_openid: str`（[S13]） | 不可 `int()` |
| 入站形状 | `post_type/message_type/group_id/message`（`service.py:51-68`） | `payload{id,op,s,t,d}`（[S7]） | 需规范化层 |
| 入站验签 | HMAC-SHA1 `X-Signature: sha1=<40hex>`（`main.py:109-117`） | Ed25519（[S10]） | 算法与依赖都要换 |
| 传输方向 | 本服务是 WS **服务端**（`main.py:124`） | 本服务是 WS **客户端**（[S9]） | 反向 |
| 出站语义 | 无 `msg_id`，`send_group_msg`（`onebot.py:34-38`） | `msg_id`+`msg_seq` 被动回复（[S15]） | 语义新增 |
| 投递结果 | `bool`（`ports.py:6-7`） | `err_code`/`trace_id` 分类（[S6]） | 需要结构化结果 |

---

## 5. 推荐架构

### 5.1 拓扑

```mermaid
flowchart LR
  MC[MC 侧事件源<br/>当前缺失] -->|私网 HTTP + Bearer| CORE[现有核心<br/>state/service/命令 allowlist]
  QQ[QQ 群] -->|桥接器主动连 WSS 收事件| IN[新增 qq_official_events<br/>归一化/去重/openid 白名单]
  IN --> CORE
  CORE -->|受控 say callback| MCOUT[MC 侧消费者<br/>当前缺失]
  CORE --> POLICY[新增 delivery<br/>回复上下文/主动权限/限流队列]
  POLICY -->|HTTPS + QQBot access_token| API[api.bot.qq.com<br/>/v2/groups/{group_openid}/messages]
  API --> QQ
```

### 5.2 组件职责

| 组件 | 状态 | 职责 | 备注 |
|---|---|---|---|
| MC 事件入口 `POST /api/v1/minecraft/events/*` | 保留 | Bearer 鉴权、Pydantic 校验、64 KiB 上限 | `main.py:60-98` |
| 状态缓存 `state.py` | 保留 | 快照 + TTL + unknown/fresh/stale | 与协议无关 |
| 命令处理骨架 | 保留并抽层 | `/mc` 前缀、`players/status/say` 分派 | `service.py:69-95` |
| OneBot adapter | 保留为回滚 | 出站 `send_group_msg`、反向 WS | 与官方 provider 互斥 |
| `qq_official.py`（建议新增） | **未实现** | token manager + REST 发送 | 名称仅建议 |
| `qq_official_events.py`（建议新增） | **未实现** | Gateway 客户端（或 Webhook 路由）、事件去重 | 名称仅建议 |
| `delivery.py`（建议新增） | **未实现** | 被动/主动模式、`msg_seq`、限流、队列、账本 | 名称仅建议 |
| Hermes 管理通道 | 首期**关闭** | `/mc hermes` 官方路由直接拒绝 | 整数 actor 契约未重构前不得启用 |

### 5.3 暴露面

| 入口 | 现状 | 目标 |
|---|---|---|
| 桥接器 HTTP | compose 仅 `127.0.0.1:8000`（`docker-compose.yml`） | 不变 |
| 官方 WS | — | 出站 `wss://api.bot.qq.com/...` 443 |
| Webhook | — | 仅 `/api/v1/qq-official/events`，公网 443/8443 反代；其余路径不对外 |
| OneBot WS/HTTP | `ONEBOT_WS_PATH`、`/api/v1/onebot/events` | 与官方 provider 互斥启用，不叠加 |
| 状态查询接口 | 无鉴权（`main.py:167-177`） | 维持仅内网；如需对外必须另加鉴权 |

### 5.4 两条数据流

- **QQ → MC 查询**：群内 @ `/mc status` → `GROUP_AT_MESSAGE_CREATE` → 验签/去重/群 openid 白名单/bot 过滤 → 读内存快照 → 用该事件 `d.id` + 递增 `msg_seq` 被动回复，回复文本必须标注「缓存/过期/未知」。
- **MC → QQ 通知**：MC webhook → Bearer 校验 → 更新状态 → 检查该事件开关与群路由 → 检查主动权限与额度 → 合并进队列 → 走主动发送（**不带 `msg_id`**）→ 记录 `err_code`/`trace_id`。失败时保留最新状态供下次查询，不把失败说成「已通知」。

### 5.5 身份映射键

- 群：`(provider, app_id, group_openid)`
- 成员：`(provider, app_id, group_openid, member_openid)`
- 保留原始字符串，**不转整数、不大小写归一、不截断**；日志脱敏。
- 首次见到的群**不得自动加入白名单**，必须由运维核对后写入（采集与批准流程见配置文档 §5）。

---

## 6. 迁移差距

| # | 差距 | 现状证据 | 目标 | 风险 |
|---|---|---|---|---|
| G1 | 配置模型只有 OneBot 与 `list[int]` 群号 | `settings.py:25-34`、`68-73` | provider 单选 + 字符串 openid 列表 | 高：直接搬旧群号会静默失效 |
| G2 | 身份是整数 | `service.py:122-130` `_int()` | 字符串 OpenID 校验 | 高 |
| G3 | 无 `msg_id`/`msg_seq` | 全仓库命中 0 | 回复上下文对象 | 高：不实现就只能发主动消息 |
| G4 | WS 方向相反 | `main.py:124`、`onebot.py:51-136` | 出站 Gateway 客户端 | 中高 |
| G5 | 验签算法与依赖 | `main.py:109-117`；无 `cryptography` | Ed25519（`cryptography` 或等价实现） | 中高 |
| G6 | 出站结果被丢弃 | `service.py:31-34` 未用返回值；MC 入口恒 202（`main.py:98`） | `SendResult(status, error_code, trace_id)` | 中：可观测性 |
| G7 | 无去重/无持久化 | `state.py` 纯内存 | 事件去重键 + 发送账本 | 中 |
| G8 | 测试与 smoke 绑死 OneBot | `scripts/smoke.py`、tests 63 处命中 | 官方协议 fixture + 新 smoke 向量 | 中：绿灯≠可用 |
| G9 | `hermes` 绕过命令白名单 | `service.py:74-77`（在 `mc_commands` 检查之前） | 官方路由首期硬拒绝 | 中 |
| G10 | 死配置误导 | `host/port/log_level/app_name`、`llm_*` 在 src 零引用 | 文档标注，避免误以为可配 | 低 |

复用面（不改协议即可用）：`state.py`、`mc_adapter.py`、`models.py` 的 MC 六模型、`service.py` 的编排与格式化、`security.py` 的 `bearer_matches`（仅 MC/callback 入口）、`Dockerfile` 的非 root/只读/cap_drop 硬化。

替换面（必须新写，不在原文件加分支）：`onebot.py` 的官方等价物、`main.py` 的 OneBot 三条路由、`settings.py` 的 OneBot 段、`ports.py` 出站契约、`scripts/smoke.py`。

---

## 7. 分阶段工作

> 不承诺工期。每阶段的退出条件是**可检查的证据**，不是「写完了」。

### 阶段 0：接入前核实（人工，未执行）

- 产出：§3.8 清单 + 三项能力结论（@ 消息 / 全量消息 / 主动推送）各自「已开通 / 未开通 / 未验证」。
- 退出条件：AppID 已取得、测试群 `group_openid` 已通过真实事件采集到（不是猜的）、群主开关状态有截图或书面确认（截图本身不放仓库）。
- 红线：不安装任何未经确认 loader/版本的 MC mod；不改 MC 服务端。

### 阶段 1：协议适配层 + 离线测试（未实现）

- 产出：token manager（含 HTTP 200 + `code` 判定、TTL 兼容解析、单飞刷新）、Gateway 客户端（Hello/Identify/Heartbeat/Resume/关闭码）、发送客户端（`msg_seq` 分配、`err_code` 分类）、事件规范化、去重表、`SendResult`。
- 测试：`pytest` fixture 覆盖 —— 401/token 业务错误、`expires_in` 字符串与数字、回复过期 `304103`/`40034005`、次数超限 `40034128`、去重 `40054005`、无主动权限 `40034105`、禁言 `40054002`、非群成员 `40054003`、限频 `429`/`40034100`、WS 关闭码 `4006/4007/4009/4014`。
- 退出条件：离线测试全绿 + 现有 53 项测试不回归。
- 红线：**mock 通过不等于平台可用**；不得因此宣称接入完成。

### 阶段 2：真实通道最小验证（未执行）

- 范围：一个群、仅 @ 查询（`/mc players`、`/mc status`），主动通知关闭，`/mc say` 关闭，Hermes 关闭。
- 退出条件：§8 矩阵中 A1–A4、B1–B3 全部拿到证据。
- 红线：只靠「WebSocket 已连接」不得通过本阶段。

### 阶段 3：主动通知与全量接收逐项开启（未执行）

- 顺序：join/leave 聚合通知 → death → chat（最后；先测量真实配额再决定是否只发摘要）→ 全量消息（需群主授权）。
- 退出条件：§8 中 C 组与 D 组逐项通过；每日账本不超预算；权限撤销后自动停发。
- 红线：不得把被动过期消息改写成主动消息；不得用「重试」绕过配额。

### 阶段 4：灰度、回滚与运维（未执行）

- provider 单选：`onebot | qq_official`，禁止同进程双出站。
- 回滚：关闭队列 → 关 provider → 恢复上一版本与 `.env` 备份 → 轮换 secret（若怀疑泄漏）→ 记录时间线。
- 退出条件：一次完整的「停队列 → 回滚 → 恢复」演练，且期间无重复发送。

---

## 8. 真实联调验收矩阵

判定原则：**每条都要有可复核的三方证据**——群内可见的实际消息 + 官方业务成功响应（`id`/`timestamp` 或 `err_code=0`）+ 本地日志（含 `trace_id`/`X-Tps-trace-ID`）。缺一项即不通过。
当前状态列全部为 **⛔ 未执行**（无 AppID、无群权限、adapter 未实现）。

| ID | 场景 | 前置 | 必须观察到的证据 | 当前状态 |
|---|---|---|---|---|
| A1 | Gateway 连接 | AppID/Secret 已注入 | 收到 Hello；Identify 后 READY 且记录 session_id；心跳有 ACK | ⛔ |
| A2 | 被动回复 `/mc players` | 群 allowlist 已含该 `group_openid` | 群内出现带缓存状态前缀的回复；响应含消息 `id` | ⛔ |
| A3 | 被动回复 `/mc status` | 同上 | 回复含「在线/离线/未知」与版本、人数 | ⛔ |
| A4 | 状态真实性 | MC 侧推送过 players/status | 回复中的 freshness 与 `/api/v1/state/meta` 一致（unknown/fresh/stale 不伪造） | ⛔ |
| B1 | 未授权群 | 另一个未加入 allowlist 的群 | 无任何回复；本地日志记录拒绝原因且不含凭据 | ⛔ |
| B2 | 非 `/mc` 消息 | — | 不回复、不执行（含全量模式下） | ⛔ |
| B3 | 伪造管理员文本 | 普通成员发送 `/mc hermes ...` | 首期必须拒绝，且不触发任何上游调用 | ⛔ |
| C1 | MC 上线/下线通知 | 群主已开主动推送 | 群内收到 join/leave 消息；官方响应成功；配额账本 +1 | ⛔ |
| C2 | MC 死亡通知 | 同上 | 同上，且不与 C1 合并错乱 | ⛔ |
| C3 | MC 聊天 → 群 | 主动开关 + `mc_notifications.chat=true` | 群内可见 `[MC] 玩家: 内容`；长文本被截断而非报错 | ⛔ |
| C4 | 主动额度耗尽 | 人为压低本地预算 | 队列停止发送并计数；不忙循环；不谎报 delivered | ⛔ |
| C5 | 群主关闭主动推送 | 群主关闭开关 | 发送失败（`40034105` 类）；停止重试；状态页说明原因 | ⛔ |
| D1 | 全量群消息 | 群主开启「接收全量消息」 | 未 @ 的消息到达 `GROUP_MESSAGE_CREATE`；仍不自动执行普通文本 | ⛔ |
| D2 | 全量模式 @ 替换 | 同上 | `<@member_openid>` / `<@all>` 被正确剥离，不污染命令参数 | ⛔ |
| E1 | 重复事件 | 平台重投同一 `msg_id` | 只产生一次副作用；重复请求仍被 ACK | ⛔ |
| E2 | 回环 | MC 侧回显自己的消息 | 不产生二次转发风暴；bot 消息被过滤 | ⛔ |
| E3 | 被动回复超时 | 延迟 > 5 分钟再回复 | 返回 `304103`/`40034005` 之一；不自动转主动 | ⛔ |
| E4 | 回复次数上限 | 同一 `msg_id` 回复 6 次 | 第 6 次失败（`40034128` 被动回复时间或次数超限）；不递增序号绕过 | ⛔ |
| F1 | WS 断线恢复 | 主动断网/重启 | Resume 成功补发；`4009` 后重连；命令不重复执行 | ⛔ |
| F2 | token 过期 | 等待 TTL 到期 | 自动换新 token；旧 token 60 秒内仍可用；无并发刷新风暴 | ⛔ |
| F3 | 桥接器重启 | 重启进程 | 内存快照被标 `unknown`；不伪造历史在线状态 | ⛔ |
| G1 | Webhook 地址验证（备选） | 公网 HTTPS + 允许端口 | `op=13` 返回正确 `signature`，官方显示验证通过 | ⛔ |
| G2 | Webhook 验签（备选） | 同上 | 正确签名通过；篡改 body/错误 AppID/过期时间戳被拒 | ⛔ |
| G3 | 反代保真（备选） | 反代已配置 | 原始 body 未被重排；`X-Signature-*` 头完整透传 | ⛔ |
| H1 | 内容政策 | 玩家发送含 URL 的聊天 | 发送被拒（`40054010`）时按策略丢弃/改写并审计，不刷重试 | ⛔ |

---

## 9. 安全与运营边界

1. **凭据隔离**：`AppSecret` / `Bot Secret` 绝不进入仓库、日志、群消息、截图；不复用 OneBot 的 `ONEBOT_ACCESS_TOKEN`、`ONEBOT_WEBHOOK_TOKEN`、`ONEBOT_WEBHOOK_SECRET` 作为官方凭据（接口与语义都不同）。
2. **日志脱敏**：不记录 `access_token`、`message_scene.ext` 中的 `auth_token`、附件下载 URL、原始 `AppSecret`；错误只记录 `err_code`、`trace_id`。
3. **不可信输入**：群消息是攻击者可控文本。命令只认完整 `/mc` 首 token（现状已实现 `service.py:70`）；不解析引用/转发/附件里的指令；不把消息拼进 shell/RCON/控制台。
4. **授权分层**：群 `member_role=owner/admin` **不等于**本项目管理员；Hermes 通道首期关闭，且不得把 QQ 身份直接映射成终端执行权限。
5. **不做无限通知承诺**：所有主动通知受 §2.2 五重约束；产品文案不得出现「实时同步全部聊天」。
6. **封禁风险**：机器人被封禁后平台「暂不支持解封」（[S2]），因此内容过滤、频率控制、灰度发布属于必需项，不是优化项。
7. **最小订阅**：只订阅当前应用确有权限的 intents；`4014` 说明订阅越权，应改配置而不是重连死循环。

---

## 10. 未核实 / 待确认

| # | 事项 | 状态 | 影响 |
|---|---|---|---|
| U1 | 本应用的主体资质、功能开关、群权限实际状态 | 未登录核实 | 决定 A/C/D 组能否开始 |
| U2 | `Bot Secret`（签名用）与 `clientSecret`（换 token 用）是否同一值 | **未确认**：[S10] 称「开发者平台的 Bot Secret」，[S5] 称 `clientSecret`，两页未互相引用 | 决定配置项是否可合并；实现前必须在管理端核对 |
| U3 | Webhook 重投次数/间隔、ACK 时限、强制时间戳容差 | 官方页面未给出 | 去重保留期、超时阈值只能实测确定 |
| U4 | 各 `event_id` 的具体有效期 | 未核实 | 仅影响非首期能力 |
| U5 | `group_openid`/`member_openid` 在机器人重建后是否变化 | 未核实 | 决定白名单是否需要重新采集 |
| U6 | 数字群号 ↔ openid 的转换 | 平台未提供，本项目不依赖 | 禁止在配置里混用 |
| U7 | 本机公网入口（Caddy :8443 / frp）能否满足 Webhook 端口与 TLS 要求 | 未测试 | 决定 Webhook 方案是否可行 |
| U8 | 全量消息与主动推送在本应用的实际可用性 | 未验证（文档存在 ≠ 已开通） | 决定 C/D 组验收能否进行 |
| U9 | 「结合 msg_seq 去重」的官方措辞含义 | 存疑（`msg_seq` 是出站序号） | 去重键设计 |
| U10 | WS SDK/客户端选型（`botpy` 或自研） | 未决定 | 影响阶段 1 工作量，不影响协议事实 |

---

## 11. 参考来源（查阅日期 2026-11-10 UTC）

> 链接可用性于 2026-11-10 04:08 UTC 用 `curl` 实测（详见配置文档 §10）。官方文档可能改版；引用以当日正文为准。

| 编号 | 标题 | URL | 本文用途 |
|---|---|---|---|
| S1 | QQ Bot 介绍与接入指南 | https://bot.q.qq.com/wiki/ | 主体/名额/服务范围/IP 白名单/体验号/连接方式 |
| S2 | QQ Bot 常见问题答疑 | https://bot.q.qq.com/wiki/bot_FAQ/ | 沙箱、群主授权、个人不可公开、封禁不解封 |
| S3 | 开发文档 v2 索引 | https://bot.q.qq.com/wiki/develop/api-v2/ | 页面结构 |
| S4 | 启动接入 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html | AppID/AppSecret、旧 Token 鉴权废弃 |
| S5 | 获取访问凭证 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html | token 接口、HTTP 200 语义、TTL、`QQBot` 头 |
| S6 | API 调用指南 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html | 基址、错误结构、`trace_id`、openid 命名空间 |
| S7 | 通用数据结构 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html | payload、opcode、intents |
| S8 | Webhook 方式 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html | 端口限制、op13 地址验证、种子派生 |
| S9 | WebSocket 方式 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html | Hello/Identify/心跳/Resume/关闭码 |
| S10 | 安全和授权（签名） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html | Ed25519 验签细节 |
| S11 | 获取通用 WSS 接入点 | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html | `GET /gateway` |
| S12 | 获取带分片 WSS 接入点 | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/shard_url_get.html | `GET /gateway/bot`、并发限制 |
| S13 | 群@机器人消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html | 事件字段、intent、重复推送提示 |
| S14 | 群消息（全量模式） | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html | 「接收所有消息」前提、@ 替换规则 |
| S15 | 发送群聊消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html | 被动 5 分钟/5 次、配额、错误码 |
| S16 | 消息收发概述 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html | 主动/被动定义、消息类型 |
| S17 | QQ 开放平台 | https://q.qq.com/ | 管理端入口（需登录） |
| S18 | 管理端回调设置 | https://q.qq.com/qqbot/#/developer/webhook-setting | Webhook 地址/事件订阅入口（需登录） |

**旧链接修正**：`https://bot.q.qq.com/wiki/bot_FAQ`（无尾斜杠）实测返回 **301**，跳转链为 `http://bot.q.qq.com/bot_FAQ/` → `https://bot.q.qq.com/bot_FAQ/` → `https://q.qq.com`，**拿不到 FAQ 正文**；可用地址是带尾斜杠的 `https://bot.q.qq.com/wiki/bot_FAQ/`（200）。引用时请用后者。

---

## 12. 交接说明

- 本文与 `docs/QQ_BOT_CONFIGURATION.md` 是同一轮文档交付，**只写文档**：未新增/修改任何源码、测试、部署工件、`.env.example`、`config.example.yaml`、`README.md`，未创建 `.env`，未 commit/reset/clean。
- 后续实现者请以本文 §4（协议事实）、§6（差距）、§7（阶段）为输入；以配置文档 §3 的拟新增键为草案，实际字段名可在实现时调整，但**必须同步更新配置文档**。
- 任何「已接入 / 已联调 / 可主动无限推送」的表述，在 §8 对应行拿到证据前都不成立。
