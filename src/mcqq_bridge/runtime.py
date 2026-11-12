"""运行时装配：依赖注入、日志、健康/就绪状态、优雅关闭。

所有默认启动入口（``python -m mcqq_bridge``、Makefile、Dockerfile、compose）
都走这里，启动的是官方 QQ + QueQiao 运行时，不会回退旧 OneBot 入口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, cast

import uvicorn
from fastapi import FastAPI, HTTPException

from .config import AppConfig, setup_logging
from .events import MCEvent, QQMessage
from .leaderboard_models import LeaderboardUnavailable, UnknownServerError
from .leaderboards import LeaderboardService
from .manager import ServerManager
from .qq_official import QQBotClient
from .router import BridgeRouter

__all__ = ["BridgeRuntime", "create_status_app"]

logger = logging.getLogger("mcqq_bridge.runtime")


class _QuietUvicornServer(uvicorn.Server):
    """不接管进程信号，由 :class:`BridgeRuntime` 统一处理 SIGTERM/SIGINT。"""

    def install_signal_handlers(self) -> None:  # pragma: no cover - 仅运行时路径
        return None


class BridgeRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        # 统计服务先建（管理器要用它的连接状态回调），随后把 manager 注入回去
        self.leaderboards = LeaderboardService(config)
        self.manager = ServerManager(
            config, self._on_mc_event, on_connection_change=self.leaderboards.on_connection_change
        )
        self.leaderboards.manager = self.manager
        self.qq = QQBotClient(config.bot, config.relay, self._on_qq_message)
        self.router = BridgeRouter(config, self.manager, self.qq, leaderboards=self.leaderboards)
        self._status_server: _QuietUvicornServer | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._started = False

    # -- adapter 回调 ------------------------------------------------------

    async def _on_mc_event(self, event: MCEvent) -> None:
        # 统计钩子在 relay 开关之前：关闭「进/退服转发」也照常统计在线时长
        self.leaderboards.offer_event(event)
        await self.router.on_mc_event(event)

    async def _on_qq_message(self, message: QQMessage) -> None:
        await self.router.on_qq_message(message)

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        setup_logging(self.config.logging.level, self.config.logging.file)
        logger.info(
            "启动 mcqq-bridge：%d 个服务器（%d 启用），QQ 接收模式=%s，主动消息=%s",
            len(self.config.servers),
            len(self.config.enabled_servers()),
            self.config.bot.receive_mode,
            "启用" if self.config.bot.proactive_enabled else "关闭",
        )
        for server in self.config.servers:
            logger.info(
                "服务器配置：%s -> %s:%d enabled=%s query=%s",
                server.name,
                server.host,
                server.port,
                server.enabled,
                server.players.mode,
            )
        await self.leaderboards.start()
        await self.manager.start()
        await self.qq.start()
        self._start_status_server()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        logger.info("正在停止 mcqq-bridge")
        await self.qq.close()
        await self.manager.stop()
        await self._stop_status_server()
        await self.leaderboards.stop()
        self._started = False
        logger.info("已停止")

    # -- 状态 HTTP ---------------------------------------------------------

    def _start_status_server(self) -> None:
        settings = self.config.runtime.status_http
        if not settings.enabled:
            logger.info("状态 HTTP 已禁用")
            return
        app = create_status_app(self)
        server = _QuietUvicornServer(
            uvicorn.Config(
                app,
                host=settings.host,
                port=settings.port,
                log_level="warning",
                access_log=False,
                lifespan="off",
            )
        )
        self._status_server = server
        self._status_task = asyncio.create_task(server.serve(), name="status-http")
        logger.info("状态 HTTP 监听 %s:%d（/healthz、/readyz、/api/v1/status）", settings.host, settings.port)

    async def _stop_status_server(self) -> None:
        server = self._status_server
        task = self._status_task
        self._status_server = None
        self._status_task = None
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=self.config.runtime.shutdown_timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- 状态快照 ----------------------------------------------------------

    def status_payload(self) -> dict[str, Any]:
        return {
            "qq": self.qq.status_payload(),
            "servers": self.manager.status_payload()["servers"],
        }


def create_status_app(runtime: BridgeRuntime) -> FastAPI:
    """只读状态接口：不暴露任何密钥、群 OpenID 或消息正文。"""
    app = FastAPI(
        title="mcqq-bridge status",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        enabled = len(runtime.config.enabled_servers())
        connected = runtime.manager.connected_count
        getter = cast(
            "Callable[[], list[str]] | None", getattr(runtime.qq, "degraded_reasons", None)
        )
        qq_degraded: list[str] = list(getter()) if getter is not None else []
        ready = runtime.qq.connected and enabled > 0 and connected > 0 and not qq_degraded
        return {
            "status": "ready" if ready else "degraded",
            "qq_connected": runtime.qq.connected,
            "servers_enabled": enabled,
            "servers_connected": connected,
            "qq_degraded": qq_degraded,
        }

    @app.get("/api/v1/status")
    async def status() -> dict[str, Any]:
        return runtime.status_payload()

    @app.get("/api/v1/leaderboards/{kind}")
    async def leaderboards(kind: str, server: str | None = None) -> dict[str, Any]:
        """只读榜单 JSON：DTO 与 /death、/playtime 文本同源（schema_version=1）。

        全量、不截断（文本层的分片只属于渲染层）；不含 UUID / openid / 路径 / 密钥。
        """
        if kind not in ("deaths", "playtime"):
            raise HTTPException(status_code=404, detail="未知榜单类型")
        service = getattr(runtime, "leaderboards", None)
        if service is None:
            raise HTTPException(status_code=503, detail="排行榜数据暂不可用")
        try:
            board = await service.get_board(kind, server or None)
        except UnknownServerError as exc:
            raise HTTPException(status_code=404, detail=f"未找到服务器：{exc.name}") from exc
        except LeaderboardUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return board.to_dict()

    return app
