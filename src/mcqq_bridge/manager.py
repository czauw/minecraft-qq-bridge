"""多服务器注册表：每个启用的服务器一个独立 QueQiaoClient / 任务 / 快照。

* 禁用项只保留配置，不建连接、不广播、不参与查询（/list 显示为「未启用」）。
* 查询与广播都按服务器隔离：单台断线、坏包、慢查询不会影响其它服务器。
* 所有结果按配置顺序返回，供 /list 稳定排序。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from .config import AppConfig, ServerConfig
from .queqiao import MCEventCallback, PlayerQueryError, QueQiaoClient

__all__ = ["ServerManager", "ServerQueryResult"]

QueryStatus = Literal["online", "offline", "disabled"]


@dataclass
class ServerQueryResult:
    name: str
    enabled: bool
    connected: bool
    status: QueryStatus
    players: list[str] = field(default_factory=list)
    max_players: int | None = None
    source: str = ""
    detail: str = ""


class ServerManager:
    def __init__(self, config: AppConfig, on_mc_event: MCEventCallback) -> None:
        self.config = config
        self._on_mc_event = on_mc_event
        self._clients: dict[str, QueQiaoClient] = {
            server.name: QueQiaoClient(server, on_mc_event)
            for server in config.servers
            if server.enabled
        }
        self._log = logging.getLogger("mcqq_bridge.manager")

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        if not self._clients:
            self._log.warning("没有启用的服务器，不会连接任何 QueQiao")
        for name, client in self._clients.items():
            self._log.info("启动服务器连接：%s", name)
            await client.start()

    async def stop(self) -> None:
        await asyncio.gather(*(client.close() for client in self._clients.values()), return_exceptions=True)

    def client(self, name: str) -> QueQiaoClient | None:
        return self._clients.get(name)

    @property
    def connected_count(self) -> int:
        return sum(1 for client in self._clients.values() if client.connected)

    # -- 查询 --------------------------------------------------------------

    async def query(self, names: list[str] | None = None) -> list[ServerQueryResult]:
        """按配置顺序查询在线玩家名单。

        ``names`` 为 None 时查询全部**启用的**服务器；禁用项既不发起请求，
        也不会出现在返回结果里（``/list`` 不展示禁用服务器）。
        """
        selected = [
            server
            for server in self.config.servers
            if server.enabled and (names is None or server.name in names)
        ]
        tasks: dict[str, asyncio.Task[ServerQueryResult]] = {}
        results: dict[str, ServerQueryResult] = {}
        for server in selected:
            tasks[server.name] = asyncio.create_task(
                self._query_one(server), name=f"query-{server.name}"
            )

        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, outcome in zip(tasks.keys(), done):
                if isinstance(outcome, ServerQueryResult):
                    results[name] = outcome
                elif isinstance(outcome, BaseException):
                    client = self._clients.get(name)
                    self._log.warning("%s 玩家查询异常：%s", name, type(outcome).__name__)
                    results[name] = ServerQueryResult(
                        name=name,
                        enabled=True,
                        connected=bool(client and client.connected),
                        status="offline",
                        detail="查询异常",
                    )
                else:  # pragma: no cover - gather 不会给出其它类型
                    results[name] = ServerQueryResult(
                        name=name, enabled=True, connected=False, status="offline", detail="未知结果"
                    )

        ordered = [results[server.name] for server in selected if server.name in results]
        return ordered

    async def _query_one(self, server: ServerConfig) -> ServerQueryResult:
        client = self._clients.get(server.name)
        if client is None:  # pragma: no cover - 只有启用项才建 client
            return ServerQueryResult(
                name=server.name, enabled=True, connected=False, status="offline", detail="未建连接"
            )
        if not client.connected:
            return ServerQueryResult(
                name=server.name, enabled=True, connected=False, status="offline", detail="未连接"
            )
        try:
            snapshot = await client.query_players()
        except PlayerQueryError as exc:
            self._log.warning("%s 玩家查询失败：%s", server.name, exc)
            return ServerQueryResult(
                name=server.name,
                enabled=True,
                connected=client.connected,
                status="offline",
                detail=str(exc)[:200],
            )
        except Exception as exc:  # 单服异常隔离
            self._log.warning("%s 玩家查询异常：%s", server.name, type(exc).__name__)
            return ServerQueryResult(
                name=server.name,
                enabled=True,
                connected=client.connected,
                status="offline",
                detail="查询异常",
            )
        return ServerQueryResult(
            name=server.name,
            enabled=True,
            connected=True,
            status="online",
            players=list(snapshot.players),
            max_players=snapshot.max_players,
            source=snapshot.source,
        )

    # -- 广播 --------------------------------------------------------------

    def online_targets(self, names: list[str] | None = None) -> list[str]:
        """当前可广播的服务器名（启用且已连接），按配置顺序。"""
        return [
            name
            for name, client in self._clients.items()
            if (names is None or name in names) and client.connected
        ]

    async def broadcast(self, text: str, names: list[str] | None = None) -> dict[str, bool]:
        """向已连接且启用的服务器广播；返回每台服务器的成功结果。"""
        targets = [
            client
            for name, client in self._clients.items()
            if (names is None or name in names) and client.connected
        ]
        if not targets:
            return {}
        results = await asyncio.gather(
            *(client.broadcast(text) for client in targets), return_exceptions=True
        )
        outcome: dict[str, bool] = {}
        for client, result in zip(targets, results):
            if isinstance(result, BaseException):
                self._log.warning("%s 广播异常：%s", client.server.name, type(result).__name__)
                outcome[client.server.name] = False
            else:
                outcome[client.server.name] = bool(result)
        return outcome

    # -- 状态 --------------------------------------------------------------

    def status_payload(self) -> dict[str, Any]:
        return {
            "servers": [
                self._clients[server.name].status_payload()
                if server.enabled and server.name in self._clients
                else {
                    "name": server.name,
                    "enabled": server.enabled,
                    "host": server.host,
                    "port": server.port,
                    "connected": False,
                    "players": 0,
                    "player_source": "",
                    "last_sync_ok": False,
                    "reconnects": 0,
                    "error": "",
                }
                for server in self.config.servers
            ]
        }
