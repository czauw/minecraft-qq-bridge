# mcqq-bridge 文档精简与事实同步执行方案

本方案对应 t_6105b40e，供既有下游 Worker t_0ea43396 执行。Planner 仅新增本文件，不修改现有正文、代码、配置或服务；Worker 完成后删除本临时方案。以下行号均为修改前快照。

## 1. 盘点口径与结论

范围：根 README.md + docs/ 下全部文件（含 .example、.json），不含本临时方案、docs.zip、代码/配置模板。实查为 **18 文件，4113 个逻辑行，4112 个换行符行（wc -l 口径）**。文件末尾无换行会产生差一；前后比较统一用换行符计数。

常驻目标为 **6 文件，约 710 行，验收预算不超过 800 行**（含历史摘要）；不是本次已实现的精简结果。保留用户入口与维护契约，移除重复的迁移设计、过期配置和评审流水账。四份核心文件均单独保留：部署实例记录依赖本机、需求契约面向开发、安全模型面向维护，合并进 README 会混淆读者并增加更新耦合。

仓库实查无 HEAD、全为 untracked，不能把“git 可找回”当作既有事实。先执行下游任务要求的可回滚基线，记录 commit ID 并确认 README 与全部 docs 已跟踪，再进行删除；不能仅依赖根目录 docs.zip（本次未核验其完整性）。提交前只检查候选路径/忽略规则，防止 .env、config.yaml、编辑器交换文件、凭据或未知打包物进入基线。若无法安全建立基线，停止删除并在卡片说明原因，不 force-add 敏感文件。Planner 本次没有提交。

## 2. 六项现行事实：以实现或实态为证据

| 项 | 核实结论 | 证据 |
|---|---|---|
| /list 禁用项 | 不建客户端、不查询、不展示；按禁用名查询返回“未找到服务器：X”；空结果返回“没有启用的服务器” | src/mcqq_bridge/manager.py:39-43、67-106；router.py:97-112、268-288 |
| 斜杠过滤 | 先处理精确列表指令，其余剥前缀后以 / 开头的内容直接返回，包括 /help、/listing；这些未知指令不保证有 QQ 回复 | router.py:168-171、206-220 |
| 全量 @ 前缀 | 循环去掉开头连续的 <@...>/<@!...> 标签，再匹配命令；转 MC 的 payload 也使用剥离后文本，不是原 message.text；不声称删除文本中间的 @ | router.py:64-76、210-228 |
| 取群 OpenID | 脚本换 token→发现 Gateway→接收含 group_openid 的事件→打印并退出；它不调用列群 API | scripts/discover_group_openid.py:2-18、67-94、115-186、195-207；README.md:83-101 已有正确用法，不重复写一套 |
| 本机运行方式 | /etc/systemd/system/mcqq-bridge.service，User=czauw，工作目录本项目，ExecStart 用 .venv/bin/python -m mcqq_bridge --config 绝对路径 --env-file 绝对路径；不是桥接器 compose | 单元:12、14、16、20；本次 systemctl show 输出 FragmentPath 对应此单元、ActiveState=active、SubState=running；config.py:252-256 默认状态监听 127.0.0.1:18080；runtime.py:144-167 三接口 |
| QueQiao 路径/实例 | config/QueQiao/config.yml，大小写敏感；四实例名称/WS/RCON/loader 不可丢 | docs/MCSMANAGER_DEPLOY.md:7-25；该文件给出真实部署记录，本次未提权重查 /opt 内实例配置，不把记录冒充本次逐实例复测 |

重要细化：

