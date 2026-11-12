> **历史归档（2026-11-11）**：本文件是旧 OneBot MVP 的验证记录，不代表本轮官方 QQ/QueQiao 能力。
> 本轮验证见 `docs/VERIFICATION.md`。

# 验证记录

在仓库目录 `/home/czauw/minecraft-qq-bridge` 执行：

1. `.venv/bin/python -m pytest -q -W error`
   - 结果：53 passed in 0.63s（warnings 作为错误处理）。
   - 覆盖：MC 六类事件模型与认证、通知开关、状态缓存 freshness、OneBot HTTP/HMAC/Bearer、反向 WebSocket Universal/API/Event、echo/retcode 确认、超时/重复连接/输入上限、群 allowlist、命令 allowlist、QQ→MC callback、Hermes 默认关闭、管理员/群鉴权、可控替身回复、官方 API payload/Bearer、上游失败和并发上限。
2. `.venv/bin/python scripts/smoke.py`
   - 结果：`SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only`。
   - 这是启动真实 Uvicorn TCP 服务、真实 HTTP 请求和本地模拟 OneBot/MC peer；没有真实 QQ 登录、OneBot 实现或 Minecraft 服务参与。
3. `git diff --check`
   - 结果：无空白错误。
4. Docker 构建/Compose 启动
   - 当前执行环境的 czauw 不在 docker 组，裸 `sudo docker version` 返回 `sudo: A terminal is required to authenticate`，因此本会话无法完成需要 root 的 Docker daemon 构建；没有改用 rootless Docker 或伪造结果。
   - 可在具备 sudo 终端的机器执行：`sudo docker compose up -d --build`，然后 `curl -fsS http://127.0.0.1:8000/healthz`。

5. Hermes 真实实例接口核验
   - 官方文档核实为 OpenAI-compatible `POST /v1/chat/completions`，Bearer `API_SERVER_KEY`；API server 默认关闭且独立于 dashboard。
   - 本机已有 Hermes dashboard/gateway 在运行，但 `127.0.0.1:8642/v1/models` 无监听，且未检测到 `API_SERVER_*` 配置；没有复制 profile 凭据、没有改 profile、没有猜测 9119 内部接口。
   - 结论：真实 Hermes 端到端调用尚未验证，阻碍是现有实例未启用/暴露可安全访问的 API server；桥接器会安全失败并报告未投递，不伪造回复。

## 独立审查后的回归修复

审查快照中的文本段、join/leave、OpenAPI schema、依赖清单不一致已修复并通过测试。另新增 `tests/test_review_regressions.py`：异常数字/浮点/布尔 ID 稳定拒绝，非 ASCII 签名返回 401，WS 异常输入不阻断后续事件；查询分别报告 accepted/delivered；HTTP/WS 失败 retcode 发出无凭据告警；增加可配置 TTL、unknown/fresh/stale 和更新时间，offline 清空旧名单；WS 子任务异常被读取并安全记录。所有最终结果均来自上述完整测试与真实 TCP smoke，而非审查中间快照。修复后的版本未取得第二次独立审查通过结论。

## 协议参考

- OneBot v11 HTTP POST：<https://github.com/botuniverse/onebot-11/blob/master/communication/http-post.md>
- OneBot v11 反向 WebSocket：<https://github.com/botuniverse/onebot-11/blob/master/communication/ws-reverse.md>
- OneBot v11 鉴权：<https://github.com/botuniverse/onebot-11/blob/master/communication/authorization.md>
- OneBot v11 `send_group_msg`：<https://github.com/botuniverse/onebot-11/blob/master/api/public.md>
