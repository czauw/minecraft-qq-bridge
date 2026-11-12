# 官方 QQ 群机器人迁移：现有项目盘点与基线验证（as-is）

- 文档性质：**现状盘点（as-is）＋ 基线实测记录**。不含实现方案、排期、部署动作或账号申请结论。
- 盘点对象：`/home/czauw/minecraft-qq-bridge`（kanban 工作区 `dir`，git 分支 `master` **无任何提交**，`git status` 全部为 untracked）。
- 生成时间：2026-11-10T04:02Z（UTC）。宿主：`Linux 7.0.0-38-generic`（Ubuntu），当前用户 `czauw`。
- 方法与约束（本轮实际遵守）：
  - 只读代码/测试/文档，运行现有测试与 smoke，做只读的主机与端口探测；**唯一写入的文件是本文件**，未改代码、未改其他文档、未 reset/clean/commit。
  - 未读取、未输出任何真实 `.env`/凭据。实测确认：仓库根目录**不存在 `.env`**（`ls .env` → No such file or directory），只有 `.env.example`；`/etc/caddy/dns.env`（0600）未读取。
  - 未申请、未登录任何 QQ 账号；**未调用任何官方 QQ Bot 接口**（无 AppID/AppSecret）。官方平台结论一律沿用姊妹文档并标注等级。
- 与既有研究材料的关系：同目录下已存在并行产物 `QQ_BOT_CURRENT_STATE.md`（mtime 2026-11-10T03:58Z）、`QQ_BOT_PLATFORM_FINDINGS.md`（03:54Z 获取官方页面，04:01Z 有更新）与 `official-qq-platform-and-architecture.md`（04:03Z）。本文件不覆盖它们，定位为**独立复现 + 主机/运行时层面新增证据**；与它们结论一致处不重复论证，差异处显式标注。

---

## 1. 结论摘要

1. **现有代码是"无 mod 的 FastAPI 单进程协议网关"**：MC 侧入站 HTTP webhook（Bearer），QQ 侧 OneBot v11（HTTP API 出站 + HTTP POST / 反向 WebSocket 入站）。12 个源文件 / 1653 行（含测试与 smoke，`wc -l` 实测）。
2. **当前没有任何真实的 MC↔QQ 群服联通**。全仓库不存在官方 QQ Bot 协议字段（`group_openid`/`member_openid`/`msg_id`/`msg_seq`/`intents`/`QQBot`/`Ed25519` 关键词命中数**全为 0**，`grep` 实测），也不存在 QQ 登录实现。所有"通过"的证据均来自 TestClient / HTTP mock / 本地模拟 peer。
3. **桥接器当前未运行**：`127.0.0.1:8000` 无监听、无 uvicorn 进程、无 systemd unit；仓库无 `.env`，因此"当前生产配置"实为代码默认值（第 5.2 节逐项实测）。
4. **Minecraft 侧的真实存量只有"服务器本体 + 面板"**：本机确实在跑 2 个 Java 实例（Forge `1.20.1-47.4.16` 与一个 `server.jar`，均 root 身份）、监听 `25565`/`25567`，并由 MCSManager 面板（`127.0.0.1:23333`，Caddy 反代 `panel.minecraft.ichenchen.cn:8443`）管理。但**没有任何组件把 MC 事件推给本仓库、也没有任何组件消费本仓库的 callback**——MC 侧对接件 100% 缺失，且 `/opt/mcsmanager` 对 `czauw` 不可读，实例级配置无法从当前权限核实。
5. **迁移的关键断点不在业务逻辑，而在接入层**：鉴权头（`Bearer` → `QQBot <access_token>`）、身份模型（`int` 群号/QQ 号 → 字符串 `group_openid`/`member_openid`）、入站验签（HMAC-SHA1 → Ed25519）、WS 方向（本服务是反连服务端 → 官方网关要求本服务作为客户端外出连接）、出站语义（无 `msg_id` → 被动回复 5 分钟/5 次 + `msg_seq` 去重）。
6. **可复用面**：`state.py`、`mc_adapter.py`、`service.py` 的编排/格式化、`models.py`、`hermes.py`（改类型）、`security.py` 的 Bearer 比较（仅限 MC/callback 入口）。**必须替换面**：`onebot.py` 全部、`main.py` 的三条 OneBot 路由、`settings.py` 的 `onebot_*` 段与 `int` 归一化、`ports.py` 的 `OutboundOneBot` 契约、`scripts/smoke.py`。
7. **本轮最重要的语义风险**（与并行盘点 R1 同结论，本轮从代码再确认）：`service.py:29-34` 对**每个 MC 事件 × 每个 allowlist 群**各发一条消息，且**忽略发送结果**（`send_group_message` 的 bool 返回值未被使用）。在官方平台这既是主动消息配额问题，也是"MC 侧永远拿到 202、无从得知 QQ 是否真的收到"的可观测性问题。
8. **测试基线是绿的但只对 OneBot 有效**：`pytest -q -W error` = **53 passed in 1.19s**；`scripts/smoke.py` = **SMOKE PASS**（本地模拟 peer）。绿灯**不能**外推为"官方 Bot 可用"，也不能外推为"真实群服联通"。

---

## 2. 仓库与运行形态（实测）

### 2.1 文件与规模

```
src/mcqq_bridge/  __init__.py(3) hermes.py(95) mc_adapter.py(34) main.py(182)
                  models.py(54) onebot.py(136) personality.py(12) ports.py(25)
                  security.py(10) service.py(138) settings.py(76) state.py(60)
tests/            test_bridge.py(150) test_features.py(103) test_hardening.py(99)
                  test_hermes_admin.py(177) test_review_regressions.py(80)
                  test_security.py(17) test_websocket_safety.py(82)
scripts/          smoke.py(120)
docs/             DEPLOYMENT.md THREAT_MODEL.md VERIFICATION.md
                  research/QQ_BOT_CURRENT_STATE.md research/QQ_BOT_PLATFORM_FINDINGS.md
                  research/official-qq-platform-and-architecture.md research/official-qq-current-state.md(本文件)
根目录            README.md config.example.yaml .env.example pyproject.toml
                  requirements.txt Makefile Dockerfile docker-compose.yml
```