- “全禁用返回提示”是 router 的空结果行为；正常 CLI 的 load_config(require_secrets=True) 会因零启用项退出 2。因此应同时保留路由语义和“不能全禁用启动”，不能让 README 暗示全禁用能正常起服务。证据 config.py:325-336、410-420；__main__.py:145-151。
- /list 只是默认名称；relay.list_command 可改成另一个以 / 开头的单 token。描述为“唯一实现的列表指令（默认 /list）”，不要宣称硬编码只能叫 /list。证据 router.py:168-171；config.py:245-249。
- manager.status_payload 仍包含禁用项（manager.py:181-199）；不要把“/list 不显示”扩大成“状态接口也隐藏”。
- manager.py:3 的旧注释仍说“/list 显示未启用”，与实际过滤冲突。记录此代码注释债务即可，本任务不改源码。
- 本次只读 HTTP 探测：/healthz={status:ok}；/readyz={status:ready,qq_connected:true,servers_enabled:4,servers_connected:2,qq_degraded:[]}；/api/v1/status 仅提取连接字段，8092 齿轮盛宴与 8094 亡者世界 connected=true，8091/8093 为 false。是即时快照，不能写成固定运行状态。ready 条件仅要求 QQ 连通、至少一台启用服连通、无 QQ degraded，见 runtime.py:148-163；HTTP 200 本身不等于 ready。
- 官方“没有列群 API”的说法已有脚本注释与 README 记载，本次不做平台全量 API 穷举。文档可写“从官方群事件取得，使用自带脚本；不使用 /users/@me/groups”，不要把脚本称为唯一接收事件的工具。真实平台权限、API 规则依查阅日期及管理端为准。

## 3. 每份文档定位与处置（全部 18 份）

以下均为仓库相对路径；“归档摘要”统一指新建 docs/archive/HISTORY.md，不保留整套旧正文副本。

| 文件（逻辑行） | 读者 / 解决问题 | 处置及保留证据位置 |
|---|---|---|
| README.md（472） | 用户 / 从准备到启动、功能与排障 | 保留并精简改写；保留当前字段语义与示例，按第4节同步；目标360行 |
| docs/THREAT_MODEL.md（38） | 维护者与运维 / 信任边界、攻击面、剩余风险 | 保留文件名并重写为现行 QQ×QueQiao 模型；旧控制不能整段移植；:6-38 的通用风险及 README:374-386 为输入；目标70行 |
| docs/MULTISERVER_REQUIREMENTS.md（21） | 开发者 / 产品契约、验收条件 | 保留并小改；保留:8-21 功能与验收，删除:5-6 临时分工/通知流程；注明全禁用 CLI 边界、@预处理与默认命令可配；目标30行 |
| docs/MCSMANAGER_DEPLOY.md（44） | 本机运维 / 四实例路径、版本、端口、实测与暴露 | 保留并扩为当前本机运行手册；:7-38 完整保留事实，新增 systemd 运维；:40-43“缺 AppID”等过期断言重写为凭据位置与事件采集流程；目标90行 |
| docs/VERIFICATION.md（503） | 维护/验收人员 / 能力证据、复现、未覆盖边界 | 改写压缩；保留:40-92 协议结论、:328-334 模拟边界、:449-478 限制（按新证据修正），现行复现命令；评审过程合并归档摘要；目标120行 |
| docs/DEPLOYMENT.md（70） | 旧路线部署者 / OneBot HTTP 拓扑 | 删除；:1-3 明确过期，:9-66 是旧接口/Compose；通用密钥/网络安全要求并入 THREAT_MODEL，不留过期启动步骤 |
| docs/LEGACY_ONEBOT_ENV.example（47） | 旧模块维护者 / 旧配置键 | 删除；:1-2 已限旧回归模块，现行 config.example.yaml 为准；必要旧模块定位进 README 附录及历史摘要，旧源码/测试保留 |
| docs/LEGACY_ONEBOT_README.md（172） | 旧模块维护者 / 历史启动与接口 | 删除；:1-3、:137-160 与现行契约不符；安全原则迁 THREAT_MODEL，README 附录留8–10行迁移说明，不再提供生产回滚教程 |
| docs/LEGACY_ONEBOT_VERIFICATION.md（34） | 审计者 / 旧53测试与模拟基线 | 压成历史摘要一段后删除原文；保留:8-13 的日期/53 tests/模拟限制与:20-27 的 Hermes 未联调、当时复审限制，不能当成新版本审查结论 |
| docs/QQ_BOT_CONFIGURATION.md（716） | 旧方案开发者 / 旧.env与拟议键 | 必要信息合并后删除；:363-410 的密钥保存/轮换进入 THREAT_MODEL；:414-440 身份语义与:494-528 平台约束迁 README/VERIFICATION；:28-362、:532-568 的旧键/回滚不移植；现行 YAML 字段只维护 README §6+模板 |
| docs/QQ_BOT_INTEGRATION.md（437） | 接入运营者 / 资格、权限、身份、官方来源 | 有效部分合并后删除；:71-131 的人工准备浓缩到 README §3；:51-67 平台限制与:178-209 ID语义进入 README/VERIFICATION；:340-373 真实验收矩阵改为现行命令后迁 VERIFICATION；旧as-is/阶段/接口草案直接去掉 |
| docs/research/QQ_BOT_CURRENT_STATE.md（323） | 迁移前开发者 / OneBot旧盘点 | 直接删除；:13-18、:251-264 是已被后续实现替代的现状/backlog；安全风险由 THREAT_MODEL 收敛，历史阶段一句话由摘要覆盖，不存全文 |
| docs/research/QQ_BOT_PLATFORM_FINDINGS.md（181） | 架构/运营 / 2026-11-10平台依据 | 必要事实/来源并入 VERIFICATION 后删除；保留:23-31 新版测试/运营规则、:33-109 权限/身份/配额、:166-181 对应引用；:111-129 旧路线建议废弃 |
| docs/research/multiserver-sources.json（143） | 维护者 / 23条上游引用清单 | 合并有用 URL 与 accessed 日期进 VERIFICATION 后删除 JSON；:5-140 无正文或事实映射且 title 全空，独立价值低；不机械复制23条重复链接 |
| docs/research/official-qq-current-state.md（456） | 迁移前维护者 / 旧主机/代码基线 | 直接删除；:14-23、:179-208、:220-248 的“未运行/无.env/无QueQiao”已失时；只将历史阶段、53测试和实测局限写进统一摘要，禁止继续以它描述现状 |
| docs/research/official-qq-handoff.md（76） | 审计者 / 旧文档复审有效结论 | 压一段到历史摘要后删除；必须保留:17-19 指出 t_863b01da 最新复审 PASS、首轮 CHANGES_REQUIRED 已过时；保留:59-66 中“不可读不等于不存在”、安全扫描及模拟边界教训 |
| docs/research/official-qq-platform-and-architecture.md（261） | 迁移设计者 / 早期方案和平台来源 | 事实/引用合并 VERIFICATION，决策压一段历史摘要后删全文；:15-38、:40-49、:94-117 可追溯；:119-234 旧设计不是现行API，不照搬 |
| docs/research/official-qq-review.md（119） | 审计者 / 首轮旧文档缺陷快照 | 与 handoff 合成一段历史摘要后删除；保留:10-40 安全示例/泄密扫描教训、:44-68 证据过推问题及有效复审卡ID，不留下孤立“当前未通过”标签 |

