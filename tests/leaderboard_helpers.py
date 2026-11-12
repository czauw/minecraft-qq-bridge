"""排行榜测试公共构造：临时实例目录（真实 stats JSON/usercache）+ 假管理器/时钟。

全部落在 pytest 的 ``tmp_path`` 下，绝不触碰真实实例目录或默认 SQLite 落点。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from helpers import make_config, server_entry
from mcqq_bridge.events import MCEvent, PlayerSnapshot
from mcqq_bridge.manager import ServerQueryResult
from mcqq_bridge.queqiao import PlayerQueryError

# 两个固定 UUID：一个"离线模式 v3"、一个"正版 v4"，用于同名双 UUID 场景
UUID_MAIN = "6f1b0c2e-1111-3a2b-9c4d-000000000001"
UUID_MAIN_ALT = "8a2c1d3f-2222-4b3c-8d5e-000000000002"
UUID_OTHER = "9b3d2e4a-3333-4c4d-9e6f-000000000003"
UUID_THIRD = "a94c3b5d-4444-4e5f-8a70-000000000004"


def make_instance(tmp_path: Path, label: str) -> tuple[Path, Path]:
    """建一个实例目录 ``<tmp>/<label>/world/stats``，返回 (实例根, stats 目录)。"""
    root = Path(tmp_path) / label
    stats = root / "world" / "stats"
    stats.mkdir(parents=True, exist_ok=True)
    return root, stats


def write_stats(
    stats_dir: Path,
    uuid_value: str,
    *,
    ticks: int = 0,
    deaths: int = 0,
    mtime: float | None = None,
) -> Path:
    path = Path(stats_dir) / f"{uuid_value}.json"
    payload = {
        "stats": {
            "minecraft:custom": {
                "minecraft:play_time": ticks,
                "minecraft:deaths": deaths,
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_raw_stats(stats_dir: Path, uuid_value: str, raw: str, *, mtime: float | None = None) -> Path:
    path = Path(stats_dir) / f"{uuid_value}.json"
    path.write_text(raw, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_usercache(root: Path, entries: list[tuple[str, str, str]]) -> Path:
    """``entries`` = [(name, uuid, expiresOn)]，写成 Minecraft 的 usercache.json。"""
    data = [{"name": name, "uuid": uuid_value, "expiresOn": expires} for name, uuid_value, expires in entries]
    path = Path(root) / "usercache.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_lb_config(
    tmp_path: Path,
    servers: list[dict[str, Any]],
    *,
    bot: dict[str, Any] | None = None,
    relay: dict[str, Any] | None = None,
):
    """``servers`` = [{"name":..., "stats_dir":..., "enabled":...}]，自动配临时 DB。"""
    entries = []
    for index, item in enumerate(servers):
        entries.append(
            server_entry(
                item["name"],
                19000 + index,
                enabled=item.get("enabled", True),
                stats_dir=item.get("stats_dir", ""),
            )
        )
    return make_config(
        servers=entries,
        bot=bot,
        relay=relay,
        stats={"db_path": str(Path(tmp_path) / "stats.db")},
    )


class FakeClock:
    def __init__(self, now: float = 1_760_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeClient:
    def __init__(self, name: str, players: list[str] | None = None, *, connected: bool = True) -> None:
        self.name = name
        self.players = list(players or [])
        self.connected = connected
        self.fail = False
        self.queries: list[str] = []

    async def query_players(self, *, rcon_only: bool = False) -> PlayerSnapshot:
        self.queries.append("rcon" if rcon_only else "auto")
        if not self.connected:
            raise PlayerQueryError("尚未连接")
        if self.fail:
            raise PlayerQueryError("rcon 失败")
        return PlayerSnapshot(players=list(self.players), authoritative=True, source="rcon")


class FakeManager:
    """只实现排行榜需要的两个能力：连接快照 + 权威 RCON 名单。"""

    def __init__(self, clients: list[FakeClient]) -> None:
        self._clients = {client.name: client for client in clients}

    def client(self, name: str) -> FakeClient | None:
        return self._clients.get(name)

    async def query_rcon(self, names: list[str] | None = None) -> list[ServerQueryResult]:
        results: list[ServerQueryResult] = []
        for name, client in self._clients.items():
            if names is not None and name not in names:
                continue
            if not client.connected or client.fail:
                results.append(
                    ServerQueryResult(
                        name=name,
                        enabled=True,
                        connected=client.connected,
                        status="offline",
                        detail="查询失败",
                    )
                )
                continue
            results.append(
                ServerQueryResult(
                    name=name,
                    enabled=True,
                    connected=True,
                    status="online",
                    players=list(client.players),
                    source="rcon",
                )
            )
        return results


def join_event(server: str, player: str, uuid_value: str = "", event_id: str = "e1") -> MCEvent:
    return MCEvent(
        server=server, type="join", player=player, uuid=uuid_value, event_id=event_id
    )


def leave_event(server: str, player: str, uuid_value: str = "", event_id: str = "e2") -> MCEvent:
    return MCEvent(
        server=server, type="leave", player=player, uuid=uuid_value, event_id=event_id
    )


async def start_service(config, manager: Any = None, clock: Any = None):
    from mcqq_bridge.leaderboards import LeaderboardService

    service = LeaderboardService(config, manager=manager, clock=clock, loops=False)
    await service.start()
    return service