- 运行时解释器：`.venv/bin/python` = **Python 3.14.9**（实测 `-V`）。`pyproject.toml:10` 声明 `>=3.11`，`Dockerfile:1` 基础镜像 `python:3.12-slim`——三处不一致（低风险，但加"对 Python 版本敏感的密码学依赖"前必须先统一）。
- 依赖（`pyproject.toml:11-16`，全部锁死）：`fastapi==0.162.2`、`httpx==0.28.1`、`pydantic-settings==2.16.2`、`uvicorn[standard]==0.54.0`；测试段另有 `pytest==8.4.2`、`pytest-asyncio==1.4.1.post1`、`httpx2==2.16.0`。
  - 实测 `.venv/bin/python -m pip list`：上述包均在位；**无 `cryptography`、无 `pynacl`**（官方 Webhook 的 Ed25519 验签需要新增依赖）。
  - `httpx2` 已安装但**零引用**（`grep -rniI httpx2 src tests scripts --include=*.py | wc -l` = 0），属冗余依赖。

### 2.2 装配与进程模型

- 入口：`src/mcqq_bridge/main.py:182` 模块级 `app = create_app()`；`create_app()`（`main.py:32-39`）装配两个 OneBot 适配器、一个 MC callback、Hermes executor 与 `BridgeService`。
- 传输二选一：`main.py:36` 按 `settings.onebot_transport`（`http|websocket`）选实例；**两条路径的入站路由同时存在**（`main.py:100-122` 与 `main.py:124-165`）。
- 路由清单（`grep -nE '@app\.(get|post|websocket)'` 实测）：
  - `GET /healthz`（`main.py:72`）、`GET /readyz`（`main.py:76`）
  - `POST /api/v1/minecraft/events/{event_type}`（`main.py:80`）
  - `POST /api/v1/onebot/events`（`main.py:100`）
  - `WS <ONEBOT_WS_PATH>`（默认 `/api/v1/onebot/ws`，`main.py:124`）
  - `GET /api/v1/state/players|status|meta`（`main.py:167/171/175`）
- 状态仅存进程内存（`state.py:9-18` dataclass），无数据库/outbox/审计；`docs/DEPLOYMENT.md:58` 明确建议单 worker。

### 2.3 部署工件

- `Dockerfile`：非 root（`Dockerfile:12-13`）、`--ws-max-size 65536`、HEALTHCHECK 打 `/healthz`。
- `docker-compose.yml`：`env_file: .env`、端口**仅** `127.0.0.1:8000:8000`、`read_only: true`、`cap_drop: ALL`、`no-new-privileges`、tmpfs `/tmp`。
- 现状：**caddy.service / docker.service / containerd.service 均在运行**（`systemctl` 实测），但 `czauw` 不在 docker 组且无免密 sudo（`docker ps` → permission denied；`sudo -n true` → "interactive authentication is required"），因此本会话**无法**核实是否存在该 compose 的容器实例。

---

## 3. 现有链路与能力表

### 3.1 拓扑（代码事实）

```
MC 事件源(本仓库之外, 缺失)
   │ POST /api/v1/minecraft/events/{chat|death|join|leave|players|status}   Bearer MC_WEBHOOK_TOKEN
   ▼
bridge (FastAPI, 单进程)
   ├─ BridgeService.handle_mc_event  → 更新内存状态 + 按开关格式化文本
   │      └─ for group_id in ALLOWED_GROUP_IDS: onebot.send_group_message(...)   ← 每事件×每群一条
   ├─ BridgeService.handle_onebot_event  ← OneBot 入站(group message, /mc 前缀)
   │      ├─ players|status → 读内存快照回帖
   │      ├─ say → POST MC_CALLBACK_URL (Bearer MC_CALLBACK_TOKEN)
   │      └─ hermes → POST {HERMES_API_URL}/chat/completions (Bearer HERMES_API_TOKEN)
   ▼
OneBot 实现(本仓库之外, 负责 QQ 登录/风控)  ← HTTP API 或 反向 WS
   ▼
QQ 群
```

### 3.2 能力表（"实体"= 有可执行代码；"占位"= 接口存在但恒拒绝/恒空）

| 能力 | 状态 | 证据（文件:符号:行） |
|---|---|---|
| MC 六类事件入站 + Pydantic 长度上限 | 实体 | `main.py:22-29,80-98`；`models.py:6-45` |
| MC webhook Bearer 鉴权（可关闭） | 实体 | `main.py:68-70`；`security.py:6-10`；开关 `settings.py:19` |
| 请求体 64 KiB 上限（流式累加） | 实体 | `main.py:60-66`（超限 413，测试 `test_oversized_body_rejected`） |
| MC→QQ chat/death/join/leave 四开关 | 实体 | `service.py:29-45`；`settings.py:21-23` |
| MC→QQ 广播到**所有** allowlist 群 | 实体 | `service.py:33-34` |
| MC→QQ **投递结果被丢弃** | 已确认缺陷性特征 | `service.py:31-34` 未使用 `send_group_message` 返回值；MC 入口恒 202（`main.py:98`） |
| 内存状态 + unknown/fresh/stale + offline 清名单 | 实体 | `state.py:20-60`；`main.py:167-177` |
| QQ→MC `/mc players` `/mc status` | 实体 | `service.py:78-83`（含"未知/过期/缓存快照"前缀） |
| QQ→MC `/mc say`（双开关 + 固定回调） | 实体 | `service.py:84-91`；`mc_adapter.py:22-34`；`settings.py:36-40` |
| 命令白名单 `MC_COMMANDS` | 实体 | `service.py:76-77`（默认 `players,status`，`settings.py:39`） |
| `/mc` 必须是完整首 token | 实体 | `service.py:69-73`（`/mcevil` 被拒，`test_exact_prefix_required`） |
| 群 allowlist（入口 + 发送二次校验） | 实体 | `service.py:55-56`；`onebot.py:28,115` |
| bot 自身消息过滤 | 实体 | `service.py:57-59`（`ONEBOT_SELF_ID` 或事件 `self_id`） |
| OneBot HTTP 出站（`send_group_msg`，纯文本） | 实体 | `onebot.py:27-48`（`auto_escape: True`） |
| OneBot 反向 WS 服务端（Universal/API/Event，echo 确认，超时 fail-closed） | 实体 | `onebot.py:51-136`；`main.py:124-165` |
| OneBot 入站 HTTP HMAC-SHA1 验签 | 实体 | `main.py:109-117`；`re.fullmatch(r"sha1=[0-9a-f]{40}")` |
| Hermes 管理员通道（OpenAI 兼容） | 实体但默认关闭 | `hermes.py:26-95`；`service.py:97-113`；`settings.py:42-51` |
| LLM 人设 | **占位**（恒 `None`） | `personality.py:6-12`；`settings.py:52-54` 三个字段**零引用** |
| QQ 账号登录 / 官方 Bot 接入 | **不存在** | 全仓库 `group_openid`/`QQBot`/`msg_id` 等关键词 0 命中；`README.md:3,156` |
| MC 插件/mod/日志 sidecar/RCON | **不存在** | `docs/DEPLOYMENT.md:5`；源码无 Minecraft 协议/日志解析代码 |
| 持久化 / 重试 / 队列 / 审计 | **不存在** | 全仓库无 DB/队列依赖；`docs/THREAT_MODEL.md:27` |
| 用户绑定 / 多服务器路由 / 富媒体 | **不存在** | 无相关模型与路由 |

