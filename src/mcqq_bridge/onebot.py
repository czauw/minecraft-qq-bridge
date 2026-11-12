from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from fastapi import WebSocket

from .ports import OutboundOneBot
from .settings import Settings

log = logging.getLogger(__name__)


class OneBotHttpAdapter(OutboundOneBot):
    """OneBot v11 HTTP API adapter; no QQ client is embedded."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=settings.onebot_api_timeout_seconds)

    async def close(self) -> None:
        await self.client.aclose()

    async def send_group_message(self, group_id: int, message: str) -> bool:
        if not self.settings.onebot_enabled or group_id not in self.settings.allowed_group_ids:
            return False
        headers = {}
        if self.settings.onebot_access_token:
            headers["Authorization"] = f"Bearer {self.settings.onebot_access_token}"
        try:
            response = await self.client.post(
                f"{self.settings.onebot_api_url.rstrip('/')}/send_group_msg",
                json={"group_id": group_id, "message": message, "auto_escape": True},
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            delivered = isinstance(body, dict) and body.get("status") == "ok" and body.get("retcode") == 0
            if not delivered:
                log.warning("OneBot HTTP API rejected message; not delivered")
            return delivered
        except (httpx.HTTPError, ValueError):
            # Do not include exception strings, URLs or credentials in logs.
            log.warning("OneBot HTTP API failed; message was not delivered")
            return False


class OneBotReverseWebSocketHub:
    """Reverse WS server: OneBot connects as Universal, or separate API/Event.

    Single bot instance, bounded in-flight API calls and matching echo/retcode
    acknowledgement. Reconnect is initiated by the external OneBot client.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._sockets: dict[str, WebSocket] = {}
        self._pending: dict[str, tuple[WebSocket, asyncio.Future[bool]]] = {}
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return bool(self._sockets.get("Universal") or self._sockets.get("API"))

    async def connect(self, websocket: WebSocket) -> bool:
        role = websocket.headers.get("x-client-role", "Universal")
        if role not in {"Universal", "API", "Event"} or role in self._sockets:
            await websocket.close(code=1008)
            return False
        if (role == "Universal" and self._sockets) or "Universal" in self._sockets:
            await websocket.close(code=1008)
            return False
        # Register before the first await so duplicate connection checks are atomic.
        self._sockets[role] = websocket
        try:
            await websocket.accept()
        except Exception:
            self._sockets.pop(role, None)
            raise
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        for role, connection in tuple(self._sockets.items()):
            if connection is websocket:
                self._sockets.pop(role, None)
        for connection, future in tuple(self._pending.values()):
            if connection is websocket and not future.done():
                future.set_result(False)

    async def close(self) -> None:
        for socket in tuple(self._sockets.values()):
            await self.disconnect(socket)
            await socket.close(code=1001)

    def resolve(self, payload: dict[str, Any], websocket: WebSocket | None = None) -> None:
        echo = payload.get("echo")
        if not isinstance(echo, str):
            return
        pending = self._pending.get(echo)
        if not pending:
            return
        connection, future = pending
        if websocket is not None and connection is not websocket:
            return
        if not future.done():
            delivered = payload.get("status") == "ok" and payload.get("retcode") == 0
            if not delivered:
                log.warning("OneBot WS API rejected message; not delivered")
            future.set_result(delivered)

    async def send_group_message(self, group_id: int, message: str) -> bool:
        if not self.settings.onebot_enabled or group_id not in self.settings.allowed_group_ids:
            return False
        socket = self._sockets.get("Universal") or self._sockets.get("API")
        if socket is None or len(self._pending) >= 32:
            return False
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = (socket, future)
        try:
            async with asyncio.timeout(self.settings.onebot_api_timeout_seconds):
                async with self._send_lock:
                    await socket.send_json({
                        "action": "send_group_msg",
                        "params": {"group_id": group_id, "message": message, "auto_escape": True},
                        "echo": echo,
                    })
                return await future
        except (TimeoutError, RuntimeError, OSError):
            log.warning("OneBot WS API unavailable or unacknowledged")
            return False
        finally:
            self._pending.pop(echo, None)
