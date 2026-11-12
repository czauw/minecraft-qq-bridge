# 官方 QQ 群 Bot 方案与配置文档：独立审查

- 审查卡：`t_863b01da`；审查日期：2026-11-10 UTC。
- **结论：CHANGES_REQUIRED（需修改，未通过）。** 两项 Major 阻断项见 M1/M2；其余为非阻断修订建议。
- 范围：完整阅读 `docs/QQ_BOT_INTEGRATION.md`、`docs/QQ_BOT_CONFIGURATION.md`、`docs/research/official-qq-current-state.md`、`docs/research/official-qq-platform-and-architecture.md`；核对现有源码、部署配置及测试；独立回读十份官方页面。
- 本阶段只是方案/文档审查。没有申请、登录、发布、部署或使用真实凭据，没有发 QQ 消息，没有证明真实群服联通。只新增本报告，未修改被审查文件、源码或部署配置，未 commit/reset/clean。

## 1. 阻断项

### M1 — Major：标注“当前可运行”的最小示例实际上无法启动

**位置**：`docs/QQ_BOT_CONFIGURATION.md:144-185`，尤其 154 行 `ALLOWED_GROUP_IDS=<NUMERIC_GROUP_ID>`、174-185 行启动/预期说明；对应代码 `src/mcqq_bridge/settings.py:68-73`、`:76`。

**问题与复现**：按原文第一段 dotenv 原样写入 reviewer scratch 的 `.env` 文件，通过真实 `Settings(_env_file=sample_path)` 加载，得到：

```text
SAMPLE_LOAD_FAILED 1 validation error for Settings
allowed_group_ids
  Value error, invalid literal for int() with base 10: '<NUMERIC_GROUP_ID>'
```

`Settings` 导入时即会实例化，因而 uvicorn 无法加载应用。185 行宣称“用上面的配置启动后 /healthz 通”与可复现结果相反。占位密钥本身可作为字符串，但数字列表占位符不能作为整数加载。现有 `.env.example` 的测试通过不能证明这份新示例可用。

**影响**：首次按文档启动失败，且会把配置校验失败误导成“健康检查通、业务安全关闭”的状态。

**最小修正**：为了匹配“未接入 OneBot 时保持关闭”，示例使用 `ALLOWED_GROUP_IDS=[]`，将待填写数字群号放在注释中；或明确本示例必须先替换并校验所有类型占位符，不能称原样可运行。修正 185 行：MC 请求是否 401 取决于请求携带的 token 是否匹配，不是“值是占位符就必定 401”。不要为通过示例而启用 OneBot 或填写真实凭据。

**验收方法**：从 Markdown 抽取该代码块到 scratch，用实际 `_env_file` 路径加载（不要把 dotenv 字符串直接当构造参数，后者不会经过 env JSON 解码）；再用相同配置启动 loopback uvicorn 检查 `/healthz`。原样安全示例应加载成功，QQ 出站保持关闭，空群白名单。

### M2 — Major：泄漏排查命令会把真实密钥打印到终端/工单日志

**位置**：`docs/QQ_BOT_CONFIGURATION.md:337-352`，尤其 350 行。

**问题**：建议执行的 `grep -rIn -e QQ_OFFICIAL_CLIENT_SECRET -e QQ_OFFICIAL_BOT_SECRET . --exclude-dir=.venv` 默认打印完整命中行，既不排除 `.env` 及备份，也不隐藏值。本文同一节又允许用 `.env` 保存真实 secret，故此命令恰好会输出 `QQ_OFFICIAL_CLIENT_SECRET=<真实值>`。声称“应只命中 .env.example/文档占位符”与保存方式矛盾。这是静态可确认的输出路径；本轮没有执行该命令扫描任何真实凭据。

**影响**：运营者为排查泄漏反而把 secret 带入终端留痕、录屏、排障输出或粘贴工单，违反本文自己的凭据不入日志边界。

**最小修正**：只报告命中文件名（例如 `grep -rl` 而非打印行），限定扫描范围，明确区分批准的 secret 存储与其他文件。不要简单排除 `.env` 后就宣称“没有泄漏”；批准存储和备份也需按权限/保留策略审计，但不输出值。

**验收方法**：在 scratch 建一个只含虚构 canary 的 `.env` 和一个误放 canary 的普通文件，运行替代命令，断言输出不包含 canary 的值，同时仍能报告误放文件。不要使用生产 secret 做该测试。

## 2. 非阻断问题与具体建议

### N1 — Minor：现状证据边界存在过度断言，架构图又把缺失件画成现有