### 3.3 入站命令语义（实测行为，不止读代码）

在"允许群 + 正确 webhook token"前提下，用 `TestClient` 直接探针（脚本见 §12.3）：

| 输入 | 结果（实测） |
|---|---|
| `/mc players`，`ONEBOT_ENABLED=false` | `202 {"accepted": true, "delivered": false}` —— **入站仍然处理，只有出站被拒** |
| `/mc shell whoami` | `202 {"accepted": false, "delivered": false}` |
| `message_type=private` 的 `/mc players` | `202 {"accepted": false, "delivered": false}` |
| `GET /api/v1/state/players` 无凭据 | `200`（状态 API 无鉴权） |
| MC webhook 无 token / 错 token | `401` / `401` |

> 迁移注意：官方平台没有"群号 int"也没有 `message_type=group`。这套语义必须在**事件规范化层**重建，不能靠改字段名。

---

## 4. 实测 / 未验证 分栏

### 4.1 实测（本轮真实执行，可复现）

| # | 项目 | 命令 | 环境 | 结果 |
|---|---|---|---|---|
| E1 | 单元/集成测试 | `.venv/bin/python -m pytest -q -W error` | `.venv` Python 3.14.9，pytest 8.4.2，`asyncio_mode=auto` | **53 passed in 1.19s**（warnings 视为 error） |
| E2 | 测试收集 | `.venv/bin/python -m pytest --collect-only -q` | 同上 | **53 tests collected**；分布：test_bridge 6、test_features 10、test_hardening 11、test_hermes_admin 6、test_review_regressions 12、test_security 2、test_websocket_safety 6 |
| E3 | TCP smoke | `.venv/bin/python scripts/smoke.py` | 真实 uvicorn 进程（随机端口 127.0.0.1:50631）+ 本地模拟 OneBot/MC peer | **`SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only`**，EXIT=0 |
| E4 | `.env.example` 可加载 | 包含在 E1（`test_env_example_loads_without_validation_error`） | `Settings(_env_file=.env.example)` | 通过：`allowed_group_ids==[123456789]`、`mc_commands==['players','status']`、`onebot_self_id is None` |
| E5 | 默认配置实测 | 直接 `Settings(_env_file=None)` 打印 | Python 3.14.9 | 见 §5.2 表（全部为安全默认：onebot 关、群白名单空、token 空） |
| E6 | 入站语义探针 | `TestClient` + 手工构造 OneBot 事件 | 同上 | 见 §3.3 表 |
| E7 | 协议关键词缺失 | `grep -rniI <kw> src tests scripts --include=*.py \| wc -l` | — | `group_openid`=0、`member_openid`=0、`app_access_token`=0、`Ed25519/ed25519`=0、`msg_id`=0、`msg_seq`=0、`intents`=0、`QQBot`=0 |
| E8 | OneBot 耦合规模 | `grep -rniI onebot src scripts tests --include=*.py \| wc -l` | — | 总 **135**（src 65、tests 63、scripts 7） |
| E9 | 桥接器未运行 | `ss -ltn` / `pgrep -af uvicorn` | 宿主 | 无 `:8000` 监听；无 uvicorn 进程；无 `bridge/mcsm/qq` 相关 systemd unit |
| E10 | 依赖实测 | `.venv/bin/python -m pip list` | — | fastapi 0.162.2 / httpx 0.28.1 / httpx2 2.16.0 / pydantic 2.14.3 / pydantic-settings 2.16.2 / uvicorn 0.54.0 / pytest 8.4.2 / pytest-asyncio 1.4.1.post1；**无 cryptography/pynacl** |
| E11 | Hermes 官方 API 不可用 | `curl -s --max-time 3 http://127.0.0.1:8642/v1/models` | 宿主 | 连接失败（curl exit 7，HTTP code 000）→ 无监听 |
| E12 | 主机 MC 存量 | `ss -ltn` / `ps` | 宿主 | `25565`、`25567` 监听；java PID 1012999（`-jar server.jar`，root，运行 1 天 1 小时）、PID 1063703（`@libraries/net/minecraftforge/forge/1.20.1-47.4.16/unix_args.txt`，root，运行 15 小时 50 分） |
| E13 | MC 面板 | `curl 127.0.0.1:23333/`、`/api/overview` | 宿主 | `http=200`；`/api/overview` 返回 `{"status":403,"data":"权限不足"}` → 面板在位且需鉴权；MCSManager 进程为 2 个 `node app.js --max-old-space-size=8192`（root，PID 116151/116275） |
| E14 | 面板 TLS 入口 | `/etc/caddy/Caddyfile:30-36` | 只读 | `panel.minecraft.ichenchen.cn:8443` → `reverse_proxy 127.0.0.1:23333`（同文件另有 cockpit→9090、hermes→9119 两条） |
| E15 | 目录权限边界 | `ls /opt/mcsmanager`、`ls /var/lib/docker` | — | 均 `Permission denied` → MCSManager 实例目录与容器元数据对本会话不可读 |
| E16 | docker 不可用 | `docker ps`；`sudo -n true` | — | `permission denied ... /var/run/docker.sock`；`sudo: interactive authentication is required` |
| E17 | git 状态 | `git log`、`git status --short` | — | `fatal: ... 'master' 尚无任何提交`；全部文件 untracked |
| E18 | 无 `.env` | `ls -la .env` | 仓库根 | No such file or directory |

