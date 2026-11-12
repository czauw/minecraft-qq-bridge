# 排行榜需求文档（死亡榜 / 在线时长榜）

> 状态：**需求已定，未实现**。本文只定义行为与口径，实现细节由后续任务卡决定。
> 决策日期 2026-11-12（第二版：命令只匹配服务器、无参输出全量、保留结构化接口供后续图片渲染）。

## 1. 目标

在现有桥接器里增加两个群内可查的排行榜：

| 榜 | 指标 | 数据来源 |
|---|---|---|
| 死亡榜 | 累计死亡次数（`minecraft:custom.minecraft:deaths`） | 服务器 `world/stats/<uuid>.json` |
| 在线时长榜 | 累计游戏时长（`minecraft:custom.minecraft:play_time`，tick → 秒） | 同上 + 当前在线会话实时加时 |

**粒度是（服务器 × 玩家）二元组**：同一玩家在不同服务器是不同条目，绝不合并成一个人；同一服务器内按玩家名聚合（见 §4）。

**默认输出是「全量」**：不带参数时，输出所有已配置且启用的服务器上的**全部玩家**，不是只给 Top N。

## 2. 非目标（本期明确不做）

- 不统计、不显示**死因**（`stats` 只有次数，死因需要事件流且无法回填历史）
- **不支持按玩家名查询**（参数只匹配服务器名；玩家名当参数一律视为「未找到服务器」）
- 不做跨服总榜（同一玩家在不同服务器分开列）
- 不做游戏内展示（不进 MC 记分板 / 不进 tab 列表）
- 不做定时推送 / 周榜月榜 / 成就榜
- 不输出图片（第一版只出文本；图片属后续版本，见 §8 / §14）
- 不写任何 MC 侧数据（只读 `stats` 与 `usercache.json`）

## 3. 已确认的决策

| # | 决策 | 说明 |
|---|---|---|
| 1 | **按玩家名合并** | 同一 `(服务器, 名字)` 下多个 UUID 的数值求和，榜上只出现一行 |
| 2 | **不写死因** | 只显示次数 |
| 3 | **时长精确到秒** | 显示格式 `20h35m07s`，由 tick ÷ 20 得出（1 tick = 0.05 s） |
| 4 | **参数只匹配服务器** | 不做玩家名查询，避免与服务器名歧义 |
| 5 | **无参 = 全量** | 输出所有启用服务器的全部玩家，不设 Top N、不默认截断 |
| 6 | **接口与数据要保留** | 命令名、表结构、结构化接口（§8）稳定；后续加图片渲染只换渲染层 |

## 4. 身份与聚合规则

真实数据里已经出现同一名字对应多个 UUID 的情况（离线模式生成的 v3 UUID 与正版 v4 UUID 并存），因此：

- **存储**按 `(server, uuid)`：保留可追溯性，改名时不丢历史
- **展示**按 `(server, name)`：`SUM()` 求和，榜上一人一行
- `name` 取该 uuid 在 `usercache.json` 里最新的名字；找不到映射时用 uuid 前 8 位并标记 `?`
- 跨服不合并：`MUEZORE` 在「齿轮盛宴」和「亡者世界」是两条独立数据

已知代价（接受）：离线模式下同名玩家会被当成同一人合并统计。

## 5. 数据口径

### 5.1 权威值：`world/stats/<uuid>.json`

```json
{"stats": {"minecraft:custom": {"minecraft:play_time": 1220773, "minecraft:deaths": 49}}}
```

- `play_time` 单位 **tick**（20 tick/秒），换算：`秒 = ticks // 20`
- 该文件在玩家退出、世界保存时落盘，因此**离线玩家有准确累计值**
- `play_time` 含单人存档以来的全部时长；**世界被删除/替换会归零**，属于可接受的语义（需在 README 说明）

### 5.2 在线玩家的当前会话

`stats` 不实时更新，所以在线玩家 = `stats` 累计值 + 本次会话已过时间：

```
显示秒数 = play_time // 20 + (now - session_started_at)
```

会话来源：QueQiao 的 `player_join` / `player_quit` 事件；桥接器重启后从 RCON `list` 对账补开。

### 5.3 崩溃与断线

- 服务器崩溃 / 强杀 → 收不到 `player_quit` → 会话残留。处理：每 **60 秒**用 RCON `list` 对账（在线但无会话 → 补开；有会话但不在线 → 以对账时刻关闭）
- 单服连接断开 → 该服所有会话在对账时刻关闭，时长不继续累加
- 因此时长误差上限 ≈ 对账间隔（60 秒）+ 最后一次 stats 落盘前的增量

## 6. 命令接口

| 输入 | 行为 |
|---|---|
| `/death` | 所有已配置且启用的服务器，每服输出**该服全部玩家**的死亡次数 |
| `/death <服务器名>` | 只输出该服的全部玩家 |
| `/playtime` | 所有已配置且启用的服务器，每服输出**该服全部玩家**的在线时长 |
| `/playtime <服务器名>` | 只输出该服的全部玩家 |

