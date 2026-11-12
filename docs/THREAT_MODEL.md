> **⚠️ 已过时（2026-11-11）**：本文对应旧 OneBot 时代的信任边界。新版本的边界见根目录 `README.md` §9
> （官方 QQ 接口、`x-self-name` 握手、QueQiao 官方 `send_rcon_command list` 只读查询、无 shell/任意 RCON）。

# 威胁模型与已知限制

信任边界：MC 事件源、OneBot 实现、callback 消费者与运维配置属于受信任组件；群成员/MC 聊天文本、外网 HTTP 请求均不可信。默认核心不会把文本交给 shell、RCON 或 Hermes。

## 资产与攻击面

- 资产：群消息权限、MC callback 访问权限、内存状态、adapter token。
- 入口：MC Bearer webhook、OneBot HTTP HMAC/Bearer、OneBot reverse WS、只读 state/health/docs API。
- 出口：固定 OneBot `send_group_msg`、固定 MC chat webhook；没有任意 API action 或命令透传。

## 已实现控制

- MC token 默认必需；OneBot HTTP 至少配置一种鉴权，否则拒绝；WS 必须启用 OneBot 且 token 非空，握手 Bearer 校验。比较使用常量时间函数。标准 OneBot v11 HTTP POST HMAC SHA1 用于兼容协议，不应用于新的密码存储方案。
- group allowlist 在业务入口和发送 adapter 重复检查；空表不激活任何群。
- `/mc` 必须为完整首 token；只执行固定的 `players/status/say` 处理器，即使配置加入 `shell` 等词也不会获得执行能力。查询读取缓存，不执行 MC 控制台命令。
- 过滤 `user_id == bot self_id` 的群事件；不把所有包含 self_id 的事件误当成自己消息。MC→QQ 只发纯文本 (`auto_escape=true`)，防止 MC 文本注入 CQ at-all/image 等动作。QQ 数组消息只接受文本段。
- MC 文本、名称、列表有 Pydantic 长度限制；HTTP 正文流式累计上限 64 KiB，WS 正文检查 64 KiB，uvicorn 启动参数也设置 WS frame 上限。WS 每连接并发事件与待确认 API 请求上限 32。未知/错误 JSON 不执行动作。
- WS action echo 与返回 retcode 关联，不把发出帧等同成功；超时、断线 fail closed，拒绝重复 role 连接，避免多连接广播重复发送。
- HTTP 请求有超时、不跟随重定向；URL 只能由运维配置，不接受群消息指定 URL。错误日志不输出 token、消息正文或外部异常 URL。
- 容器非 root、只读、无 capabilities、no-new-privileges，仅 loopback 绑定；构建上下文排除 .env/.git/.venv。
- Hermes 默认关闭。开启后只接受群 allowlist + `HERMES_ADMIN_IDS` 的 QQ 号，使用显式 `API_SERVER_KEY` 调用已有 Hermes 的官方 OpenAI-compatible HTTP API；有超时、并发、输入/输出长度上限，失败不泄漏上游错误。桥接器不启动 Hermes、不读取 profile、不复制 profile 凭据、不执行 shell/CLI。由于 Hermes API server 具备终端等工具，API key 必须独立、最小范围、仅通过 secret 注入，并且只能经受控私网访问。

## 仍需部署层控制

- TLS（跨主机 HTTPS/WSS）、防火墙/私网、入站请求速率限制、HTTP/WS 连接数、反向代理 body read timeout 和全局资源预算。正文上限不是 DoS 的完整防护。
- 只读状态 API 和 OpenAPI 默认无鉴权，可能暴露玩家名/版本；请仅内网开放，或由反向代理保护所有路由。健康探针不包含秘密。
- HTTP HMAC 不提供时间窗/nonce，重放未全局去重；不要双路同时上报同一事件。MVP 无持久消息队列/自动重试，进程重启、断线可能丢消息。可靠投递需要幂等键+持久 outbox，不应宣称 exactly-once。
- token 被盗或 OneBot/MC 源被攻陷后，可在已授权边界内伪造事件/刷屏；应轮换独立 token、限制来源 IP，使用专用 bot 账号。
- callback 消费者必须防命令注入/富文本注入；本服务的纯文本格式并不自动保证第三方消费者安全。
- MC 状态来自 webhook 内存快照，不是主动探测；单 worker/replica 部署，不适合未配置共享存储的水平扩容。
- 不提供 QQ 登录/防封保证，不替代官方 QQ bot 合规评估；外部 OneBot 软件与账号风险由部署者评估。

## 后续扩展的安全契约

LLM provider 只输出文本候选，默认关闭、概率默认 0，不得授予管理工具。Hermes adapter 只能调用显式配置的 API endpoint；禁止“QQ 文本 → shell/Hermes CLI”的执行接口。生产还应补充外部审计、群/管理员变更审计和更细的任务策略。
