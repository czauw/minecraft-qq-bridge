# MCSManager 四实例 QueQiao 接入记录

> 生成时间：2026-11-12。所有实例均为 MCSManager `processType: docker`（eclipse-temurin 容器，实例目录挂到 `/data`）。

## 端口与命名

| 实例名 | 实例 UUID（前 8 位） | MC 版本 / loader | MC 端口 | QueQiao WS（宿主） | RCON（容器内） |
|---|---|---|---|---|---|
| Cursed-Walking | `6224e6c9` | 1.20.1 / Forge 47.4.0 | 25566 | 8091 | 25581 |
| 齿轮盛宴 | `6973f04f` | 1.20.1 / Forge 47.4.16 | 25565 | 8092 | 25582 |
| 原版生电 | `89cba5e2` | 26.2 / Fabric 0.20.1 | 25567 | 8093 | 25583 |
| 亡者世界 | `a01b36be` | 1.20.1 / Forge 47.4.10 | 25566 | 8094 | 25584 |

- QueQiao 版本：Forge 服 `0.5.0+forge-1.20.1`，Fabric 服 `0.5.2+fabric-26.1`（该构建声明支持 26.2；`fabric.mod.json` 要求 `minecraft >=26.1 <26.3`、`fabricloader >=0.19.2`、`java >=25`、`fabric-api`）。
- 桥接器 `config.yaml` 中 `servers[].name` 与 QueQiao 的 `server_name` 一一对应（中文名由桥接器 URL 编码 `x-self-name`）。
- 每台独立 `access_token`（128 位）与独立 RCON 口令；密码只存在于 `server.properties` 与 `config/QueQiao/config.yml`，不写入本文件。

## 改了哪些文件（每台实例目录下）

| 文件 | 变更 |
|---|---|
| `mods/QueQiao-*.jar` | 新增对应 loader/版本 jar |
| `config/QueQiao/config.yml` | 新增（QueQiao v0.5.x 的真实读取路径是 `config/QueQiao/config.yml`，大小写敏感） |
| `server.properties` | `enable-rcon=true`、`rcon.port`、`rcon.password`、`broadcast-rcon-to-ops=false`（备份为 `server.properties.bak-mcqq-*`） |
| `/opt/mcsmanager/daemon/data/InstanceConfig/<uuid>.json` | `docker.ports` 追加 `WS:WS/tcp`（备份为 `*.json.bak-mcqq-*`），使宿主可访问容器内的 QueQiao WS |

MCSManager daemon 与 web 已重启以加载新端口映射（启动日志：`所有应用实例已加载，总计 4 个`）。

## 实测证据（逐台起服后抓取，验完即停）

| 实例 | QueQiao 启动 | RCON | `send_rcon_command list` | `broadcast` |
|---|---|---|---|---|
| Cursed-Walking | `WebSocket Server 在 0.0.0.0:8091 启动` | `Rcon 连接成功 [25581]` | `There are 0 of a max of 20 players online:` | SUCCESS |
| 齿轮盛宴 | `:8092` | `[25582]` | `... max of 20 ...` | SUCCESS |
| 原版生电 | `:8093` | `[25583]` | `... max of 64 ...` | SUCCESS |
| 亡者世界 | `:8094` | `[25584]` | `... max of 20 ...` | SUCCESS |

鉴权行为也已实测：错误/缺失 `Authorization` → 关闭码 `1008 Authorization Header is wrong`；正确 token → `code 200 SUCCESS`。

## 仍然缺的（只能人工完成）

1. QQ 开放平台：`bot.app_id`、`bot.client_secret`（写进 `.env` 的 `QQ_BOT_CLIENT_SECRET`）、`bot.group_openid`。
2. 取 `group_openid`：`.venv/bin/python scripts/discover_group_openid.py --app-id <AppID> --env-file .env`，然后在群里 @机器人。
3. 端口对外暴露说明：四个 WS 端口目前由 Docker 发布在 `0.0.0.0`（UFW 未启用、无防火墙），依赖 `access_token` 鉴权。若要求只允许本机访问，需在 Docker 端口绑定上加 `127.0.0.1:` 前缀（MCSManager 面板是否支持取决于其版本，未验证）。