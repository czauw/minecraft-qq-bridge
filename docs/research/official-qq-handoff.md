# QQ 官方 Bot 文档最终验收与交付

本次验收任务：`t_f87d4eda`。结论：**方案与配置文档交付通过（PASS，保留非阻断建议）**。本阶段完成了现状研究、官方平台研究、两份用户文档、两项 Major 修复及独立复审；没有实现官方 adapter，没有部署、申请账户、登录管理端或发送 QQ 消息，未证明真实 QQ↔MC 联通。

## 交付文件

| 文档 | 绝对路径 | 内容 |
|---|---|---|
| 接入文档 | `/home/czauw/minecraft-qq-bridge/docs/QQ_BOT_INTEGRATION.md` | 主体与应用、测试群权限、官方接收及鉴权、架构、迁移差距、阶段和真实联调矩阵 |
| 配置文档 | `/home/czauw/minecraft-qq-bridge/docs/QQ_BOT_CONFIGURATION.md` | 当前配置与未来草案分栏、安全最小示例、密钥保存、OpenID、网络、限流、排障和回滚 |
| 本汇总 | `/home/czauw/minecraft-qq-bridge/docs/research/official-qq-handoff.md` | 有效审查结论、证据、限制和后续准备 |

两份用户文档已完整读取。平台资料查阅日期为文中记录的 2026-11-10 UTC；平台规则执行前仍须复核。

## 有效审查结论与证据链

已回读以下卡的实际状态、评论与交接：现状 `t_5464e0c4`、平台架构 `t_80913f24`、文档 `t_ed9ec500`、修复 `t_8bd76e90`、独立审查 `t_863b01da`，均为 done。审查卡最新复审的结构化结论为 `verdict=PASS`、`review_outcome=approved`，关闭 M1/M2，无未解决的 Major 阻断。

**历史报告说明：** `docs/research/official-qq-review.md` 保留首轮 CHANGES_REQUIRED 快照。负责人要求复审不得修改项目文件，因此它没有被重写；当前有效结论是 `t_863b01da` 最新 reviewer 评论及第二次运行完成交接，不应把首轮快照当作当前仍阻塞。本汇总将这一区别保存在项目内。