### 4.2 未验证（明确未做 / 无法做，不得当作已知）

| # | 项目 | 原因 |
|---|---|---|
| U1 | Docker 构建与 `compose up` | `czauw` 不在 docker 组且无免密 sudo；本会话不能弹出交互式密码。**未尝试绕过**（未用 rootless Docker、未改权限）。 |
| U2 | 是否存在运行中的 bridge 容器 | 同上（E15/E16）。仅能确认宿主 `127.0.0.1:8000` 无监听。 |
| U3 | 真实 QQ 登录 / 任一 OneBot 实现（NapCat/AstrBot/LLOneBot/QueQiao） | 本机 home 下未发现相关安装目录；`pgrep` 无相关进程；且本轮禁止登录第三方账号。 |
| U4 | 真实 MC → bridge 推送 | 没有任何 MC 侧插件/sidecar；`grep` 在可读目录（`~/scripts`、`~/桌面`、`~/文档`）未命中 `api/v1/minecraft/events` 或 `25580` → **无对接件**。 |
| U5 | 真实 bridge → MC callback 消费 | 默认 `MC_CALLBACK_URL=http://127.0.0.1:25580/bridge/qq`，实测该端口**无监听**（`ss` 无 25580）。 |
| U6 | 真实 Hermes API 端到端 | `127.0.0.1:8642` 无监听（E11）。本机仅 Hermes dashboard（`0.0.0.0:9119`）。 |
| U7 | 官方 QQ Bot 任意接口 | 无 AppID/AppSecret，未申请账号，未调用 `api.bot.qq.com`。平台结论全部来自官方文档（见 §10，等级 O1/U1/U2）。 |
| U8 | MC 实例的版本/在线人数/mod 列表 | 25565 的 status ping 返回的是 disconnect 包（原始 JSON：`{"translate":"disconnect.genericReason","with":["Internal Exception: io.netty.handler.codec.DecoderException: java.lang.IndexOutOfBoundsException: Index 763 out of bounds for length 1"]}`），25567 探测直接 EOF；实例目录/配置在 `/opt/mcsmanager`（root-only，E15）。**没有把猜测写成结论**。 |
| U9 | `GROUP_MESSAGE_CREATE`（群全量消息）可得性 | 官方索引提及但资质/权限未核实（平台等级 U1，见姊妹文档），本轮未新增证据。 |
| U10 | 独立审查结论 | `docs/VERIFICATION.md:24` 自述"修复后的版本未取得第二次独立审查通过结论"；本轮只复现测试，不做代码审查。 |

---

## 5. 运行时真实配置来源

### 5.1 代码里的唯一加载路径

- `settings.py:10-11`：`class Settings(BaseSettings)`，`SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")`。
- 即：**环境变量 > 当前工作目录下 `.env` > 类内默认值**（pydantic-settings 语义）。模块级实例在 `settings.py:76` 创建，`main.py:32-33` 在 `create_app()` 未传参时使用它。
- `config.example.yaml` **不参与运行时**：README 已如实声明（`README.md:136`），并且 `grep -rniI yaml src scripts tests --include=*.py` **零命中**——代码里没有任何 YAML 解析。
- 部署侧注入点：`docker-compose.yml` 的 `env_file: .env`（容器内 `/app/.env` 亦会被 pydantic-settings 读取，但 compose 已把变量注入进程环境，优先级更高）。

### 5.2 当前"实际生效配置"（实测，无 `.env` → 全默认）

来源：`Settings(_env_file=None)` 逐字段打印（E5）。这里只列安全相关字段，值均为占位/空，**不含任何真实凭据**。

| 字段 | 实测默认值 | 迁移影响 |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8000` | **两个字段在 src 中零引用**（`grep "\.host\b"`/`"\.port\b"` = 0）——实际监听地址只由 uvicorn CLI/Dockerfile CMD 决定 |
| `mc_webhook_require_token` / `mc_webhook_token` | `True` / `''` | 默认要求 token；但空 token 会让所有 MC 事件 401（fail-closed，安全） |
| `allowed_group_ids` | `[]` | 空表 = 不向任何群发送，也不激活任何入站群（fail-closed） |
| `onebot_enabled` / `onebot_transport` | `False` / `'http'` | 默认不产生真实 QQ 出站 |
| `onebot_api_url` | `http://127.0.0.1:3000` | 指向不存在的本地 OneBot（无监听） |
| `onebot_access_token` / `onebot_webhook_token` / `onebot_webhook_secret` | `''` / `''` / `''` | 入站无任何已配置凭据 → `POST /api/v1/onebot/events` 恒 401（`main.py:103-104`） |
| `onebot_self_id` / `onebot_ws_path` | `None` / `/api/v1/onebot/ws` | — |
| `mc_notifications` | `{chat:true, death:true, join:true, leave:true}` | 默认全开 |
| `qq_to_mc_enabled` / `mc_callback_url` / `mc_callback_token` | `True` / `http://127.0.0.1:25580/bridge/qq` / `''` | 开关开但目标无监听、无 token → 实际不可达 |
| `mc_commands` / `mc_say_enabled` | `['players','status']` / `False` | `say` 默认不可用 |
| `hermes_admin_enabled` / `hermes_admin_ids` / `hermes_api_url` / `hermes_api_token` | `False` / `[]` / `http://127.0.0.1:8642/v1` / `''` | 默认关闭；上游也未监听（E11） |
| `state_ttl_seconds` | `120.0` | 缓存时效阈值 |
| `llm_*`（3 个字段） | `False` / `''` / `0.0` | **src 零引用**（E7 同类 grep = 0）：纯占位 |