新增 docs/archive/HISTORY.md：目标40行，标题明确“历史摘要，不是部署指南”。仅记三个阶段（OneBot基线→官方文档设计与复审→官方×QueQiao实现和队列修复），每段注明日期、适用范围、原卡ID/基线commit、结果与未联调范围。可由基线 git show <commit>:<原路径> 找回原文；不把临时 scratch 路径当长期归档。README 留一行链接指向它。

## 4. README 逐章节动作（沿用现有章节号）

| 原章节/行号 | 具体改动 |
|---|---|
| 开头 :3-12、§1 :16-33 | 去掉“含禁用状态”；能力表精简到用户可感知能力，将关闭码/队列修复细节转验证文档；真实状态按“离线模拟/真实MC端局部验证/网关连接快照/游戏内双向待证”分层 |
| §2 / §2.1 :37-64 | 保留 Python要求、安装命令；依赖明细引用 pyproject 避免双抄；精确已部署 loader/jar 引用 MCSMANAGER_DEPLOY，移走983版本文件数等动态统计；历史v0.5.0/core核对标日期，不再写“最新” |
| §3 / §3.1 :68-101 | 保留已有 discover 命令，补 systemctl stop 后取ID、完成后 start 的操作顺序（写成运维步骤，不在本次执行）；用 --env-file，不推荐 --secret；脚本打印成员ID/消息片段（脚本:97-112），不要粘原输出到公开工单；去掉强制旧“沙箱配置”路径，沿用旧研究中新版管理员可直接测试的日期化依据并要求以管理端实际显示为准；不声称脚本是唯一事件读取方式 |
| §4 / §4.1 :105-189 | 每处插件配置明确 config/QueQiao/config.yml；MC server.properties 与 QueQiao 双侧 RCON步骤必须保留；本机四服用端口表链接；泛用WS示例优先回环，同容器MC侧0.0.0.0监听与宿主发布绑定分别讲清，不把模板改成对所有环境都适用；删除重复上游产物统计与过宽loader范围 |
| §5 :193-224 | 保留复制安全模板、chmod 600、check-config、手动前台启动；加现有systemd管理命令（status/start/stop/restart、journalctl），引用真实单元路径/User/工作目录/ExecStart；新主机如何用这些同样参数建单元写简短模板或明确步骤，不能仅给start命令却假设单元自带；不用 sudo 打印 .env；不实际重启服务 |
| §6 :228-292 | 现行 YAML 字段表保留，合并默认值/用途的重复说明；bot/runtime等可分紧凑表，不能为了行数漏字段；明确 list_command 可配、禁用项仍参与唯一性校验且可在status中出现；配置唯一细节以 config.py 与中文模板为准 |
| §7.1–7.4 :296-343 | 保留已有正确/list示例，补“全禁用空结果”及CLI启动拒绝区别；增加 <@A> <@B> /list 与 <@A> 大家好 的行为；未知斜杠命令仅过滤、不承诺/help回复；事件格式、禁用零连接保留 |
| §8 :347-370 | 压缩协议考据，留下rcon/status模式、完整名单判据、失败显示离线、重连重同步；auto 在RCON失败后仍可用完整status样本兜底，纠正“不开RCON必定离线”的绝对说法；引用 VERIFICATION 与 queqiao.py |
| §9 :374-386 | 保留简短安全摘要，详见新版 THREAT_MODEL；“不执行RCON”改为“群文本不能发起任意控制台/shell，列表命令仅来自本地配置由QueQiao执行”；官方默认运行时不加载旧Hermes，旧模块仍在，不宣称全仓库不存在旧接口 |
| §10 :390-405 | 原Docker章节替换成“运行状态与维护”，解释三接口判据、日志/重启/安全轮换的指路；Docker仅附注“仓库保留可选工件、非本机运行方式、尚无构建验证”，不再把compose当推荐主路径，不改Docker工件 |
| §11 :409-424 | 保留 pytest、smoke、check-config 的复现命令及模拟含义；不得把旧228计数冒充本轮执行结果；加实际群发普通话→游戏可见、游戏chat→群可见、/list含已有玩家、断线/重连/权限关闭的验收步骤，结果按证据记录在VERIFICATION |
| §12 :428-472 | 保留失败排查；未找到名需补“也可能禁用”；权限未知/11253导致degraded不等于进程失败；移除旧OneBot文档链接，附8–10行迁移摘要：新入口/新YAML/旧键不生效/ID不可互换/旧源码仅回归/无自动provider回滚/Hermes未接入/历史摘要链接 |