**位置**：
- `docs/research/official-qq-current-state.md:19,167,231,235-246`；同文件 165、237 行明确容器/实例目录不可核实。
- `docs/research/official-qq-platform-and-architecture.md:151,155` 标为“现有 MC 事件生产者”“现有私网 MC callback consumer”。
- `docs/QQ_BOT_INTEGRATION.md:15,20,24-25`。

未发现监听/可读目录未命中，只足以说“未发现已对接本 bridge 的证据”，不能在同时承认实例目录及容器不可读时证明全机“没有任何组件”。没有根目录 `.env` 也不能独立证明服务环境没有变量注入，尤其当前根本没有运行实例。Fabric launcher 的文件名不能证明其为“客户端侧 jar”，应标为未运行且与目标 Forge 实例不匹配，而非客户端工件。

**建议**：保留已观测的无监听、无 bridge 进程、源码缺少采集器等事实，将全机否定判断降为“未发现/未核实”；图中统一写“待提供/未验证”。当前“未联通”的总体判断不因此改变。

### N2 — Minor：迁移阶段未把 MC 缺失件安排为明确交付，验收矩阵缺少 say 的游戏内显示

**位置**：`docs/QQ_BOT_INTEGRATION.md:303-332,336-367`；对照架构研究 `:125,218-219`、源码 `src/mcqq_bridge/models.py:47-54`、`service.py:87-90`。

文档已正确指出 MC 事件源和 callback consumer 缺失，但后续阶段主要安排 QQ adapter；矩阵没有一项明确验证“QQ `/mc say` → 真实游戏内显示”。现有 callback 体的 group_id/user_id 为整数，官方身份为字符串，不能仅称 mc_adapter 可复用就默认 consumer 契约也可复用。

**建议**：增加独立 MC 事件源/consumer 选型与版本确认前置工作、产出及退出条件，不在本阶段安装 mod；把官方 callback 的版本化字符串身份契约写入差距清单。若 say 纳入后续阶段，补“双开关、未授权拒绝、真实游戏显示、防回流”的矩阵；否则明确本轮范围不验收 QQ→MC 聊天，不将 @ 查询称为完整双向聊天。

### N3 — Minor：若干精确统计、路径与流程有小范围自相矛盾

- `QQ_BOT_CONFIGURATION.md:30` 称 8 个死配置，实际列出的 app_name/log_level/host/port 与三个 llm 字段合计为 7 个；本轮源码标识符探针没有发现这 7 个字段在其他源码文件中的引用。
- `official-qq-platform-and-architecture.md:260` FAQ 来源无尾斜杠，接入文档 `:425` 自己已记录该形式不能取得正文。应同步采用已直读成功的带尾斜杠地址。
- `QQ_BOT_CONFIGURATION.md:279` 称账本/去重“默认不在首期开启”，但接入文档 `:311` 已将去重表/发送账本列为阶段 1 产出，且架构研究要求持久化/恢复；明确首期需要什么持久化及可写卷，别以静默不开启替代安全要求。
- 接入文档 `:338` 把“每条验收都必须群内可见消息+成功响应”套到 B1/B2/B3 等拒绝测试及 A1 连接测试，不可满足；改为仅适用于正向发送场景，负向场景应以无副作用和拒绝证据通过。
- 接入文档 `:306` 在阶段 0 要求从真实事件取得 group_openid，官方接收器却阶段 1 才实现；可以先记录“待采集”，随后用受控接收器取得候选值再人工批准，不必让运营者在尚无工具时完成循环前置。

## 3. 独立核查中已成立的部分

以下不是对申请权限或真实集成的认可，只是对匿名官方文档与本仓库事实的交叉核对。