> 结论：当前若直接 `uvicorn mcqq_bridge.main:app` 启动，服务会以"MC 鉴权必失败、QQ 双向全关"的安全姿态起来——`/healthz` 通、业务不通。这解释了为什么"启动成功"不能当作联通证据。

### 5.3 配置项与代码的偏差（本轮新增发现）

| 字段 | 问题 | 证据 |
|---|---|---|
| `host`、`port`、`log_level`、`app_name` | 声明但从未被读取；`.env.example` 里给运维的提示会让人误以为能改监听地址/日志级别 | `grep "\.host\b"` 等 = 0；`settings.py:13-16` |
| `llm_enabled`、`llm_persona`、`llm_random_reply_probability` | 声明但从未被读取（`personality.py` 是恒 `None` 的占位） | `settings.py:52-54`；`personality.py:6-12` |
| `ONEBOT_WS_PATH` | 路由在启动时绑定（`main.py:124`），修改需重启；不是运行期可变 | `main.py:124` |
| `MC_WEBHOOK_REQUIRE_TOKEN=false` | 一旦关闭即**完全无鉴权**，任何能访问端口的人都能伪造 MC 事件 | `main.py:69` |
| `httpx2==2.16.0` | 已安装但零引用，引入第二套 HTTP 栈额外审计面 | `pyproject.toml:22`；E7 同类 grep = 0 |

---

## 6. Minecraft 侧：真正已有的 vs 缺失的

### 6.1 已有（主机层面，实测）

| 组件 | 实测证据 | 与本项目的关系 |
|---|---|---|
| Minecraft Java 实例 ×2（root） | PID 1012999 `java -Xms2G -Xmx6G -jar server.jar nogui`；PID 1063703 `java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.16/unix_args.txt nogui` | **无**：本仓库不与之交互 |
| 监听端口 | `0.0.0.0:25565`、`0.0.0.0:25567`（IPv4+IPv6） | **无** |
| MCSManager 面板/守护进程 | 2× `node app.js --max-old-space-size=8192`（root）；`127.0.0.1:23333` → 200，`/api/overview` → 403 权限不足 | **无**：本仓库没有 MCSManager 插件或 API 调用代码 |
| Caddy TLS 入口 | `/etc/caddy/Caddyfile:30-36`：`panel.minecraft.ichenchen.cn:8443` → `127.0.0.1:23333` | 潜在的部署复用点（官方 Webhook 需 HTTPS 与 80/443/8080/8443 之一），但**当前未为本项目配置任何站点** |
| 可读的 MC 相关残留 | `~/minecraftServer/createDelight/.mixin.out`（Mixin 输出目录，无 mods/config/日志） | 无 |
| 客户端侧 jar | `~/下载/MC26.2-Fabric-Server/fabric-server-mc.26.2-loader.0.20.1-launcher.1.1.2.jar`（下载未运行） | **不得**当作服务端组件使用（客户端/新版本，与在跑的 Forge 1.20.1 无关） |

### 6.2 缺失（这就是"未联通"的根因）

1. **事件源缺失**：没有任何 mod/plugin/日志 sidecar/面板 webhook 向 `POST /api/v1/minecraft/events/*` 推送。MCSManager 本身不产生逐条聊天/进退服/死亡事件；可读目录无相关配置或脚本。
2. **消费者缺失**：`MC_CALLBACK_URL` 默认指向 `127.0.0.1:25580`，实测**无监听**；没有任何进程消费 `{"event":"chat",...}`。
3. **实例级配置不可核实**：`/opt/mcsmanager` 权限 `drwxr-x--- root`（E15），因此"目标实例是否装了可用的桥接 mod（例如 QueQiao 类）"**无法从 `czauw` 权限判定**——不能因为看不到就断言不存在，也不能假设存在。
4. **无 RCON/控制台通道**：本仓库不实现 RCON；`docs/THREAT_MODEL.md:15` 明确"查询读取缓存，不执行 MC 控制台命令"。

### 6.3 对"是否已联通"的判定

**未联通，且缺的是两侧的对端。** 具体地说：

- MC 侧：服务器与面板是真的在跑（E12/E13），但**没有任何一个组件知道 bridge 存在**。
- 桥接侧：代码与测试是真的（E1–E3），但**当前没有进程、没有 `.env`、没有 OneBot 实现**（E9/E18/U3）。
- QQ 侧：**没有任何真账号或官方 Bot**（U3/U7）。

因此本仓库当前的能力上限是"**协议与编排逻辑已被本地模拟 peer 验证**"，不是"群服互通可用"。`docs/VERIFICATION.md:10` 也已如实标注"smoke 使用本地模拟 OneBot/MC peer"。

---

## 7. OneBot 耦合点清单（按文件/符号）

总命中 135 处（E8）。以下为必须处理的位置：

