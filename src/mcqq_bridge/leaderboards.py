"""排行榜服务：生命周期、增量扫描、在线会话、RCON 对账与 DTO 生成。

数据口径严格按 ``docs/LEADERBOARD_REQUIREMENTS.md``：

* **粒子度是（服务器 × 玩家）**：跨服不合并；同服同名多 UUID 求和成一行。
* **权威值来自 ``world/stats/<uuid>.json``**（``play_time`` 是 tick，20 tick/秒）。
* **在线加时** = ``ticks // 20 + floor(now - 会话 anchor)``；扫描到该 UUID 的
  ticks 变化时把 anchor 推进到该文件的 mtime，避免「文件已含本次会话」再被
  加一次（防双计）；ticks 下降（换世界）按新值重置基线。
* **60 秒 RCON ``list`` 对账**收敛崩溃/断线残留会话；对账失败绝不批量清空在线记录，
  而是以「该服最后一次成功 ``list`` 的时刻 + 一个对账周期」封顶（成功 ``list`` 之后才 join
  的会话也不延长该封顶）**把会话关掉并把封顶内的增量折进
  ``player_stats.ticks``**——展示与落盘同一口径，RCON 恢复后不会再补加这段时间。
  对账误差上限即一个对账周期（60 秒），另加最后一次 ``stats`` 落盘前的增量（不可控）。
* 统计层失败只降级自己：不使桥接器退出、不影响 /list 与消息转发。

本模块不做文本渲染（见 :mod:`leaderboard_text`），也不做 HTTP/QQ 收发。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable

from .config import AppConfig
from .events import MCEvent
from .leaderboard_models import (
    Leaderboard,
    LeaderboardKind,
    LeaderboardUnavailable,
    RankRow,
    ServerBoard,
    UnknownServerError,
    utc_now_text,
)
from .leaderboard_text import format_deaths, format_duration
from .redact import error_kind
from .stats_scanner import StatsScanner, UserCache, merge_usercache_names, normalize_uuid
from .stats_store import PlayerRow, SessionRow, StatsPatch, StatsRow, StatsStore, StoreUnavailable

__all__ = [
    "LeaderboardService",
    "RECONCILE_INTERVAL_SECONDS",
    "SCAN_INTERVAL_SECONDS",
    "MAX_ONLINE_EXTRAPOLATION_SECONDS",
]

SCAN_INTERVAL_SECONDS = 60
RECONCILE_INTERVAL_SECONDS = 60
# 对账持续失败时，在线加时最多外推到「最后一次确证在线 + 一个对账周期」
MAX_ONLINE_EXTRAPOLATION_SECONDS = RECONCILE_INTERVAL_SECONDS
MAX_QUEUED_EVENTS = 512


@dataclass
class _PlayerState:
    name: str
    first_seen: str
    last_seen: str


@dataclass
class _StatState:
    ticks: int
    deaths: int
    updated_at: str


@dataclass
class _SessionState:
    name: str
    started_at: float  # epoch 秒：尚未计入 player_stats.ticks 的在线时间起点
    evidence_at: float  # epoch 秒：最后一次「确证在线」的观测时刻


@dataclass
class _ServerState:
    status: str = "empty"  # ok | empty | missing | unavailable
    has_history: bool = False
    # 最近一次成功拿到 RCON list 的时刻（epoch 秒）；对账失败时用它算封顶
    last_verified_at: float | None = None
    # 最近一轮无法确证在线名单（RCON 失败 / 桥接断开）：展示与落盘一起改用严格封顶
    degraded: bool = False


class LeaderboardService:
    def __init__(
        self,
        config: AppConfig,
        *,
        manager: Any = None,
        clock: Any = None,
        logger: logging.Logger | None = None,
        loops: bool = True,
        store: StatsStore | None = None,
    ) -> None:
        self.config = config
        self.manager = manager  # 由 runtime 在 ServerManager 建好后注入
        self._clock = clock or time.time
        self._log = logger or logging.getLogger("mcqq_bridge.leaderboards")
        self._loops = loops
        self._store: StatsStore | None = store
        self._store_ready = False
        self._started = False
        self._stopping = False
        self._scanners: dict[str, StatsScanner] = {
            server.name: StatsScanner(server.name, server.stats_dir, logger=self._log)
            for server in config.enabled_servers()
            if server.stats_dir
        }
        # 没配置任何 stats_dir 时整个统计层不启动：不创建默认数据库、不开周期任务
        self._active = bool(self._scanners)
        self._players: dict[tuple[str, str], _PlayerState] = {}
        self._stats: dict[tuple[str, str], _StatState] = {}
        self._sessions: dict[tuple[str, str], _SessionState] = {}
        self._server_state: dict[str, _ServerState] = {
            server.name: _ServerState() for server in config.servers
        }
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._aux_tasks: set[asyncio.Task[Any]] = set()
        self._wake = asyncio.Event()
        self._events: deque[MCEvent] = deque(maxlen=MAX_QUEUED_EVENTS)
        self._event_task: asyncio.Task[None] | None = None

    # -- 生命周期 ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """是否配置了至少一个数据源（没配时榜单恒为「暂无数据」）。"""
        return self._active

    async def start(self) -> None:
        if self._started:
            return
        self._stopping = False
        if not self._active:
            # 没有任何 stats_dir：不建库、不扫描、不建周期任务（保持旧行为）
            self._started = True
            return
        if self._store is None:
            self._store = StatsStore(
                self.config.stats.db_path,
                root=self.config.stats.root,
                logger=self._log,
            )
        try:
            await self._store.start()
            players, stats, sessions = await self._store.load_all()
        except StoreUnavailable as exc:
            self._store_ready = False
            self._log.warning("排行榜数据库不可用（%s），榜单降级为不可用", type(exc).__name__)
        except Exception as exc:  # 未预期错误同样只降级本功能
            self._store_ready = False
            self._log.warning("排行榜数据库初始化失败（%s），榜单降级为不可用", error_kind(exc))
        else:
            self._store_ready = True
            for item in players:
                self._players[(item.server, item.uuid)] = _PlayerState(
                    item.name, item.first_seen, item.last_seen
                )
            for item in stats:
                self._stats[(item.server, item.uuid)] = _StatState(
                    item.ticks, item.deaths, item.updated_at
                )
            # 会话不跨进程续算：重启后由权威 RCON 名单重新补开（需求 §9 / 风险节）
            if sessions:
                try:
                    await self._store.apply(StatsPatch(clear_sessions=True))
                except Exception as exc:
                    self._log.warning("重启清理残留会话失败（%s）", error_kind(exc))
        self._started = True
        if self._store_ready:
            await self._scan_all()
        if self._loops:
            self._tasks = [
                asyncio.create_task(self._scan_loop(), name="leaderboard-scan"),
                asyncio.create_task(self._reconcile_loop(), name="leaderboard-reconcile"),
            ]

    async def stop(self) -> None:
        self._stopping = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        aux, self._aux_tasks = list(self._aux_tasks), set()
        for task in aux:
            task.cancel()
        if aux:
            await asyncio.gather(*aux, return_exceptions=True)
        if self._event_task is not None:
            self._event_task.cancel()
            await asyncio.gather(self._event_task, return_exceptions=True)
            self._event_task = None
        self._events.clear()
        if self._store is not None:
            await self._store.stop()
        self._store_ready = False
        self._started = False

    # -- 事件入口（非阻塞） ------------------------------------------------

    def offer_event(self, event: MCEvent) -> None:
        """join/leave 事件入口：只入队并唤醒处理任务，绝不阻塞 MC 转发链路。"""
        if event.type not in ("join", "leave"):
            return  # 死亡数只以权威 stats 文件为准
        if not self._started or self._stopping or not self._store_ready:
            return
        if event.server not in self._scanners:
            return
        self._events.append(event)
        if self._event_task is None or self._event_task.done():
            try:
                self._event_task = asyncio.create_task(self._drain_events(), name="leaderboard-events")
            except RuntimeError:  # pragma: no cover - 无运行循环时忽略
                self._events.clear()

    async def _drain_events(self) -> None:
        while self._events:
            event = self._events.popleft()
            try:
                await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("排行榜会话事件处理失败（%s）", error_kind(exc))

    def on_connection_change(self, server: str, connected: bool) -> None:
        """QueQiao 连接状态回调：断开立刻收敛该服会话，连上立刻唤醒对账。"""
        if not self._started or self._stopping:
            return
        if server not in self._scanners:
            return
        if connected:
            self._wake.set()
            return
        self._server_state.setdefault(server, _ServerState()).degraded = True
        self._schedule(self._close_server_sessions(server, self._clock()))

    # -- 对外查询 ----------------------------------------------------------

    async def get_board(
        self, kind: LeaderboardKind, server: str | None = None
    ) -> Leaderboard:
        if kind not in ("deaths", "playtime"):
            raise ValueError("未知榜单类型")
        if not self._started:
            raise LeaderboardUnavailable("排行榜数据暂不可用")
        if self._active and not self._store_ready:
            raise LeaderboardUnavailable("排行榜数据暂不可用")
        selected = []
        if server:
            configured = self.config.server(server)
            if configured is None or not configured.enabled:
                raise UnknownServerError(server)
            selected = [configured]
        else:
            selected = self.config.enabled_servers()

        now = self._clock()
        boards: list[ServerBoard] = []
        configured_sources = 0
        unavailable_sources = 0
        for item in selected:
            board = self._build_server_board(item.name, kind, now)
            if item.stats_dir:
                configured_sources += 1
                state = self._server_state[item.name]
                if state.status == "unavailable" and not state.has_history:
                    unavailable_sources += 1
                    board = ServerBoard(server=item.name, rows=[], total_players=0)
            boards.append(board)
        if configured_sources and unavailable_sources == configured_sources:
            raise LeaderboardUnavailable("排行榜数据暂不可用")
        return Leaderboard(kind=kind, generated_at=utc_now_text(now), servers=boards)

    def _build_server_board(self, server: str, kind: str, now: float) -> ServerBoard:
        state = self._server_state.get(server) or _ServerState()
        if state.status == "missing":
            # 目录不存在：不展示旧 DB 行（需求 §10）
            return ServerBoard(server=server, rows=[], total_players=0)
        cache: UserCache = self._scanners[server].usercache() if server in self._scanners else UserCache()
        totals: dict[str, int] = {}
        online_names: set[str] = set()
        keys: Iterable[tuple[str, str]] = {
            key for key in list(self._stats) + list(self._sessions) if key[0] == server
        }
        for key in keys:
            uuid_value = key[1]
            player = self._players.get(key)
            name = player.name if player and player.name else merge_usercache_names(cache, uuid_value)
            stat = self._stats.get(key)
            if kind == "deaths":
                value = stat.deaths if stat else 0
            else:
                value = stat.ticks // 20 if stat else 0
                session = self._sessions.get(key)
                if session is not None:
                    online_names.add(name)
                    value += max(0, int(self._session_cap(session, state, now) - session.started_at))
            totals[name] = totals.get(name, 0) + value

        ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        rows: list[RankRow] = []
        for index, (name, value) in enumerate(ordered, start=1):
            online = kind == "playtime" and name in online_names
            if kind == "deaths":
                display = format_deaths(value)
            else:
                display = format_duration(value)
                if online:
                    display = f"{display}（在线中）"
            rows.append(
                RankRow(rank=index, player=name, value=value, display=display, online=online)
            )
        return ServerBoard(server=server, rows=rows, total_players=len(rows))

    def _session_horizon(self, session: _SessionState, state: _ServerState, now: float) -> float:
        """**正常在线**会话的加时封顶（服务端最近一轮对账成功时使用）。

        「最后确证在线」= max(会话证据时刻, 该服最近一次成功 RCON 名单时刻)；
        封顶 = 它 + 一个对账周期，且不超过当前时刻。
        """
        evidence = session.evidence_at
        if state.last_verified_at is not None:
            evidence = max(evidence, state.last_verified_at)
        return min(now, evidence + MAX_ONLINE_EXTRAPOLATION_SECONDS)

    def _convergence_cap(self, session: _SessionState, state: _ServerState, now: float) -> float:
        """**对账失败/无法确证**时的严格封顶（展示与落盘共用）。

        一旦该服 RCON 失败（或桥接断开），最后一次「确证在线」只能是该服最后一次
        成功拿到 ``list`` 的时刻，**不再用会话证据时刻延长**——否则「成功 list 之后
        才 join 的会话」会把封顶往后推，突破验收要求的 ``last_verified_at + 60s``。
        从未成功对账过（``last_verified_at is None``）才退回会话自身的证据时刻，
        避免启动即失败时把全部会话一律记成 0 秒。
        """
        base = state.last_verified_at
        if base is None:
            base = session.evidence_at
        return min(now, base + RECONCILE_INTERVAL_SECONDS)

    def _session_cap(self, session: _SessionState, state: _ServerState, now: float) -> float:
        """按该服当前是否可确证选择封顶口径（展示与落盘必须同源）。"""
        if state.degraded:
            return self._convergence_cap(session, state, now)
        return self._session_horizon(session, state, now)

    # -- 扫描 --------------------------------------------------------------

    async def _scan_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)
                if self._stopping:
                    break
                await self._scan_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单轮异常不能杀死循环
                self._log.warning("排行榜扫描循环异常（%s）", error_kind(exc))

    async def _scan_all(self) -> None:
        for server in self.config.enabled_servers():
            if not server.stats_dir or self._stopping:
                continue
            try:
                await self._scan_server(server.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("排行榜扫描失败（%s）：%s", server.name, error_kind(exc))

    async def _scan_server(self, server: str) -> None:
        scanner = self._scanners.get(server)
        if scanner is None:
            return
        known = {uuid_value for (name, uuid_value) in self._stats if name == server}
        result = await asyncio.to_thread(scanner.scan, known_uuids=known)
        state = self._server_state.setdefault(server, _ServerState())
        if result.kind == "unavailable":
            state.status = "unavailable"
            self._log.warning("排行榜数据源不可用（%s），保留上次有效值", server)
            return
        if result.kind == "missing":
            state.status = "missing"
            return

        cache = scanner.usercache()
        now = self._clock()
        stamp = utc_now_text(now)
        patch = StatsPatch()
        anchors: dict[tuple[str, str], float] = {}
        names: dict[tuple[str, str], str] = {}
        async with self._lock:
            for entry in result.entries:
                key = (server, entry.uuid)
                previous = self._stats.get(key)
                existing_player = self._players.get(key)
                display = cache.uuid_to_name.get(entry.uuid) or (
                    existing_player.name if existing_player and existing_player.name else ""
                )
                if not display:
                    display = merge_usercache_names(cache, entry.uuid)
                first_seen = existing_player.first_seen if existing_player else stamp
                patch.stats.append(StatsRow(server, entry.uuid, entry.ticks, entry.deaths, stamp))
                patch.players.append(PlayerRow(server, entry.uuid, display, first_seen, stamp))
                names[key] = display
                session = self._sessions.get(key)
                if session is None:
                    continue
                if previous is None or entry.ticks != previous.ticks:
                    if previous is not None and entry.ticks < previous.ticks:
                        # 世界被替换/回档：基线随新的权威值重置，绝不用 MAX 保住旧值
                        anchor = now
                    else:
                        # 文件已包含到落盘时刻的时长：anchor 推进到 mtime，防止双计
                        anchor = min(max(entry.mtime, session.started_at), now)
                    patch.anchors.append((server, entry.uuid, utc_now_text(anchor)))
                    anchors[key] = anchor
            for uuid_value in result.removed:
                patch.removed.append((server, uuid_value))
            # usercache 改名（stats 未变）也要更新展示名
            for key, player in list(self._players.items()):
                if key[0] != server:
                    continue
                new_name = cache.uuid_to_name.get(key[1])
                if new_name and new_name != player.name:
                    patch.players.append(
                        PlayerRow(server, key[1], new_name, player.first_seen, stamp)
                    )
                    names[key] = new_name
            if not await self._persist(patch):
                # 事务失败：不推进 mtime 缓存、不改内存，下一轮重试
                return

            scanner.commit(result)
            for entry in result.entries:
                key = (server, entry.uuid)
                self._stats[key] = _StatState(entry.ticks, entry.deaths, stamp)
                existing_player = self._players.get(key)
                self._players[key] = _PlayerState(
                    names.get(key, existing_player.name if existing_player else entry.uuid[:8] + "?"),
                    existing_player.first_seen if existing_player else stamp,
                    stamp,
                )
            for key, display in names.items():
                player = self._players.get(key)
                if player is not None and display != player.name:
                    player.name = display
            for uuid_value in result.removed:
                key = (server, uuid_value)
                self._stats.pop(key, None)
                self._players.pop(key, None)
                self._sessions.pop(key, None)
            for key, anchor in anchors.items():
                session = self._sessions.get(key)
                if session is not None:
                    session.started_at = anchor
            state.has_history = True
            state.status = "ok"

    async def scan_once(self) -> None:
        """供测试/运维调度的单轮全量增量扫描。"""
        await self._scan_all()

    # -- 会话 --------------------------------------------------------------

    async def _handle_event(self, event: MCEvent) -> None:
        server = event.server
        scanner = self._scanners.get(server)
        if scanner is None:
            return
        cache = scanner.usercache()
        raw_uuid = normalize_uuid(event.uuid)
        display = ""
        if raw_uuid:
            display = cache.uuid_to_name.get(raw_uuid) or event.player
        else:
            raw_uuid = cache.name_to_uuid.get(event.player, "")
            if not raw_uuid:
                # RCON/事件都没有 UUID 证据：不伪造 UUID、不生成玩家行
                self._log.debug("排行榜无法确定玩家 UUID（%s），暂不建立会话", server)
                return
            display = cache.uuid_to_name.get(raw_uuid) or event.player
        key = (server, raw_uuid)
        now = self._clock()

        if event.type == "join":
            async with self._lock:
                session = self._sessions.get(key)
                if session is not None:
                    session.evidence_at = now  # 重复 join 幂等
                    return
                patch = StatsPatch(
                    open_sessions=[SessionRow(server, raw_uuid, display, utc_now_text(now))]
                )
                if key not in self._players:
                    patch.players.append(PlayerRow(server, raw_uuid, display, utc_now_text(now), utc_now_text(now)))
                if not await self._persist(patch):
                    return
                self._sessions[key] = _SessionState(display, now, now)
                if key not in self._players:
                    self._players[key] = _PlayerState(display, utc_now_text(now), utc_now_text(now))
            return

        # leave：按 UUID 关闭；缺 UUID 时只在名字能唯一定位现有会话时才关闭
        async with self._lock:
            targets = [key] if key in self._sessions else []
            if not targets and not normalize_uuid(event.uuid):
                targets = [
                    candidate
                    for candidate, session in self._sessions.items()
                    if candidate[0] == server and session.name == event.player
                ]
            if not targets:
                return
            patch = StatsPatch(close_sessions=list(targets))
            if not await self._persist(patch):
                return
            for candidate in targets:
                self._sessions.pop(candidate, None)
        # 退出后 MC 会落盘 stats：安排一次该服增量扫描，让权威值接上
        self._schedule(self._scan_server(server))

    async def _close_server_sessions(self, server: str, now: float, *, reason: str = "连接断开") -> int:
        """关闭该服所有在线会话，并把封顶内的估算增量折进 ``player_stats.ticks``。

        只在「无法确证在线名单」时调用（RCON 查询失败 / 桥接断开），所以封顶走
        :meth:`_convergence_cap`：严格以该服 ``last_verified_at + 一个对账周期`` 为界，
        不被会话证据时刻延长（成功 ``list`` 之后才 join 的会话也不延长）。
        展示端在 ``degraded`` 时用同一个函数（:meth:`_session_cap`），落盘与显示同口径。
        落盘失败时本轮整体不动、下一轮重试，会话保持打开。
        """
        state = self._server_state.setdefault(server, _ServerState())
        async with self._lock:
            keys = [key for key in self._sessions if key[0] == server]
            if not keys:
                return 0
            patch = StatsPatch()
            folded: list[tuple[tuple[str, str], int | None]] = []
            for key in keys:
                session = self._sessions[key]
                cap = self._convergence_cap(session, state, now)
                extra_seconds = max(0, int(cap - session.started_at))
                stat = self._stats.get(key)
                ticks = (stat.ticks if stat else 0) + extra_seconds * 20
                patch.close_sessions.append(key)
                folded.append((key, ticks if extra_seconds else None))
                if extra_seconds:
                    patch.stats.append(
                        StatsRow(
                            server,
                            key[1],
                            ticks,
                            stat.deaths if stat else 0,
                            utc_now_text(cap),
                        )
                    )
            if not await self._persist(patch):
                return 0
            for key, ticks in folded:
                self._sessions.pop(key, None)
                if ticks is None:
                    continue
                stat = self._stats.get(key)
                if stat is None:
                    self._stats[key] = _StatState(ticks, 0, utc_now_text(now))
                else:
                    stat.ticks = ticks
                    stat.updated_at = utc_now_text(now)
        self._log.info("排行榜已关闭 %s 的 %d 个在线会话（%s）", server, len(keys), reason)
        return len(keys)

    # -- 对账 --------------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        while not self._stopping:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log.warning("排行榜对账循环异常（%s）", error_kind(exc))
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=RECONCILE_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            self._wake.clear()

    async def reconcile_once(self) -> None:
        """一轮 RCON ``list`` 对账：补齐漏开的会话、关闭残留会话。

        * 成功拿到名单 → 记录该服 ``last_verified_at``（本轮封顶基准）。
        * 查询失败/抛异常 → **不跳过**：以 ``last_verified_at + 一个对账周期`` 封顶
          关闭该服所有会话，并把封顶内的增量折进 ``player_stats.ticks``
          （展示与落盘同一口径，恢复名单后不会补加这段时间）。
        """
        if not self._started or not self._store_ready:
            return
        now = self._clock()
        for server in self.config.enabled_servers():
            if not server.stats_dir or self._stopping:
                continue
            if server.name not in self._scanners:
                continue
            if not self._is_connected(server.name):
                self._server_state.setdefault(server.name, _ServerState()).degraded = True
                await self._close_server_sessions(server.name, now, reason="连接断开")
                continue
            # 查询前先冻结「本代次已存在的会话」：查询期间新开的会话不能被旧快照删掉
            before = {key for key in self._sessions if key[0] == server.name}
            names, reason = await self._online_names(server.name)
            if names is None:
                # 无法确证名单：严格按 last_verified_at + 一个对账周期封顶收敛
                state = self._server_state.setdefault(server.name, _ServerState())
                state.degraded = True
                closed = await self._close_server_sessions(
                    server.name, self._clock(), reason=f"对账失败（{reason}）"
                )
                self._log.warning(
                    "排行榜对账失败（%s，原因=%s），已按「最后确证在线 + %d 秒」封顶关闭 %d 个会话",
                    server.name,
                    reason,
                    RECONCILE_INTERVAL_SECONDS,
                    closed,
                )
                continue
            # 成功拿到 list 的时刻：失败的下一轮用它做封顶基准
            state = self._server_state.setdefault(server.name, _ServerState())
            state.last_verified_at = self._clock()
            state.degraded = False
            cache = self._scanners[server.name].usercache()
            online: dict[str, str] = {}
            online_names: list[str] = [str(item) for item in names]
            for name in online_names:
                uuid_value = cache.name_to_uuid.get(name, "")
                if not uuid_value:
                    # RCON 只有名字：无 usercache 映射时不伪造 UUID
                    continue
                online[uuid_value] = cache.uuid_to_name.get(uuid_value) or name
            await self._apply_online_set(server.name, online, now, candidates=before)

    async def _apply_online_set(
        self,
        server: str,
        online: dict[str, str],
        now: float,
        *,
        candidates: set[tuple[str, str]] | None = None,
    ) -> None:
        async with self._lock:
            patch = StatsPatch()
            for uuid_value, display in online.items():
                key = (server, uuid_value)
                session = self._sessions.get(key)
                if session is not None:
                    continue  # 已有会话：等提交成功后再刷新 evidence
                patch.open_sessions.append(
                    SessionRow(server, uuid_value, display, utc_now_text(now))
                )
                if key not in self._players:
                    patch.players.append(
                        PlayerRow(server, uuid_value, display, utc_now_text(now), utc_now_text(now))
                    )
            # 只关闭「查询开始前就存在、且不在本次名单里」的会话：
            # 查询期间新开的会话（并发 join）不能被旧快照删掉。
            scope = candidates if candidates is not None else {key for key in self._sessions if key[0] == server}
            stale = [key for key in scope if key in self._sessions and key[1] not in online]
            patch.close_sessions = stale
            if not await self._persist(patch):
                return
            for uuid_value, display in online.items():
                key = (server, uuid_value)
                session = self._sessions.get(key)
                if session is None:
                    self._sessions[key] = _SessionState(display, now, now)
                else:
                    session.evidence_at = now
                if key not in self._players:
                    self._players[key] = _PlayerState(display, utc_now_text(now), utc_now_text(now))
            for key in stale:
                self._sessions.pop(key, None)

    # -- 内部工具 ----------------------------------------------------------

    async def _online_names(self, server: str) -> tuple[list[str] | None, str]:
        """取权威在线名单；失败返回 ``(None, 原因类型)``（绝不当作「空名单」）。"""
        query = getattr(self.manager, "query_rcon", None)
        try:
            if query is not None:
                results = await query([server])
                for result in results:
                    if result.name != server:
                        continue
                    if result.status == "online":
                        return [str(item) for item in result.players], "ok"
                    return None, f"rcon_{result.status}"
                return None, "rcon_no_result"
            client = self._client(server)
            if client is None:
                return None, "no_client"
            snapshot = await client.query_players(rcon_only=True)
            return [str(item) for item in list(snapshot.players)], "ok"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, error_kind(exc)

    def _client(self, server: str) -> Any:
        getter = getattr(self.manager, "client", None)
        if getter is None:
            return None
        try:
            return getter(server)
        except Exception:  # pragma: no cover - 防御性
            return None

    def _is_connected(self, server: str) -> bool:
        client = self._client(server)
        return bool(client is not None and getattr(client, "connected", False))

    def _schedule(self, coro: Any) -> None:
        """后台辅助任务：异常隔离，任务集合有界。"""
        try:
            task = asyncio.create_task(coro, name="leaderboard-aux")
        except RuntimeError:  # pragma: no cover
            return
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)

    async def _persist(self, patch: StatsPatch) -> bool:
        if patch.is_empty():
            return True
        if self._store is None:
            return False
        try:
            await self._store.apply(patch)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning("排行榜数据写入失败（%s），本轮不推进扫描缓存", error_kind(exc))
            return False