| 审查点 | 证据与结论 |
|---|---|
| 当前接收方式 | 官方现行指南明确允许 WebSocket/Webhook 切换且立刻生效，WS 不依赖公网回调；本方案 WS 首选有直接依据。[1] |
| Gateway 协议 | 独立读到 Hello、Identify token=`QQBot {AccessToken}`、心跳 ACK、Resume/session/seq、关闭码 4006/4007/4008/4009/4014/4914/4915。采用客户端出站方向与现有 OneBot 反向 WS 服务端不同。[3] |
| HTTP 地址验证/验签 | 官方列 HTTPS 与 80/443/8080/8443；op13 按 event_ts+plain_token 签名；事件签名按头时间戳+原始 body，Bot Secret repeat/截取 32 字节种子，Ed25519。文档并未给出这里所需的固定重投/ACK/时钟容差保证，待实测标签恰当。[4][5] |
| token | 当前接口是 appId/clientSecret 换取 access_token；失败可为 HTTP 200+业务 code；expires_in 按实际返回解析，临近到期 60 秒内可取新 token；HTTP `QQBot` 与 OneBot Bearer 不可互换。[6] |
| 身份 | 官方明确不同 AppID 得到不同 openid，群事件携带 group_openid/member_openid。不能把旧数字群号或管理员 QQ 号直接搬过去；本地授权复合键是应用策略，不是“已完成绑定”。[7][8] |
| @ 与全量 | @ 事件正文说明去掉机器人前缀；全量事件另有开启“接收所有消息”前提，且包含成员 mention 替换。群主权限开关有官方 FAQ 依据；文档没有把其当作本应用已开通。[2][8][9] |
| 消息窗口和额度 | 独立读取发送群聊消息页确认被动 5 分钟/5 次，msg_id/msg_seq 去重语义，event_id 的三类允许事件，主动 Bot 60/30 qpm、单关系 20 qpm、每群每天 1000 条及接口 100 QPS。没有把频道限制替代群接口规则。[10] |
| 沙箱与发布 | 当前 FAQ 确有管理员无需沙箱、旧版管理入口停用、个人暂不公开、封禁暂不解封等说明；个人认证进群 500 与主体服务范围有指南依据。不把旧资料套成强制沙箱申请流程。[1][2] |
| 本地协议/安全边界 | 实读 main.py、settings.py、service.py、mc_adapter.py、models.py 和 compose：只有 OneBot 装配，无官方路由；MC Bearer、64 KiB 上限、群 allowlist、say 默认关闭、Hermes 独立开关与整数 admin 契约均与主体描述一致。compose 发布限于 loopback。 |
| 未实现标识 | 两份用户文档对官方 adapter、拟新增键和真实联调均明确标未实现/未执行，没有以 mock 或 socket 连接冒充真实 QQ/MC 通信。 |

## 4. 本轮真实执行记录

1. `git status --short`：所有项目交付仍为 untracked，没有可用提交差异；按文件完整审读而非虚构 git diff。
2. `.venv/bin/python -m pytest -q -W error`：**53 passed in 1.15s**。
3. `.venv/bin/python scripts/smoke.py`：退出 0，原文输出 `SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only`。临时 uvicorn 正常关闭。仅本地模拟 peer，非官方联调。
4. `ss -ltn`：本轮未看到 8000、3000、25580、8642 监听，看到 MC 25565/25567、面板 23333、Caddy 8443。未据此推断不可读容器/实例目录中绝无任何相关文件。
5. scratch 探针：`Settings.model_fields` 为 35，`qq_official*` 字段为空；未知 `qq_official_app_id` 构造输入被忽略；根目录 `.env` 不存在；7 个疑似死配置标识符在其余 src 文件中无引用。
6. 对新配置示例做真实 `_env_file` 解析，确认 M1。首次试验使用构造参数传 dotenv 字符串，产生额外 dict 类型错误；该试验方法不等价于 env 加载，已纠正，不将该额外错误列为缺陷。
7. 通过 `web_extract` 分两批独立读取下列十份官方正文，非仅检查 HTTP 200。接口页面提取在长示例处截止，但本报告引用的基础字段与额度均出现在已读取正文内。未调用任何带凭据 OpenAPI。
8. 所有探针只存 reviewer scratch：`/home/czauw/.hermes/profiles/reviewer/cache/scratch/qq_review_probe.py`、`qq_review_sample.env`；不含真实凭据。引用 ledger 同在该 scratch，文件名 `qq-review-ledger.json`。

## 5. 修复与复审交接

- 由负责人协调原文档 Worker 修复 M1/M2，不要求新增实现或部署；本 reviewer 不直接修改其他文件。
- 非阻断建议 N1/N2 涉及上游研究结论与架构，不应由多人并行改同一文件。尤其不要为填补证据去读取真实 secret 或擅自提升系统权限。
- 复审最小范围：确认 M1 的安全示例通过实际 env 加载与 loopback 启动；M2 用虚构 canary 证明无密钥值输出；检查两份用户文档相应预期已同步。离线 53 项测试不能替代这两个文档专项验证。
- **在阻断项解决并独立复审前，本卡不标 done；按本卡协议置 needs_input，等待负责人安排有界文档修复。** 不需要 Expert 升级。

引用说明：下方 Sources 由 ledger 生成。ledger 对 URL 尾斜杠做规范化；其中 FAQ 的实际成功读取地址是 `https://bot.q.qq.com/wiki/bot_FAQ/`，请保留尾斜杠访问，不能用规范化短地址作为可用性证明。

## Sources

[1] https://bot.q.qq.com/wiki
[2] https://bot.q.qq.com/wiki/bot_FAQ
[3] https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html
[4] https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html
[5] https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html
[6] https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
[7] https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html
[8] https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html
[9] https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html
[10] https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html