| 位置 | 符号 | 耦合性质 | 迁移动作 |
|---|---|---|---|
| `main.py:17,34-39` | `OneBotHttpAdapter` / `OneBotReverseWebSocketHub` / `onebot = ...` | 装配层硬编码两套 OneBot 适配器 | 改为"平台适配器"工厂；官方适配器独立文件 |
| `main.py:36` | `config.onebot_transport == "websocket"` | 二选一开关 | 官方平台下该维度变为"Webhook 回调 vs 网关 WS"，语义不同，需重新建模 |
| `main.py:78` | `readyz()` → `{"onebot_enabled": ...}` | 就绪信息泄露/误导 | 改字段并补真实就绪判据（token 是否取到、事件通道是否在连） |
| `main.py:100-122` | `onebot_event()` + HMAC-SHA1 验签 | OneBot 专属入站 | 新增官方回调路由（`op=0/12/13` + Ed25519）；旧路由下线或隔离 |
| `main.py:124-165` | `onebot_reverse_websocket()` | **反连服务端** + echo 关联 + 32 并发 + 子任务异常兜底 | 方向相反，整体作废；"发帧≠成功、必须等确认"的设计思想应保留 |
| `onebot.py:17-48` | `OneBotHttpAdapter.send_group_message` | `/send_group_msg` + `group_id:int` + `status/retcode` | 换 `POST /v2/groups/{group_openid}/messages` + `QQBot` 头 + `msg_id/msg_seq` |
| `onebot.py:51-136` | `OneBotReverseWebSocketHub`（connect/disconnect/close/resolve/send） | 单 bot role 去重、pending echo、超时 fail-closed | 不复用；改为网关客户端（Identify/Heartbeat/Resume/intents） |
| `service.py:51-68` | `post_type/message_type/group_id/user_id/message\|raw_message` 解析 | OneBot 事件形状 | 抽 `InboundEvent` 规范化层（群 openid、成员 openid、msg_id、内容） |
| `service.py:122-130` | `_int()`（拒绝 bool/float/非规范字符串） | 数字 ID 校验 | openid 是字符串，**不能**复用；需新增 openid 校验（长度/字符集/作用域） |
| `service.py:33-34` | `for group_id in allowed_group_ids: await send(...)` | 广播 + 忽略结果 | 官方平台需改为配额感知的聚合/队列 + 失败分类 |
| `settings.py:25-34` | `allowed_group_ids: list[int]`、`onebot_*` 共 11 个字段 | 配置模型 | 换 `qqbot_app_id/app_secret/webhook secret/callback 端口/openid 白名单` |
| `settings.py:68-73` | `normalize_groups()` → `int(item)` | 归一化 | openid 场景改字符串集合，且保留"空集合 = 不激活"语义 |
| `ports.py:6-7` | `OutboundOneBot.send_group_message(group_id:int, message:str)->bool` | 唯一协议接缝 | 扩契约：目标 openid、`reply_to(msg_id)`、`msg_seq`、结构化失败原因（配额/去重/非群成员/无主动权限） |
| `scripts/smoke.py:27-31,54-63` | 断言 `send_group_msg` 体与 `ONEBOT_*` 环境变量 | smoke 与 OneBot 绑死 | 重写；官方协议需另立向量与 smoke |
| `tests/*` 63 处 | `test_features.py:68-87`、`test_hardening.py:56-86`、`test_websocket_safety.py` 全部、`test_review_regressions.py:32-44` | OneBot 断言 | 迁移后替换；MC/状态/限长类测试可保留 |

---

## 8. 迁移复用 / 替换 / 风险

### 8.1 复用点（与协议无关，低风险）

| 模块 | 复用度 | 依据 |
|---|---|---|
| `state.py` | **原样** | 纯内存快照 + TTL（`state.py:20-60`），无协议依赖；`freshness()` 语义可继续用于"未知/新鲜/过期" |
| `mc_adapter.py` | **原样** | 固定 JSON + 可选 Bearer + 10s 超时 + 不跟随重定向（`mc_adapter.py:22-34`） |
| `models.py`（MC 侧 6 个模型） | **原样** | `models.py:6-45` 与 QQ 协议无关；`CallbackEvent`（`:47-54`）字段名需语义重命名 |
| `service.py` 的 `handle_mc_event` / `_format_mc` | **基本原样** | `service.py:20-45`；只需把"每群广播"的策略替换为"配额感知发送" |
| `service.py` 的命令处理骨架 | **抽层后复用** | `players/status/say/hermes` 分派逻辑（`service.py:69-113`）与协议无关，输入换成规范化结构即可 |
| `hermes.py` | **复用 + 改类型** | 只依赖 OpenAI 兼容 HTTP（`hermes.py:26-95`）；`actor_id:int`（`hermes.py:45`、`ports.py:14-15`）改 `str` |
| `security.py` 的 `bearer_matches` | **部分复用** | `security.py:6-10` 仅适用于 MC webhook / MC callback；**不可**用于 `QQBot <token>`，也不可用于 Ed25519 |
| `personality.py` | 保留占位 | `personality.py:6-12` |
| `Dockerfile` 的非 root / 只读 / cap_drop / no-new-privileges | 复用 | `Dockerfile:12-13`、`docker-compose.yml:10-14` |

### 8.2 替换点（必须新写，不能在原文件加分支）

1. `onebot.py` → 新增官方 `qqbot.py`（Access Token 获取/刷新 + 消息发送 + 网关客户端或回调验签）。
2. `main.py` 的 `/api/v1/onebot/*` 三条路由 → 官方回调（Webhook）或网关客户端（WS），二选一，不要并存双协议解析。
3. `settings.py` 的 OneBot 段与 `int` 归一化；`ports.py` 的出站契约。
4. `scripts/smoke.py` 与 OneBot 专属测试；新增官方协议测试向量（先写向量再接线）。
5. 事件规范化层（新增）：官方 `payload{id,op,s,t,d}` → 内部统一结构。

### 8.3 风险（R 表；★ = 本轮从代码/主机实测新增或强化的证据）

| 编号 | 风险 | 等级 | 依据 |
|---|---|---|---|
| R1 | "MC→QQ 实时全量同步"在官方平台不成立（主动消息配额 + 群/用户可关闭） | 高 | `service.py:33-34`；平台配额见 §10 |
| R2 | 身份模型 `int` → `openid` 字符串，白名单直接失效 | 高 | `settings.py:25,44`；`service.py:53-56,102-103,122-130`；官方 openid 为 Bot 作用域字符串 |
| R3 | 无 `msg_id`/`msg_seq`，无法被动回复 | 高 | `onebot.py:36,128`；全仓库 `msg_id` 命中 0（E7） |
| R4 | WS 方向相反，`OneBotReverseWebSocketHub` 整体作废 | 中高 | `main.py:124`（服务端）vs 官方网关（客户端） |
| R5 | 验签算法与依赖缺失 | 中高 | `main.py:109-117`（HMAC-SHA1）；**实测无 cryptography/pynacl**（E10） |
| R6 | 官方 Webhook 需 HTTPS 且端口限 80/443/8080/8443；现有部署仅 loopback:8000 | 中高 | `docker-compose.yml:9`；本机虽有 Caddy(:8443, E14) 与 frp，但**未为本项目配置站点**，且 frp 公网入口路径未验证 |
| R7 ★ | **MC→QQ 发送结果被丢弃**：MC 侧恒 202，无法感知 QQ 投递失败 | 中（可观测性） | `service.py:31-34` 未使用返回值；`main.py:98` 恒返回 `accepted: true` |
| R8 | 单 bool 表示投递成功，无法区分配额/去重/非群成员/主动权限关闭 | 中 | `onebot.py:41`、`ports.py:6-7` |
| R9 | 内存状态 + 单 worker，重启丢数据；迁移期双栈并存会放大不一致 | 中 | `state.py:9-18`；`docs/DEPLOYMENT.md:58` |
| R10 | 测试基线只覆盖 OneBot，绿灯制造虚假信心 | 中 | E7 关键词全 0；`tests/` 63 处 OneBot 断言 |
| R11 | `hermes` 绕过 `MC_COMMANDS`，且上游具备终端能力 | 中 | `service.py:74-77`（`hermes` 分支在白名单检查之前）；`README.md:154` |
| R12 | `host/port/log_level/llm_*` 是"看起来能配"的死配置，易误导部署 | 低 | §5.3；`grep` 0 引用 |
| R13 | 依赖冗余（`httpx2` 零引用）与 Python 版本三处不一致 | 低 | `pyproject.toml:10,22`；`Dockerfile:1`；E10 |
| R14 | 群消息全量模式（`GROUP_MESSAGE_CREATE`）资质/权限未核实 | 中 | 平台 U1（姊妹文档），本轮无新增证据 |