| 验收点 | 实际证据 | 结论与范围 |
|---|---|---|
| M1 安全示例 | 配置文档 144–205 行；Worker 修复后，Reviewer 独立从 Markdown 抽取 dotenv，经真实 `Settings(_env_file=...)` 加载并启动临时 loopback uvicorn；healthz/readyz 均 200 | 通过；空白名单、OneBot 出站/say/Hermes admin 关闭、token 全空；临时进程已退出 |
| M1 token 语义 | Reviewer 以虚构配置 token 对照：无 token 401、错误 token 401、匹配 token 202；空期望 token 拒绝 | 通过；202 仅说明本地接受事件，不证明 QQ 投递 |
| M2 扫描不打印秘密 | 配置文档 370–402 行；Reviewer 独立在 scratch 虚构 canary 夹具执行键名与按值扫描，报告批准位置和误放文件，排除依赖，stdout/stderr 不含值 | 通过；未扫描真实凭据 |
| 回归 | 本最终验收实际执行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -W error -p no:cacheprovider` | **53 passed in 1.19s，退出 0** |
| 官方事实依据 | 已回读平台研究和首轮独立审查；Reviewer 首轮独立读取十份官方正文，核对 WS/Webhook、token、签名、群身份与配额 | 作为研究交付证据；最终验收没有重复调用官方 API 或登录核权 |
| 交付物存在 | 最终验收读取两份全文，并运行 `wc -l -c` 与 `sha256sum` | 接入文档 433 行/37978 字节；配置文档 710 行/54421 字节 |

验收时文件 SHA-256：

```text
34f1f07a0027aad7d113ffd1e100a3b08bb1f1fead6b4b112242d2c0d0990897  docs/QQ_BOT_INTEGRATION.md
e146b1e20e485ac593c38f53017ae35f2ecf21f5a08fe818c9e5d6ccb5928517  docs/QQ_BOT_CONFIGURATION.md
```

上游模拟 smoke 通过仅证明 OneBot/MC 模拟 peer 路径，不能作为官方 QQ 或真实 Minecraft 集成证据。最终验收采用刚完成的 Reviewer 专项复审证据，未重复启动服务。一次内联 Python 统计探针被无交互自动审批拒绝，未执行；已用获准的 `wc`/`sha256sum` 完成交付物统计，字段支持通过读取实际 `settings.py` 和复审证据核对，无需修改审批配置。

## 推荐方案与关键限制

推荐拓扑：**QQ 官方平台 ↔ 新增官方 adapter（客户端出站 WSS 收事件、HTTPS 发消息）↔ 现有 MC 状态/命令核心 ↔ 待确认或提供的 MC 事件源与 callback consumer**。

- 首期一个测试群，仅 `@机器人 /mc players`、`/mc status`，回复标记缓存时效。@ 查询不代表完整双向聊天。`say`、主动通知、全量接收分别验收；Hermes admin 保持关闭。
- 本机桥接器及 MC/状态/管理入口保持 loopback 或私网，首期 WSS 不需要新增公网回调。Webhook 只是备选，须另验公网 HTTPS 端口、证书、原始 body 和 Ed25519 签名；本机 Caddy/frp 尚未验证满足要求。
- 官方被动群回复有 5 分钟/每条消息 5 次限制。主动通知还受群主授权、应用权限、Bot/单群频率、每日额度和内容审核限制；不能承诺无限或无损全量同步、零风控。
- 数字 QQ 号/群号不能直接迁移为 `member_openid`/`group_openid`。白名单使用官方事件中的原样字符串，并按 AppID/群作用域批准，首次出现不自动授权。
- OneBot 与官方协议鉴权、事件体、WS 方向不同，不能只替换 URL/token。官方与旧 provider 只能单选出站；回滚 OneBot 必须显式选择，仍有第三方登录风险。

## 当前能力与待实现差距

已有代码包含 MC 六类 HTTP 事件模型、Bearer 校验、状态 TTL 缓存、OneBot HTTP/反向 WS、查询命令、受控 say callback，以及默认关闭的 Hermes 管理调用。它们已经过本地测试，但不等于已部署或真实接通。

尚需实现官方 token 生命周期、Gateway 客户端、事件归一化/去重、字符串身份、被动回复上下文与 msg_seq、结构化投递结果、主动权限控制、限流队列和持久账本。源码 `Settings` 未定义官方配置字段；文档中的 `QQ_PROVIDER`、`QQ_OFFICIAL_*` 都是草案，当前写入环境会被忽略，服务启动不能证明官方配置生效。MC 事件处理返回 202 也不能证明群内收到消息。

MC 侧尚未取得已对接本 bridge 的事件源/consumer 证据，默认 callback 端口在上游探测时无监听。实例目录与容器因权限未充分检查，**不能断言整台主机绝无组件**。目标 Minecraft 版本、loader、模组和部署位置需在实施前逐实例确认，不能把 Forge/Paper/Fabric 工件混用。

## 保留的非阻断建议及阅读修正

Reviewer 的 N1/N2/N3 按有界修复范围保留，未声称已全部修复。本卡仅能写本汇总，不改两份用户文档和上游报告。后续使用文档时按以下解释执行：

1. 研究图将 MC producer/consumer 写作“现有”不构成部署证据；按“待提供/未验证”理解。无 `.env` 与无宿主监听不能证明所有服务环境无注入。Fabric server launcher 文件名不能证明是客户端 jar；只能确认它与目标 Forge 版本不匹配，未经核实不安装。
2. 实施前应明确 MC 采集器/consumer 交付及 loader/version；官方 callback 需版本化字符串身份契约。若后续纳入 `/mc say`，必须验收双开关、未授权拒绝、真实游戏内显示与防回流，callback HTTP 200 不代替游戏显示。
3. 配置文档“8 个死配置”应为 **7 个**：app_name/log_level/host/port 和三个 llm 字段。以逐项表及源码为准。§2 也含明确标成零引用的字段，并非所有列项改了都会生效。
4. 接入矩阵“三方成功证据”仅适用于正向发消息；连接测试看 READY/心跳，负向测试看拒绝和无副作用，不能要求未授权群收到成功回复。
5. 阶段 0 可以先记录群 OpenID 待采集；受控官方接收器可用后才采集候选并人工批准，避免循环前置。首期持久化需求、可写卷与账本格式应在实现任务明确，不能用静默不开启代替要求。
6. FAQ 使用带尾斜杠的 `https://bot.q.qq.com/wiki/bot_FAQ/`。Bot Secret 与 clientSecret 是否同值、Webhook 重投/ACK 时限/时间戳容差、event_id 时效、账号实际开通能力仍待核实，不猜数值。

## 用户后续准备项（本次未代办）

- 确定自管测试群或公开服务目标、主体认证类型及管理员归属；按接入文档人工创建/核对 QQ 群机器人应用，确认真实服务范围和权限。公开服务资格不能由文档代替审批。
- 准备一个群主可配合的测试群，记录全量接收/主动推送开关状态；首次测试只开 @ 查询。取得应用凭据后保存在受控 secret 存储，**不要发到聊天、工单或仓库**。
- 明确服务器实例、MC 版本/loader/模组、是否跨主机，以及授权的实例检查途径；后续才能选择精确兼容的事件采集与回调组件。
- 核实真实 API 出口 IPv4、代理 WSS 长连接与平台白名单。若以后采用 Webhook，再准备官方允许端口的公网 HTTPS 回调入口。
- 后续另行授权实现与真实联调。取得接收器后采集并人工批准 OpenID，先验一个群的查询，再验真实 MC 状态、通知、重连/去重、权限撤销；需要 say 时加游戏内显示验收。

本次唯一新增项目文件为本汇总；现有 untracked 原样保留，未 reset/clean/commit，未启动实现、部署或账户申请。文档阶段验收结束，真实迁移及联调仍未执行。
