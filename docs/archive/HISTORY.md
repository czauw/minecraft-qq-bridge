# 历史摘要（不是部署指南）

本文件只记录三个阶段的来龙去脉与当时的结论，供追溯用。**部署与运行请看根目录 `README.md`。**
原文已从工作树删除，可从基线提交取回：

```bash
cd /home/czauw/minecraft-qq-bridge
git show ae93e5a:docs/LEGACY_ONEBOT_README.md      # 任一被删文件都可用同样方式找回
git show ae93e5a:docs/QQ_BOT_INTEGRATION.md
```

## 阶段一：OneBot v11 基线（2026-11-09）

* 内容：MC 六类 HTTP 事件模型、Bearer 鉴权、状态 TTL 缓存、OneBot HTTP/反向 WS 出站、`/mc players|status`、
  默认关闭的 Hermes 管理调用；Docker compose 用 loopback 发布。
* 验证：`tests/test_bridge.py` 等回归 + `scripts/smoke.py`（原文
  `SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only`）。
* 适用：已废弃路线。协议是非官方的，需要第三方 QQ 登录实现承风险；代码保留但不在默认入口。

## 阶段二：官方 QQ 文档设计与独立复审（2026-11-10）

* 卡：现状 `t_5464e0c4`、平台架构 `t_80913f24`、接入与配置文档 `t_ed9ec500`、修复 `t_8bd76e90`、
  独立审查 `t_863b01da`（最终 `verdict=PASS`）。
* 产出：`QQ_BOT_INTEGRATION.md`（433 行）、`QQ_BOT_CONFIGURATION.md`（710 行）与六份研究文档；
  平台资料查阅日期 2026-11-10（WebSocket 与 Webhook 均可、token 语义、群身份 openid、被动 5 分钟/5 次、
  主动配额、群主权限开关）。
* 当时的阻断项（已修）：安全示例里的 `<NUMERIC_GROUP_ID>` 占位符无法被真实 `Settings` 加载；
  泄漏排查命令 `grep -rn` 会把密钥打到终端——改为 `grep -rl` 只报文件名。
* 局限：本阶段没有实现官方 adapter、没有凭据、没有部署，也没证明真实 QQ↔MC 联通；
  `QQ_OFFICIAL_*` 等键当时就是草案，写进环境也不生效。

## 阶段三：官方 × QueQiao 实现与队列修复（2026-11-11）

* 卡：实现 `t_b49922d8`、首轮 9 项 Major 修复 `t_73ee0162`、Gateway 队列溢出修复 `t_b857e19a`。
* 结果：当前架构落地（严格 YAML、官方 token/Gateway、每服独立 QueQiao 连接、manager/router、
  只读状态接口）；当时 `228 passed` + `SMOKE PASS: 10/10`，均为模拟 peer。
* 该阶段的关键修复保留为回归测试：关闭码分流、连续完成才推进恢复水位、队列满主动 Resume 且事件不进去重表、
  广播前登记回环指纹、查询期间 join/leave 合并与代次作废、`bot_state` 降级与冷却、坏 JSON 不算投递成功。
* 教训（写文档时仍适用）：**「不可读」不等于「不存在」**（没权限看目录时不能断言全机没有任何组件）；
  安全扫描只报文件名不报值；模拟测试不能代替真实联调结论。

## 与当前文档的关系

* 当前能力、边界、命令：`README.md`；逐项证据与来源：`docs/VERIFICATION.md`。
* 本机四实例的事实：`docs/MCSMANAGER_DEPLOY.md`；威胁模型：`docs/THREAT_MODEL.md`。
* 旧文档里的 `228 passed`、`10/10`、`53 passed` 都是各自时点的输出，不是当前结果；
  其中 `资源服 未启用` 之类的 `/list` 文本已因「禁用项完全不展示」的实现变更而失效。