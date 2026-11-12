"""历史兼容模块（OneBot v11 / MC HTTP webhook 时代的入口）。

⚠️ 本模块仅保留给旧回归测试使用，**不是默认启动入口**，官方 QQ + QueQiao 多服务器
运行时在 ``mcqq_bridge.runtime`` / ``python -m mcqq_bridge``。不要在新部署中启动
``uvicorn mcqq_bridge.main:app``。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import asyncio
import re
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status

from .mc_adapter import HttpMcCallback
from .hermes import DisabledHermesExecutor, HermesApiExecutor
from .models import ChatEvent, DeathEvent, PlayerEvent, PlayersEvent, StatusEvent
from .onebot import OneBotHttpAdapter, OneBotReverseWebSocketHub
from .security import bearer_matches
from .service import BridgeService
from .settings import Settings, settings as default_settings

EVENT_MODELS = {
    "chat": ChatEvent,
    "death": DeathEvent,
    "join": PlayerEvent,
    "leave": PlayerEvent,
    "players": PlayersEvent,
    "status": StatusEvent,
}


def create_app(config: Settings | None = None) -> FastAPI:
    config = config or default_settings
    http_onebot = OneBotHttpAdapter(config)
    websocket_onebot = OneBotReverseWebSocketHub(config)
    onebot = websocket_onebot if config.onebot_transport == "websocket" else http_onebot
    callback = HttpMcCallback(config)
    hermes = HermesApiExecutor(config) if config.hermes_admin_enabled else DisabledHermesExecutor()
    bridge = BridgeService(config, onebot, callback, hermes_admin=hermes)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await websocket_onebot.close()
        await http_onebot.close()
        await callback.close()
        await hermes.close()

    app = FastAPI(
        title="Minecraft ↔ QQ Bridge",
        version="0.1.0",
        description=(
            "Docker 优先的 Minecraft webhook 与 OneBot v11 adapter。"
            "核心不绑定 NapCat/AstrBot/QueQiao。"
        ),
        lifespan=lifespan,
    )
    app.state.bridge = bridge

    async def bounded_body(request: Request) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(status_code=413, detail="request body exceeds 64 KiB")
        return bytes(body)

    async def verify_mc_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if config.mc_webhook_require_token and not bearer_matches(authorization, config.mc_webhook_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid Minecraft webhook token")

    @app.get("/healthz", tags=["system"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["system"])
    async def readyz() -> dict[str, Any]:
        return {"status": "ready", "onebot_enabled": config.onebot_enabled}

    @app.post("/api/v1/minecraft/events/{event_type}", status_code=202, tags=["minecraft"],
              openapi_extra={"requestBody": {"required": True, "content": {
                  "application/json": {"schema": {"oneOf": [
                      {**model.model_json_schema(), "title": name}
                      for name, model in EVENT_MODELS.items()
                  ]}}
              }}})
    async def minecraft_event(
        event_type: Literal["chat", "death", "join", "leave", "players", "status"],
        request: Request,
        _: None = Depends(verify_mc_token),
    ) -> dict[str, Any]:
        model = EVENT_MODELS[event_type]
        try:
            payload = model.model_validate_json(await bounded_body(request))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await bridge.handle_mc_event(event_type, payload)
        return {"accepted": True, "event": event_type}

    @app.post("/api/v1/onebot/events", status_code=202, tags=["onebot"])
    async def onebot_event(request: Request) -> dict[str, Any]:
        raw = await bounded_body(request)
        if not config.onebot_webhook_token and not config.onebot_webhook_secret:
            raise HTTPException(status_code=401, detail="OneBot webhook authentication is not configured")
        if config.onebot_webhook_token and not bearer_matches(
            request.headers.get("Authorization"), config.onebot_webhook_token
        ):
            raise HTTPException(status_code=401, detail="invalid OneBot webhook token")
        if config.onebot_webhook_secret:
            supplied = request.headers.get("X-Signature", "")
            expected = "sha1=" + hmac.new(
                config.onebot_webhook_secret.encode(), raw, hashlib.sha1
            ).hexdigest()
            if not re.fullmatch(r"sha1=[0-9a-f]{40}", supplied) or not hmac.compare_digest(
                supplied.encode("ascii"), expected.encode("ascii")
            ):
                raise HTTPException(status_code=401, detail="invalid OneBot webhook signature")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON") from exc
        return await bridge.handle_onebot_event(payload)

    @app.websocket(config.onebot_ws_path)
    async def onebot_reverse_websocket(websocket: WebSocket) -> None:
        if not config.onebot_enabled or not bearer_matches(
            websocket.headers.get("Authorization"), config.onebot_access_token
        ):
            await websocket.close(code=1008)
            return
        if not await websocket_onebot.connect(websocket):
            return
        tasks: set[asyncio.Task] = set()
        def task_done(task: asyncio.Task) -> None:
            tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logging.getLogger(__name__).warning("OneBot event rejected after handler failure")
        async def handle_event(payload: dict) -> None:
            await bridge.handle_onebot_event(payload)
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode()) > 65536:
                    await websocket.close(code=1009)
                    break
                try:
                    payload = json.loads(raw)
                except ValueError:
                    await websocket.close(code=1007)
                    break
                if not isinstance(payload, dict):
                    continue
                if "echo" in payload and "retcode" in payload:
                    websocket_onebot.resolve(payload, websocket)
                elif payload.get("post_type") and len(tasks) < 32:
                    task = asyncio.create_task(handle_event(payload))
                    tasks.add(task)
                    task.add_done_callback(task_done)
        except WebSocketDisconnect:
            pass
        finally:
            await websocket_onebot.disconnect(websocket)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @app.get("/api/v1/state/players", tags=["state"])
    async def state_players() -> dict[str, Any]:
        return bridge.state.players_payload()

    @app.get("/api/v1/state/status", tags=["state"])
    async def state_status() -> dict[str, Any]:
        return bridge.state.status_payload()

    @app.get("/api/v1/state/meta", tags=["state"])
    async def state_meta() -> dict[str, Any]:
        return bridge.state.metadata_payload()

    return app


app = create_app()