---

## 9. 与官方 QQ Bot 的接口形状差异（速查，供后续文档引用）

> 来源：`docs/research/QQ_BOT_PLATFORM_FINDINGS.md`（官方页面获取时间 2026-11-10T03:54:36Z）。等级：**O1** = 官方页面直读；**U1** = 官方索引/摘要提及但细节未核实；**U2** = 未确认。

| 维度 | 现状（代码事实） | 官方 Bot v2 | 等级 |
|---|---|---|---|
| 鉴权头 | `Authorization: Bearer <ONEBOT_ACCESS_TOKEN>`（`onebot.py:31-32`、`main.py:126-128`） | `Authorization: QQBot <AccessToken>` | O1 |
| 凭据获取 | 运维手填静态 token | `POST /app/getAppAccessToken`（`appId`+`clientSecret`，≈7200s，过期前 60s 换新） | O1 |
| API 基址 | `${ONEBOT_API_URL}/send_group_msg` | `https://api.bot.qq.com` + `/v2/groups/{group_openid}/messages` | O1 |
| 群标识 | `group_id: int`（`settings.py:25`） | `group_openid: str`（Bot 作用域） | O1 |
| 用户标识 | `user_id: int`（`settings.py:44`） | `author.member_openid: str` | O1 |
| 入站事件 | `post_type/message_type/group_id/message`（`service.py:51-68`） | `payload{id,op,s,t,d}`；`GROUP_AT_MESSAGE_CREATE` | O1 |
| 入站验签 | HMAC-SHA1 `X-Signature: sha1=<40hex>`（`main.py:109-117`） | `X-Signature-Ed25519` + `X-Signature-Timestamp`，签名体 `timestamp+body` | O1 |
| 回调地址验证 | 无 | `op=13` + `plain_token`/`event_ts` → 返回 `signature` | O1 |
| 回调网络 | 任意私网 HTTP（compose 仅 loopback） | HTTPS，端口限 80/443/8080/8443 | O1 |
| WS 方向 | 本服务是**服务端**（`main.py:124`） | 本服务作为**客户端**连 `wss://api.bot.qq.com/websocket`（Identify/Heartbeat/Resume/intents） | O1 |
| 出站语义 | 无 `msg_id`，`{group_id,message,auto_escape}` | 被动回复需 `msg_id`（5 分钟内、每条消息最多 5 次）+ `msg_seq` 去重 | O1 |
| 配额 | 本服务不限制 | Bot 维度 60/30 qpm（认证/未认证）、单关系 20 qpm、每群每天 1000 条、接口 100 QPS | O1 |
| 主动推送许可 | 无 | 群/用户可关闭；群状态含 `allow_proactive_msg` | O1 |
| 群全量消息 | OneBot 协议层可收全量 | `GROUP_MESSAGE_CREATE` 需"接收所有消息"开启；资质/权限细节未核实 | U1 |
| 历史域名 | — | 旧 SDK 常见 `api.sgroup.qq.com`，官方当前页面未出现 | U2 |

> 说明：本轮**没有**新增官方平台调研，上表仅做"代码现状 ↔ 官方形状"的对照索引；任何 U1/U2 项不得在后续文档中升级为确定事实。

---

## 10. 待确认项（交给 Planner / 后续 Worker）

1. **MC 侧事件源选型**：目标实例在 MCSManager 下（`/opt/mcsmanager` root-only），到底用 `QueQiao` 类 mod、MCSManager 插件、还是日志 sidecar 提供 `chat/join/leave/death/players/status`？这决定"MC→bridge"这一段能否成立，也决定要不要安装 mod（当前实例是 Forge 1.20.1-47.4.16，另一个是 `server.jar`，两者版本不同，需分别确认）。需要 sudo 或面板账号才能查实例目录与 mod 列表。
2. **官方接入方式二选一**：Webhook（需公网 HTTPS + 80/443/8080/8443）还是 Gateway WS（需外出连接 + intents + resume）。本机有 Caddy（`/etc/caddy/Caddyfile`，已有 3 个 `:8443` 站点）与 SakuraFrp 容器，但**未验证**是否能把某个域名/端口稳定映射给官方回调——这属部署决策，本轮未评估。
3. **产品语义**：官方平台下"MC 聊天全量同步进群"是否保留？若不保留，降级为"@bot 查询 + 聚合通知"后，`mc_notifications` 四开关的语义需要重新定义（聚合窗口、去抖、每日上限）。
4. **openid 白名单的持久化**：`group_openid`/`member_openid` 在 AppID 作用域内是否稳定、是否随机器人重建而变化——直接影响 `settings` 里写死白名单是否可行。平台文档未给明确结论（U1）。
5. **迁移期并存策略**：`ALLOWED_GROUP_IDS`（int）与 openid 白名单若同时存在，必须防止"旧 ID 仍然生效"绕过新策略；建议单链路切换，不做双协议同进程解析。
6. **`hermes` 通道是否随迁移保留**：目前它绕过 `MC_COMMANDS`、上游具备终端能力（R11）。若保留，管理员白名单要改成 `member_openid`，并需要任务级策略/审计——属产品与安全决策。
7. **Docker 路径由谁验证**：本轮因权限无法构建/启动容器（E16）。需要一个能交互 sudo 的会话，或把 `czauw` 加入 docker 组（属系统变更，需用户授权）。
8. **是否存在并行盘点冲突**：`docs/research/` 下已有 3 份相关文档（`QQ_BOT_CURRENT_STATE.md` 03:58Z、`QQ_BOT_PLATFORM_FINDINGS.md` 04:01Z、`official-qq-platform-and-architecture.md` 04:03Z）与本文件（04:04Z）。内容有重叠但层次不同，建议 Planner 指定唯一"现状基线"文件作为后续 Worker 的引用源，避免分叉。