解析规则：

1. 命令本身大小写不敏感（`/Death` 也算）；参数不做模糊匹配
2. 参数按**服务器名精确匹配**（区分大小写，与 `/list` 一致）
3. 参数不是任何已配置服务器的名字 → `未找到服务器：<参数>`（**不会**再当成玩家名去找）
4. 参数命中 `enabled: false` 的服务器 → 同样回 `未找到服务器：<参数>`（禁用项对外不可见，与 `/list` 一致）
5. 多余参数（如 `/death 齿轮盛宴 5`）→ 忽略多余部分，仍按该服输出全量，不报错

其它约束沿用现有行为：

- **以 `/` 开头的消息一律不转发 MC**
- 全量模式下 `<@member_openid>` 前缀先剥离再匹配
- 命令来自群内任意成员即可，不做权限分级

## 7. 输出格式（严格）

时长格式化规则：

| 区间 | 格式 | 例 |
|---|---|---|
| ≥ 1h | `XhYYmZZs` | `20h35m07s` |
| < 1h | `YYmZZs` | `07m12s` |
| < 1m | `ZZs` | `42s` |
| 0 | `0s` | `0s` |

在线玩家追加标记：`20h35m07s（在线中）`。

排序：数值降序；数值相同按玩家名 Unicode 升序（保证输出确定）。**包含 0 值玩家**（满足「全部玩家」的要求）。

```
/death
齿轮盛宴 死亡榜：
1. MUEZORE  82 次
2. yui_Thefish  50 次
3. Longsz  19 次
4. RoyalSSS  3 次

亡者世界 死亡榜：
1. MUEZORE  115 次
2. Saltedfish_SHELL  7 次
3. lazyhape  5 次
```

```
/playtime
齿轮盛宴 在线时长榜：
1. Longsz  20h35m07s
2. MUEZORE  19h09m12s（在线中）
3. yui_Thefish  16h59m40s
4. RoyalSSS  04m03s

亡者世界 在线时长榜：
1. MUEZORE  01h27m11s
2. Saltedfish_SHELL  57m30s
3. lazyhape  33m05s
```

- 每个服务器一段，段首 `<服务器名> <榜名>：`，段间空行分隔，顺序与 `config.yaml` 的 `servers` 一致
- 某服无任何玩家数据：`<服务器名> 暂无数据`
- 某服禁用（`enabled: false`）：**整段不出现**
- 全部服都无数据：`排行榜暂无数据`
- 长度控制：单条消息受 `max_message_chars`（默认 1500）限制，超出按**服务器边界**切分为多条，最多 `max_reply_parts`（5 片）；切分不得切断单个玩家行；仍放不下时最后一片末尾加 `（列表过长，已截断）`

> 示例中的玩家名与次数取自 2026-11-12 的真实 `stats` 快照（当时配置里启用的是「齿轮盛宴」「亡者世界」两台）；**秒数是格式演示**，不是真实值。

## 8. 结构化接口与数据（为后续图片渲染保留）

第一版只出文本，但**数据与接口必须与渲染方式解耦**：后续加图片时只新增渲染器，不改命令、不改表、不改取数逻辑。

### 8.1 内部数据结构

两个榜共用同一结构（渲染层只需这一个输入）：

```python
@dataclass(frozen=True)
class RankRow:
    rank: int
    player: str            # 展示名（已按 §4 合并）
    value: int             # deaths: 次数；playtime: 秒
    display: str           # 已格式化，如 "82 次" / "20h35m07s"
    online: bool = False   # 仅 playtime 有意义

@dataclass(frozen=True)
class ServerBoard:
    server: str
    rows: list[RankRow]    # 全量，不截断
    total_players: int

@dataclass(frozen=True)
class Leaderboard:
    kind: Literal["deaths", "playtime"]
    schema_version: int    # 从 1 开始，字段只增不改
    generated_at: str      # RFC3339 UTC
    servers: list[ServerBoard]   # 顺序 = config.yaml 顺序
```

- 文本渲染器与（将来的）图片渲染器都只消费 `Leaderboard`
- 文本渲染器负责分片/截断；图片渲染器负责画图，不参与取数与格式化决策

### 8.2 只读 JSON 接口（复用现有 loopback 状态端口）

