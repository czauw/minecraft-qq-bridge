"""排行榜持久化：一个 SQLite 文件、一个连接、一个串行作业队列。

设计约束（对应 ``docs/LEADERBOARD_REQUIREMENTS.md`` §8.3 / §9）：

* 表结构与需求 §8.3 **完全一致**（``players`` / ``player_stats`` / ``sessions``），
  不新增业务表或业务列。
* 只有一个实际连接；所有访问都投递到同一个 asyncio 队列，由唯一消费者用
  ``asyncio.to_thread`` 串行执行，因此不会出现多线程并发访问同一连接。
* 写失败只抛给调用方（由 service 记受控日志），绝不伪造数据、也不把失败当成功。
* 打开时做完整性检查；确认损坏则把旧库重命名隔离后重建（stats 是权威源，可重算）。
  权限不足/磁盘满不当作“损坏”，直接降级为不可用。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "PlayerRow",
    "SessionRow",
    "StatsPatch",
    "StatsRow",
    "StatsStore",
    "StoreUnavailable",
]

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS players (
      server TEXT, uuid TEXT, name TEXT,
      first_seen TEXT, last_seen TEXT,
      PRIMARY KEY (server, uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS player_stats (
      server TEXT, uuid TEXT,
      ticks INTEGER NOT NULL DEFAULT 0,
      deaths INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (server, uuid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      server TEXT, uuid TEXT, name TEXT,
      started_at TEXT NOT NULL,
      PRIMARY KEY (server, uuid)
    )
    """,
)


class StoreUnavailable(Exception):
    """数据库不可用（无法打开、目录不可写等）；调用方应降级而不是伪造数据。"""


@dataclass(frozen=True)
class PlayerRow:
    server: str
    uuid: str
    name: str
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class StatsRow:
    server: str
    uuid: str
    ticks: int
    deaths: int
    updated_at: str


@dataclass(frozen=True)
class SessionRow:
    server: str
    uuid: str
    name: str
    started_at: str


@dataclass
class StatsPatch:
    """一次事务内的全部变更；任何一步失败则整批回滚（不推进扫描缓存）。"""

    stats: list[StatsRow] = field(default_factory=list)
    players: list[PlayerRow] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    open_sessions: list[SessionRow] = field(default_factory=list)
    close_sessions: list[tuple[str, str]] = field(default_factory=list)
    anchors: list[tuple[str, str, str]] = field(default_factory=list)
    clear_sessions: bool = False

    def is_empty(self) -> bool:
        return not (
            self.stats
            or self.players
            or self.removed
            or self.open_sessions
            or self.close_sessions
            or self.anchors
            or self.clear_sessions
        )


