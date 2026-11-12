# MCSManager 四实例接入记录与本机运行手册

> 生成时间：2026-11-12。四台实例均为 MCSManager `processType: docker`（eclipse-temurin 容器，
> 实例目录挂到 `/data`）。本文件记录已实施的事实，不是通用教程。

## 1. 端口与命名

| 实例名 | 实例 UUID（前 8 位） | MC 版本 / loader | MC 端口 | QueQiao WS（宿主） | RCON（容器内） |
|---|---|---|---|---|---|
| Cursed-Walking | `6224e6c9` | 1.20.1 / Forge 47.4.0 | 25566 | 8091 | 25581 |
| 齿轮盛宴 | `6973f04f` | 1.20.1 / Forge 47.4.16 | 25565 | 8092 | 25582 |
| 原版生电 | `89cba5e2` | 26.2 / Fabric 0.20.1 | 25567 | 8093 | 25583 |
| 亡者世界 | `a01b36be` | 1.20.1 / Forge 47.4.10 | 25566 | 8094 | 25584 |

* QueQiao 版本：Forge 服 `0.5.0+forge-1.20.1`；Fabric 服 `0.5.2+fabric-26.1`（该构建声明支持 26.2；
  `fabric.mod.json` 要求 `minecraft >=26.1 <26.3`、`fabricloader >=0.19.2`、`java >=25`、`fabric-api`）。
* MC 端口有两台同为 25566（Cursed-Walking 与亡者世界），是真实配置，不是笔误；它们的宿主 WS 端口不同，
  桥接器按 `host:port` 区分的是 WS 端口，不受此影响。
* 桥接器 `config.yaml` 的 `servers[].name` 与 QueQiao 的 `server_name` 一一对应（中文名由桥接器 URL 编码
  `x-self-name`）。
* 每台独立 `access_token`（128 位）与独立 RCON 口令；口令只存在于各实例的 `server.properties` 与
  `config/QueQiao/config.yml`，不写入本文件、不写入桥接器配置。

## 2. 每台实例改了哪些文件

| 文件 | 变更 |
|---|---|
| `mods/QueQiao-*.jar` | 新增对应 loader/版本的 jar |
| `config/QueQiao/config.yml` | 新增（QueQiao v0.5.x 的真实读取路径是 `config/QueQiao/config.yml`，**大小写敏感**） |
| `server.properties` | `enable-rcon=true`、`rcon.port`、`rcon.password`、`broadcast-rcon-to-ops=false`（备份 `server.properties.bak-mcqq-*`） |
| `/opt/mcsmanager/daemon/data/InstanceConfig/<uuid>.json` | `docker.ports` 追加 `WS:WS/tcp`（备份 `*.json.bak-mcqq-*`），让宿主能访问容器内 QueQiao WS |

MCSManager daemon 与 web 已重启以加载新端口映射（启动日志：`所有应用实例已加载，总计 4 个`）。

## 3. 实测证据

逐台起服后抓取，验完即停：

| 实例 | QueQiao 启动 | RCON | `send_rcon_command list` | `broadcast` |
|---|---|---|---|---|
| Cursed-Walking | `WebSocket Server 在 0.0.0.0:8091 启动` | `Rcon 连接成功 [25581]` | `There are 0 of a max of 20 players online:` | SUCCESS |
| 齿轮盛宴 | `:8092` | `[25582]` | `... max of 20 ...` | SUCCESS |
| 原版生电 | `:8093` | `[25583]` | `... max of 64 ...` | SUCCESS |
| 亡者世界 | `:8094` | `[25584]` | `... max of 20 ...` | SUCCESS |

鉴权行为也已实测：错误/缺失 `Authorization` → 关闭码 `1008 Authorization Header is wrong`；
正确 token → `code 200 SUCCESS`。

这些证据只覆盖：插件启动、RCON 连接、`list` 返回零人在线、`broadcast` 返回 ACK、token 鉴权。
**没有证明**：真实玩家在游戏内看到广播、非零在线名单、join/leave/death 事件、QQ 群双向收发。

## 4. 桥接器在本机的运行方式

systemd 单元 `/etc/systemd/system/mcqq-bridge.service`：

* `User=czauw`、`Group=czauw`，`WorkingDirectory=/home/czauw/minecraft-qq-bridge`
* `ExecStart=.../.venv/bin/python -m mcqq_bridge --config .../config.yaml --env-file .../.env`（全绝对路径）
* `Restart=on-failure`、`RestartSec=10`、`StartLimitBurst=3 / 120s`（密钥缺失时退出码 2，限次避免刷日志）；
  加固项见单元文件；`After=... docker.service` 因为四台 QueQiao 在容器里
* 通用 systemd 说明与新建单元的模板见 `README.md` §5.1

```bash
sudo systemctl status mcqq-bridge.service
sudo systemctl restart mcqq-bridge.service
journalctl -u mcqq-bridge.service -f
```

状态接口（只读、默认 `127.0.0.1:18080`）：

| 端点 | 判据 |
|---|---|
| `/healthz` | 进程活着即 `{"status":"ok"}` |
| `/readyz` | `ready` 需 QQ 已连 + ≥1 台启用服已连 + 无 `qq_degraded`；否则 `degraded` |
| `/api/v1/status` | 每台连接状态、人数、`reconnects`、受控错误；**含禁用项** |

`servers_connected` 少于 4 是正常现象：只要求至少一台，具体哪台看 `servers[].connected`。
2026-11-12T06:29Z 的一次只读探测：`ready`、QQ 已连、4 台启用中 2 台已连（8092 齿轮盛宴、8094 亡者世界，
`player_source: rcon`），8091/8093 为 `connect_failed:ConnectionRefusedError`（对应实例当时未运行）。
这类数值随实例起停变化，不能当固定状态或 SLA 依据。

## 5. 凭据位置与 `group_openid` 采集

1. `bot.app_id`、`bot.group_openid` 写在 `/home/czauw/minecraft-qq-bridge/config.yaml`；
   `bot.client_secret` 用 `${QQ_BOT_CLIENT_SECRET}` 引用，真实值只在 `.env`（权限 600）里。
   三者都不写入本文件、不进群、不进仓库。
2. `group_openid` 只能从群事件里取（官方没有列群接口）。取之前先停桥接器，避免两处抢占网关会话：

   ```bash
   sudo systemctl stop mcqq-bridge.service
   .venv/bin/python scripts/discover_group_openid.py --app-id <AppID> --env-file .env
   # 在目标群 @机器人 发一句话，脚本打印 group_openid 后退出
   # 填进 config.yaml 的 bot.group_openid，再启动
   sudo systemctl start mcqq-bridge.service
   ```

   脚本还会打印群名、`member_openid` 与消息前 40 字，不要整段粘到工单或聊天里。
3. 换机器人或换群后 openid 会失效，需要重新采集。

## 6. 网络暴露与待办

四个 QueQiao WS 端口目前由 Docker 发布在 `0.0.0.0`，UFW 未启用、没有其它防火墙，
安全性完全依赖 `access_token` 鉴权。要只允许本机访问，需在 Docker 端口绑定上加 `127.0.0.1:` 前缀，
但 MCSManager 面板是否支持该写法取决于版本，**未验证**；这是待实施项，不是现状。

RCON 只在容器内被 QueQiao 连接（25581–25584），不要为调试映射到公网。
状态端口与 `.env` 同样只留在本机。