复用 `127.0.0.1:18080`，不新增端口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/leaderboards/deaths` | 全部启用服务器 + 全部玩家 |
| GET | `/api/v1/leaderboards/playtime` | 同上，时长秒级 |
| GET | `/api/v1/leaderboards/{kind}?server=<名字>` | 单服 |

- 返回 §8.1 结构的 JSON，带 `schema_version: 1`
- 与 `/api/v1/status` 一样不返回 openid、不返回密钥
- 这是图片渲染器的取数入口：后续 `/death 图` 或定时出图都直接读它，不重复查库

### 8.3 持久化（三张表固定）

```sql
CREATE TABLE players (
  server TEXT, uuid TEXT, name TEXT,
  first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (server, uuid)
);
CREATE TABLE player_stats (
  server TEXT, uuid TEXT,
  ticks INTEGER NOT NULL DEFAULT 0,
  deaths INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (server, uuid)
);
CREATE TABLE sessions (
  server TEXT, uuid TEXT, name TEXT,
  started_at TEXT NOT NULL,
  PRIMARY KEY (server, uuid)
);
```

## 9. 数据刷新与存储

- 存储：SQLite 单文件 `/home/czauw/.local/share/mcqq-bridge/stats.db`
- 扫描：启动时全量导入一次；之后每 **60 秒**扫 `world/stats/*.json`，仅当 mtime 变化才解析（避免反复读几千个文件）
- 写入：单连接 + 串行队列（`asyncio.to_thread` 包装）；写失败只记日志，不影响转发链路
- 保留：只保留累计值，不存逐次明细（不写死因，无明细需求）

## 10. 失败与降级（不得伪造数据）

| 情况 | 行为 |
|---|---|
| stats 文件解析失败（半写） | 跳过该文件，保留上一次的值；日志 WARNING 一次（含服务器名，不含玩家数据） |
| 某服 `world/stats` 目录不存在 | 该服显示 `<服务器名> 暂无数据` |
| 整个数据源不可用 | 榜单显示 `排行榜数据暂不可用`，不显示 0 |
| 单台 RCON 失败 | 只影响该服的对账，不影响其它服榜单 |
| 数据库损坏 | 启动时重建并重新全量导入（stats 是权威源，可重算） |

## 11. 权限与运行环境

- 服务以 `root` 运行（`CapabilityBoundingSet=CAP_DAC_READ_SEARCH`），因为 `/opt/mcsmanager` 是 `750 root:root`
- 实例数据只读挂载：`ReadOnlyPaths=/opt/mcsmanager/daemon/data/InstanceData`
- 唯一可写路径：`ReadWritePaths=/home/czauw/.local/share/mcqq-bridge`（SQLite 落点）
- 除 `world/stats/*.json` 与 `usercache.json` 外不读任何实例文件；不向实例目录写入

## 12. 验收标准

1. `/death` 无参数时输出**所有启用服务器 × 每服全部玩家**（含 0 值），不是 Top N
2. `/playtime` 无参数时同上
3. 参数只匹配服务器：`/death MUEZORE`（玩家名）必须回 `未找到服务器：MUEZORE`，不得当成玩家查询
4. 禁用服务器整段不出现；`/death <禁用服名>` 回 `未找到服务器：<名字>`
5. `/playtime <服>` 的秒数能被 `world/stats` 独立复算（离线完全一致；在线差值 ≤ 对账间隔）
6. 秒级精度：`ticks // 20`，不允许分钟粒度
7. 同服同名双 UUID 只出现一行且数值求和（用「齿轮盛宴 / MUEZORE」验证：82 次 / 19h09m 量级）
8. 跨服同名不合并：`MUEZORE` 在两服各一行
9. 重启桥接器后数字不丢、不清零
10. 崩溃场景：会话残留由 60 秒对账收敛，时长误差 ≤ 60 秒
11. 长名单正确切分：不切断玩家行；超 5 片时末尾标注截断
12. `GET /api/v1/leaderboards/deaths` 与文本结果一致，含 `schema_version`
13. `/list`、消息转发、`/` 前缀抑制、`<@...>` 剥离等既有行为回归全绿
14. 榜单查询走被动回复（带 `msg_id`），不消耗主动消息 1000 条/天配额

## 13. 风险与未验证

- **26.2（原版生电）未验证**：该实例目前没有 `world/stats` 目录，`play_time`/`deaths` 键名与 1.20.1 是否一致需实测
- **世界重置归零**：换世界/删档会让累计时长跳变，属预期
- **同名合并的副作用**：离线模式下任何人可用任何名字，统计边界以此为准
- **stats 半写**：MC 整文件覆盖写入，读的瞬间可能拿到截断 JSON，已定义跳过策略
- **多世界/多维度**：本期只统计 `world/stats`（默认维度世界），`world_nether` 等独立世界不计入
- **全量输出的长度**：玩家很多的服（>100 人）会撞 5 片上限，本期接受截断

## 14. 后续版本（不在本期）

- **图片渲染**：消费 §8.1 的 `Leaderboard` / §8.2 的 JSON 出图发群（需要富媒体上传）
- 死因榜 / 最近死亡（需要事件流，从部署起累计）
- 周榜 / 月榜（需要按天快照）
- 成就榜、在线峰值