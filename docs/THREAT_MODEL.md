# 威胁模型与已知限制（QQ 官方机器人 × QueQiao）

适用范围：本仓库当前实现（`src/mcqq_bridge/`，官方 QQ 接口 + 每服一条 QueQiao WebSocket）。
旧 OneBot 模块仍在源码树里，但不在默认启动路径上。

## 资产

群消息收发权限（官方 openid 作用域）、每服的 QueQiao `access_token`、QQ `client_secret` /
`access_token`、RCON 口令、玩家名单与聊天内容、只读状态接口暴露的运行信息。

## 信任边界

* **可信（运维控制）**：`config.yaml`、`.env`、systemd 单元、主机与网络配置、QueQiao 侧 `config.yml`。
* **不可信**：QQ 群成员文本与昵称、MC 聊天/死亡文本、MC 玩家名、QueQiao 与官方 API 的响应体、
  任何网络对端。这些内容只作为**数据**使用，不进入命令、不进入 shell、不拼进 URL。

## 入口与出口

| 方向 | 入口 | 出口 |
|---|---|---|
| QQ → MC | 官方网关 WebSocket（出站长连接，无需公网入站） | QueQiao `broadcast`（纯文本） |
| MC → QQ | 每服 QueQiao WebSocket（出站） | `POST /v2/groups/{openid}/messages` |
| 运维 | `127.0.0.1:18080` 三个只读端点 | 无写操作 |

没有入站 HTTP 回调、没有公网监听（状态端口默认 loopback），也没有接受外部命令的接口。

## 已实现的控制

* **QQ 侧鉴权**：`client_secret` 换 `access_token`，业务请求头为 `Authorization: QQBot <access_token>`；
  只有接口返回 HTTP 401 时才作废本地缓存并刷新 token 重试一次，不无限重试。token 与密钥只从
  YAML/环境变量读取，日志与状态接口不输出。
* **QueQiao 侧鉴权**：QueQiao `config.yml` 配了非空 `access_token` 时，握手头须为
  `Authorization: Bearer <access_token>`，且对端把 `x-self-name` URL 解码后与 `server_name` 全等比较；
  缺失或错误即 `1008 Authorization Header is wrong`，该台隔离，不影响其它服务器；对端未配置则不校验该头。
* **群目标精确匹配**：`group_openid` 不等的事件直接丢弃；私聊、频道事件不处理；`author.bot=true` 过滤。
* **去重**：`(group_openid, message_id)` / `(server, event_id)` 有界 TTL 去重；去重表只在事件**成功入队后**写入，
  队列满的溢出事件保持可重投（不因去重丢失），未处理事件不推进恢复水位。
* **回环抑制**：QQ→MC 广播**前**逐目标登记 `(server, 归一化文本)` 指纹；广播失败撤销登记；引用计数处理并发同文。
  属有界时间窗口缓解，不是强一致。
* **命令面收敛**：唯一被执行的命令是 `players.rcon_command`（默认 `list`），来自本地配置；
  QQ 消息永远不能变成 RCON/shell 命令。所有 `/` 开头的群聊都不转发到 MC。
* **单服隔离**：坏包、慢查询、拒绝广播、断线只影响该台；断线清空名单并作废在途查询结果（连接代次）。
* **关闭码分流**：按官方表区分 Resume / 重新 Identify / 终止，避免无休止重连。
* **错误脱敏**：日志与 `status` 只记异常类型名与 `http_<status>_code_<code>`，抹掉 `Bearer`/`QQBot ` 头、
  URL userinfo、`token=`/`password=` 形态；上游 `message` 不入日志。
* **配置严格化**：未知字段、隐式类型转换（`enabled: "yes"`、`port: true`）直接拒绝，错误只给字段路径。
* **进程加固**：systemd 单元 `NoNewPrivileges` / `PrivateTmp` / `ProtectSystem=full`，以 `User=czauw` 运行。
* **不下载附件**：图片/文件只显示占位符。

## 仍需部署层承担

1. **传输加密**：桥接器→QueQiao 默认 `ws://` 明文（`tls: true` 才走 `wss://`）；跨主机部署应加 TLS 或私网隧道。
2. **网络 ACL**：WS 的 `access_token` 是鉴权不是网络隔离。本机四台实例的 8091–8094 目前由 Docker 发布在
   `0.0.0.0`（UFW 未启用），只有 token 挡着。建议在端口映射上加 `127.0.0.1:` 前缀——**这是待实施建议，尚未生效**。
3. **RCON 暴露面**：RCON 在容器内由 QueQiao 连（25581–25584），不要为调试映射到公网。
4. **状态端口**：`/api/v1/status` 无鉴权，含服务器名、人数与错误种类；只监听 loopback 或由反代加鉴权。
5. **密钥保存与轮换**：`.env` 权限 600、不进仓库；轮换时 QueQiao 侧 token 与桥接器同步改。
   排查泄漏只列文件名（`grep -rl`），不要打印命中行。
6. **入站刷屏**：桥接器对群消息没有按用户限速，群内任何人都能把消息放大到所有服务器。
   需要时在群权限、QueQiao 侧或前置反代加限制；这属于产品策略，不在当前实现内。

## 剩余风险与已知限制

* 去重与 Resume 只覆盖有限时间/序号窗口；进程重启、平台超时不保证消息不丢，也不保证恰好一次。
  没有持久消息账本、没有日配额强制预算、没有聚合摘要——早期研究里的这些建议都未实现。
* `event_workers` 默认 4，同一时刻到达的多条群消息发送顺序不保证；需要严格顺序设 1。
* `bot_state` 探测失败或 `11253` 时降级为 `unknown`：仍会尝试主动发送，并在 `status`/`readyz` 暴露 degraded；
  只有官方明确 `allow_proactive_msg=false` 才硬性禁止。
* 主动推送同时受群主开关、Bot/单关系 qpm、每群每日 1000 条、内容审核约束；配置绕不过审核。
* 昵称兜底不是真群名片；只有 `content`+`attachments` 时占位符统一追加在文本后，无法还原原始交错顺序。
* `list` 输出解析覆盖常见中英文格式，不保证覆盖所有本地化/插件改写；超长玩家名会被截断。
* 回环抑制延迟超过窗口时仍可能漏一次自激。

## 与 Hermes / LLM 的关系

默认入口 `python -m mcqq_bridge`（`runtime.py`）**不装配 Hermes**：只有 config / manager / qq_official / router。
`src/mcqq_bridge/hermes.py` 与 `settings.py` 里的 `hermes_*` 字段只被旧 OneBot 入口 `main.py` 引用
（`hermes_admin_enabled` 默认 false），默认路由上不可启用。

将来若要接入，必须：独立授权与身份映射（群管理员不自动等于主机执行权限）、独立最小权限凭据、
只经私网访问、明确拒绝「QQ 文本 → shell/CLI」这类执行接口。Hermes API server 自带终端等工具，
因此其 API key 必须独立、最小范围、只经受控私网。