## 5. 不能删的事实、证据和风险

1. **本机实例表及网络边界**：四个名称、UUID前8位、MC版本/loader、MC端口25566/25565/25567/25566、WS8091–8094、容器内RCON25581–25584；不要因MC端口重复就擅自修正。QueQiao jar版本与Fabric依赖要求、配置大小写、MCSManager端口映射路径、备份位置保留（MCSMANAGER_DEPLOY:7-27）。
2. **实测不等于全链路**：该记录:29-38证明逐实例插件启动、RCON list零人返回、broadcast ACK及token鉴权；没证明真实玩家看见消息、非零已有名单、join/leave/death和QQ群双向。本次HTTP证明QQ已连且部分MC已连，不能据此确认真实消息收发。:40-43旧“尚缺凭据”需撤销绝对断言，但不能写凭据值。
3. **真实暴露风险**：四个WS经Docker发布在0.0.0.0，记录UFW未启用，token鉴权不是私网ACL；RCON是容器内，不要指导开放公网。127.0.0.1绑定建议标成待实施建议、不要称已经生效（MCSMANAGER_DEPLOY:44）。本次未改网络。
4. **名单权威性与失败条件**：发布v0.5.0/core0.6.8源码核对范围；不伪造get_players；get_status是ping采样，仅完整样本接受；RCON两侧配置/密码一致、桥接器不直连RCON；断线清缓存、增量不是完整快照、解析人数必须与名字数一致（VERIFICATION:42-77；queqiao.py:85-123；README:155-189、347-370）。不要从历史核对推断所有未来版本都没有API。
5. **现行信任边界**：QQ和MC文本不可信，本地配置/密钥属运维控制；群目标精确匹配、过滤机器人、有限去重/回环、连接代次隔离、广播前登记、受控错误脱敏；QQ/QueQiao鉴权头不同，状态端口不得公网开放。官方路由未接Hermes，旧Hermes模块存在不代表当前可启用；将来扩展必须独立授权/身份映射，群admin不自动等于主机执行权限（runtime.py:34-50；router.py:198-247、292-309；旧THREAT_MODEL:24、28-38）。
6. **权限/身份/额度**：OpenID不是QQ/群号，按AppID作用域；全量接收与主动发送是两个权限；token HTTP200仍可能业务失败；被动5分钟/5次与msg_seq；主动配额和内容审核不是配置可绕过。引用迁入VERIFICATION且保留日期（INTEGRATION:51-67、146-209；PLATFORM_FINDINGS:166-181）。README:78的“60/20”不能当同维度Bot额度，按已有来源区分Bot60/30、单关系20、每日群1000，未重新在线核验则明确为2026-11-10资料值。
7. **已实现与建议分开**：当前bot_state unknown会暴露degraded但可尝试主动发送，false才禁止；没有持久消息账本/聚合/日配额强制预算的实证，不把研究建议当已实现。去重/Resume只能覆盖有限窗口，进程重启与超时不保证无丢失/恰好一次。昵称fallback不是真群名片、附件fallback顺序不可还原、event_workers默认4可能乱序、vanilla/Velocity死亡限制保留（VERIFICATION:271-280、449-478）。
8. **历史验证与当前验证分开**：53旧模块回归、228与10/10为2026-11-11历史输出；旧smoke文本:351、355仍包含禁用“未启用”，与现行代码不符，不可作为新输出保留在当前结论；移摘要并注明旧输出已失效。保留安全回归测试映射（队列满→Resume重投、ACK前回环、UUID/查询代次、坏JSON不算成功、严格类型、日志canary）而删修复叙事。
9. **来源可追溯**：精简后的VERIFICATION保留最小来源表：QQ接入/FAQ（带尾斜杠）、token、Gateway、全量事件、发群消息、bot_state、QueQiao仓库/版本core处理器路径、V2 RCON/get_status/配置/死亡事件；每条链接绑定对应结论和查阅日期。Webhook没实现，不保留大段可执行Webhook教程；摘要说明未来若选Webhook必须另验签、去重、公网入口与平台规则。