---

## 11. 本轮对仓库的影响

- 新增：`docs/research/official-qq-current-state.md`（本文件）。除此之外**零改动**：未修改 `src/`、`tests/`、`scripts/`、`README.md`、其他 `docs/`、`.env.example`、部署工件；未创建 `.env`；未 commit / reset / clean。
- 临时探针脚本只写在会话 scratch 目录（`~/.hermes/profiles/worker/cache/scratch/mcping*.py`、`mcdump.py`），**不在仓库内**。

---

## 12. 附录：证据与复现

### 12.1 关键命令与输出（原始摘录）

```text
$ cd /home/czauw/minecraft-qq-bridge
$ .venv/bin/python -V
Python 3.14.9

$ .venv/bin/python -m pytest -q -W error
.....................................................                    [100%]
53 passed in 1.19s

$ .venv/bin/python -m pytest --collect-only -q | tail -2
53 tests collected in 0.13s

$ .venv/bin/python scripts/smoke.py
INFO:     Uvicorn running on http://127.0.0.1:50631 (Press CTRL+C to quit)
SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only
EXIT=0

$ ls -la .env
ls: cannot access '.env': No such file or directory

$ git log --oneline -5
fatal: 您的当前分支 'master' 尚无任何提交

$ grep -rniI "group_openid\|member_openid\|msg_id\|msg_seq\|intents\|QQBot\|Ed25519" src tests scripts --include=*.py | wc -l
0

$ ss -ltn | grep -E ':(8000|3000|25565|25567|25580|8642|9119|23333|8443)\b'
LISTEN 0 4096   127.0.0.1:23333  0.0.0.0:*
LISTEN 0 4096   127.0.0.1:8443   0.0.0.0:*
LISTEN 0 4096     0.0.0.0:25567  0.0.0.0:*
LISTEN 0 4096     0.0.0.0:25565  0.0.0.0:*
LISTEN 0 2048     0.0.0.0:9119   0.0.0.0:*
LISTEN 0 4096        [::]:25567     [::]:*
LISTEN 0 4096        [::]:25565     [::]:*
# 注意：8000 / 3000 / 25580 / 8642 均未出现 —— 桥接器、OneBot、MC callback 消费者、Hermes API 都不在运行

$ curl -s --max-time 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8642/v1/models
000        # 连接失败，无监听

$ curl -s --max-time 4 http://127.0.0.1:23333/api/overview
{"status":403,"data":"权限不足","time":1794283288537}

$ ps -o user,pid,etime,args -p 1012999,1063703
root 1012999 1-01:02:33 java -Xms2G -Xmx6G -jar server.jar nogui
root 1063703   15:50:53 java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.16/unix_args.txt nogui

$ docker ps
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
$ sudo -n true
sudo: interactive authentication is required
```

### 12.2 代码索引（引用锚点）

- 装配：`main.py:32-39`；传输开关 `main.py:36`。
- 入站 MC：`main.py:80-98`；鉴权 `main.py:68-70`；体积上限 `main.py:60-66`。
- 入站 OneBot HTTP：`main.py:100-122`（验签 `109-117`）；反向 WS：`main.py:124-165`。
- 出站 OneBot：`onebot.py:27-48`（HTTP）、`51-136`（WS hub）。
- 编排：`service.py:20-45`（MC→QQ）、`47-113`（QQ→MC）、`122-130`（`_int`）。
- 状态：`state.py:20-60`。模型：`models.py:6-45`、`47-54`。端口契约：`ports.py:6-17`。
- 配置：`settings.py:10-11`（env_file）、`21-54`（字段）、`56-73`（归一化）。
- Hermes：`hermes.py:45-84`、`86-95`；MC callback：`mc_adapter.py:22-34`。
- 声明与边界：`README.md:3,111-120,122-131,141-156`；`docs/DEPLOYMENT.md:5,24-36,58`；`docs/THREAT_MODEL.md:13-21,23-31`；`docs/VERIFICATION.md:5-12,17-20,24`。

### 12.3 临时探针（scratch，不在仓库）

```python
# ~/.hermes/profiles/worker/cache/scratch/mcping2.py  — MC status ping（多协议号探测）
# ~/.hermes/profiles/worker/cache/scratch/mcdump.py    — 原始包 dump，用于确认 25565 返回的是 disconnect 包
# 入站语义探针（内联执行）：
from fastapi.testclient import TestClient
from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings
s = Settings(_env_file=None, onebot_webhook_token="probe-token",
             allowed_group_ids=[123456], onebot_enabled=False)
with TestClient(create_app(s)) as c:
    print(c.post("/api/v1/onebot/events", json=dict(
        post_type="message", message_type="group", group_id=123456,
        user_id=42, message="/mc players"),
        headers={"Authorization": "Bearer probe-token"}).json())
    # -> {'accepted': True, 'delivered': False}
```

---

**交接说明**：本文件只描述"现在是什么、哪些经过实测、哪些没有"，不含实施方案。后续 `docs/QQ_BOT_*` 类实现文档应基于 §3（能力）、§7（耦合点）、§8（复用/替换/风险）撰写，并与 `docs/research/QQ_BOT_PLATFORM_FINDINGS.md` 对齐；本文标 U1/U2 的平台项不得升级为确定事实。