class StatsStore:
    def __init__(self, path: str, *, logger: logging.Logger | None = None) -> None:
        self.path = path
        self._log = logger or logging.getLogger("mcqq_bridge.stats_store")
        self._conn: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[tuple[asyncio.Future[Any], Callable[[sqlite3.Connection], Any]]] = asyncio.Queue(
            maxsize=256
        )
        self._worker: asyncio.Task[None] | None = None
        self._inflight: asyncio.Future[Any] | None = None
        self._closed = False

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        if self._worker is not None:
            return
        await asyncio.to_thread(self._open_sync)
        self._closed = False
        self._worker = asyncio.create_task(self._consume(), name="stats-store")

    async def stop(self) -> None:
        self._closed = True
        worker, self._worker = self._worker, None
        # 先记下在途作业：取消消费者会让线程继续跑，所以必须等它跑完再关连接
        inflight = self._inflight
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if inflight is not None and not inflight.done():
            try:
                await asyncio.wait_for(asyncio.shield(inflight), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # pragma: no cover - 作业自身的异常已交给调用方
                pass
        self._inflight = None
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(_close_connection, conn)

    # -- 同步实现（只在 to_thread 里执行） ---------------------------------

    def _open_sync(self) -> None:
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise StoreUnavailable(f"无法创建数据库目录：{exc.strerror or exc}") from exc
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"无法打开数据库：{exc}") from exc
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()
            healthy = bool(check) and str(check[0]).lower() == "ok"
            if healthy:
                conn.executescript(";".join(SCHEMA))
                conn.commit()
        except sqlite3.DatabaseError:
            healthy = False
        if not healthy:
            _close_connection(conn)
            self._quarantine()
            try:
                conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                conn.executescript(";".join(SCHEMA))
                conn.commit()
            except sqlite3.Error as exc:  # pragma: no cover - 重建仍失败
                raise StoreUnavailable(f"数据库损坏且重建失败：{exc}") from exc
        self._conn = conn

    def _quarantine(self) -> None:
        """把损坏库连同 journal 一起改名隔离；不删除、不覆盖。"""
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        for suffix in ("", "-journal", "-wal", "-shm"):
            source = f"{self.path}{suffix}"
            if os.path.exists(source):
                try:
                    os.replace(source, f"{source}.corrupt-{stamp}")
                except OSError:  # pragma: no cover - 隔离失败不应阻断重建
                    self._log.warning("统计数据库损坏文件隔离失败（%s）", suffix or "main")

    # -- 串行作业队列 ------------------------------------------------------

    async def _consume(self) -> None:
        while True:
            future, fn = await self._queue.get()
            conn = self._conn
            if conn is None or self._closed:
                if not future.done():
                    future.set_exception(StoreUnavailable("数据库已关闭"))
                continue
            job = asyncio.ensure_future(asyncio.to_thread(fn, conn))
            self._inflight = job
            try:
                # shield：消费者被取消时线程要继续跑完，stop() 才能安全关闭连接
                result = await asyncio.shield(job)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except BaseException as exc:  # 交给调用方处理（含 StoreUnavailable / sqlite3.Error）
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self._inflight = None

    async def _submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = self._conn
        if conn is None or self._closed:
            raise StoreUnavailable("数据库尚未就绪")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((future, fn))
        return await future

    # -- 对外操作 ----------------------------------------------------------

    async def load_all(self) -> tuple[list[PlayerRow], list[StatsRow], list[SessionRow]]:
        players = await self._submit(
            lambda conn: [
                PlayerRow(*row)
                for row in conn.execute("SELECT server, uuid, name, first_seen, last_seen FROM players")
            ]
        )
        stats = await self._submit(
            lambda conn: [
                StatsRow(*row)
                for row in conn.execute("SELECT server, uuid, ticks, deaths, updated_at FROM player_stats")
            ]
        )
        sessions = await self._submit(
            lambda conn: [
                SessionRow(*row) for row in conn.execute("SELECT server, uuid, name, started_at FROM sessions")
            ]
        )
        return list(players), list(stats), list(sessions)

    async def apply(self, patch: StatsPatch) -> None:
        if patch.is_empty():
            return
        await self._submit(lambda conn: _apply_patch(conn, patch))


def _apply_patch(conn: sqlite3.Connection, patch: StatsPatch) -> None:
    with conn:  # 单事务：失败自动回滚
        if patch.clear_sessions:
            conn.execute("DELETE FROM sessions")
        for server, uuid_value, started_at in patch.anchors:
            conn.execute(
                "UPDATE sessions SET started_at = ? WHERE server = ? AND uuid = ?",
                (started_at, server, uuid_value),
            )
        for row in patch.stats:
            conn.execute(
                """
                INSERT INTO player_stats (server, uuid, ticks, deaths, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(server, uuid) DO UPDATE SET
                  ticks = excluded.ticks,
                  deaths = excluded.deaths,
                  updated_at = excluded.updated_at
                """,
                (row.server, row.uuid, row.ticks, row.deaths, row.updated_at),
            )
        for row in patch.players:
            conn.execute(
                """
                INSERT INTO players (server, uuid, name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(server, uuid) DO UPDATE SET
                  name = excluded.name,
                  first_seen = players.first_seen,
                  last_seen = excluded.last_seen
                """,
                (row.server, row.uuid, row.name, row.first_seen, row.last_seen),
            )
        for server, uuid_value in patch.removed:
            conn.execute("DELETE FROM player_stats WHERE server = ? AND uuid = ?", (server, uuid_value))
            conn.execute("DELETE FROM players WHERE server = ? AND uuid = ?", (server, uuid_value))
            conn.execute("DELETE FROM sessions WHERE server = ? AND uuid = ?", (server, uuid_value))
        for row in patch.open_sessions:
            conn.execute(
                """
                INSERT INTO sessions (server, uuid, name, started_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(server, uuid) DO NOTHING
                """,
                (row.server, row.uuid, row.name, row.started_at),
            )
        for server, uuid_value in patch.close_sessions:
            conn.execute("DELETE FROM sessions WHERE server = ? AND uuid = ?", (server, uuid_value))


def _close_connection(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except sqlite3.Error:  # pragma: no cover - 关闭期容错
        pass