## 6. 执行顺序、规模与验收

1. 建可回滚基线，记录commit和文件清单；不要覆盖现有未跟踪文件，不读取/公开真实凭据。遵守下游卡的仅文档改动边界。
2. 先写好四份核心文档、压缩VERIFICATION、新建历史摘要；按上述映射迁出有效事实/来源，再删原13份。research不留空壳；保留旧源码/测试/脚本及Docker工件。
3. 每个删除文件在Worker卡评论列出理由、迁入位置与基线恢复方式。删除引用随正文一起更新；README只指向现行五份配套文档之一，不指向删除文件。
4. 文件预算：README 360 + THREAT_MODEL 70 + MULTISERVER_REQUIREMENTS 30 + MCSMANAGER_DEPLOY 90 + VERIFICATION 120 + archive/HISTORY 40 = **6文件约710行**；容许到800行保证安全事实/全部字段不丢。超出预算说明必要内容，不用压成超长单行凑统计。
5. 完成后删DOC_PLAN.md；同口径实测文件数/换行符行数。输出改前18/4112、改后实数、git diff --stat及逐文件删除/合并清单。方案本身与归档全文备份不计入常驻目标；历史摘要计入。
6. 检查相对Markdown链接全可解析；搜索“含禁用/未启用/queqiao/config/8000/QQ_OFFICIAL_/OneBot/无AppID/未装真实插件/最新”逐项人工解释，历史摘要可带限定语，不是一律删词；复核所有源码和配置未改。
7. 以 config.py/config.example.yaml 核对字段表；静态核对README命令入口/绝对路径，运行安全模板 --check-config、现有pytest与smoke（不得重启生产、运行discover、发送真实消息或起真实MC）。如执行测试，使用 -B 和 -p no:cacheprovider 避免无关缓存；报告实际结果与失败，不把历史计数当新结果。文档任务不要求新增测试。
8. 真实QQ↔MC消息矩阵只写操作与当前证据状态，不代发验证。/readyz至少一服连通的语义、全禁用CLI限制、状态仍含禁用项是专项审读点。

交接：本任务完成只表示方案交付，实际改写由已存在Worker执行，其后接既有审查卡。无需新增子任务或Expert。
