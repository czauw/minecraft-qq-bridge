"""真实本地 TCP 集成测试：3 台模拟 QueQiao WS + 1 台禁用 + 模拟 QQ 网关/HTTP。

这里不 mock 任何业务函数：QueQiao 侧是真实的 WebSocket 服务端，按官方
Protocol V2（``x-self-name`` 握手、``api``/``data``/``echo`` 请求、``code``/
``status`` 响应、事件推送）应答；QQ 侧是真实的 HTTP mock transport 与真实
WebSocket 网关（Hello→Identify→READY→心跳）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from websockets.asyncio.server import serve

from helpers import make_config, server_entry
from mcqq_bridge.manager import ServerManager
from mcqq_bridge.qq_official import QQBotClient
from mcqq_bridge.router import BridgeRouter


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class MockQueQiao:
    """按 QueQiao Protocol V2 应答的最小服务端。"""

    def __init__(
        self,
        name: str,
        players: list[str],
        *,
        rcon_enabled: bool = True,
        ping_online: int | None = None,
        ping_sample: list[dict[str, Any]] | None = None,
    ) -> None:
        self.name = name
        self.players = players
        self.rcon_enabled = rcon_enabled
        self.ping_online = ping_online
        self.ping_sample = ping_sample
        self.connections = 0
        self.headers: list[Any] = []
        self.broadcasts: list[str] = []
        self.apis: list[str] = []
        self.ws: Any = None
        self.port = 0
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket) -> None:
        self.connections += 1
        self.headers.append(websocket.request.headers)
        self.ws = websocket
        try:
            async for raw in websocket:
                message = json.loads(raw)
                self.apis.append(message.get("api"))
                await websocket.send(json.dumps(self._respond(message), ensure_ascii=False))
        except Exception:  # connection closed
            pass

    def _respond(self, message: dict[str, Any]) -> dict[str, Any]:
        api = message.get("api")
        echo = message.get("echo")
        base = {"api": api, "post_type": "response", "echo": echo}
        if api == "send_rcon_command":
            if not self.rcon_enabled:
                return {
                    **base,
                    "code": 400,
                    "status": "FAILED",
                    "message": "Rcon is not enabled",
                    "data": {"error": "rcon disabled"},
                }
            data = (
                f"There are {len(self.players)} of a max of 20 players online: "
                + ", ".join(self.players)
            )
            return {**base, "code": 200, "status": "SUCCESS", "message": "success", "data": data}
        if api == "get_status":
            online = self.ping_online if self.ping_online is not None else len(self.players)
            sample = (
                self.ping_sample
                if self.ping_sample is not None
                else [{"name": player} for player in self.players]
            )
            return {
                **base,
                "code": 200,
                "status": "SUCCESS",
                "message": "success",
                "data": {
                    "timestamp": int(time.time() * 1000),
                    "server_type": "paper",
                    "server_version": "1.20.1",
                    "server_list_ping": {
                        "available": True,
                        "reason": "ok",
                        "host": "127.0.0.1",
                        "port": 25565,
                        "players": {"online": online, "max": 20, "sample": sample},
                    },
                },
            }
        if api == "broadcast":
            self.broadcasts.append(message["data"]["message"]["text"])
            return {**base, "code": 200, "status": "SUCCESS", "message": "success", "data": None}
        return {**base, "code": 404, "status": "FAILED", "message": f"Unknown API {api}", "data": None}

    async def push_event(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def drop(self) -> None:
        await self.ws.close()


class MockQQGateway:
    def __init__(self) -> None:
        self.ws: Any = None
        self.identifies: list[dict[str, Any]] = []
        self.port = 0
        self._server: Any = None

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handler(self, websocket) -> None:
        self.ws = websocket
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        identify = json.loads(await websocket.recv())
        self.identifies.append(identify)
        await websocket.send(
            json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "SESSION-1"}})
        )
        try:
            async for raw in websocket:
                payload = json.loads(raw)
                if payload.get("op") == 1:
                    await websocket.send(json.dumps({"op": 11}))
        except Exception:
            pass

    async def push(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload, ensure_ascii=False))


class QQHttpRecorder:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.token_calls = 0
        self.bot_state_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/getAppAccessToken":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "T1", "expires_in": "7200"})
        if path.endswith("/bot_state"):
            self.bot_state_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allow_proactive_msg": True,
                        "recv_msg_setting": "all",
                        "member_role": "member",
                    },
                },
            )
        body = json.loads(request.content)
        self.sent.append(body.get("content", ""))
        return httpx.Response(200, json={"id": f"OUT-{len(self.sent)}"})


class World:
    """3 台启用 + 1 台禁用的真实本地 TCP 场景。"""

    def __init__(self) -> None:
        self.main = MockQueQiao("主服", ["Alice", "Bob"])
        self.survival = MockQueQiao("生存服", [])
        self.creative = MockQueQiao("创造服", ["Carol"])
        self.resource = MockQueQiao("资源服", ["Dave"])
        self.enabled_servers = (self.main, self.survival, self.creative)
        self.all_servers = (self.main, self.survival, self.creative, self.resource)
        self.gateway = MockQQGateway()
        self.http = QQHttpRecorder()
        self.manager: ServerManager | None = None
        self.qq: QQBotClient | None = None
        self.router: BridgeRouter | None = None
        self.http_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        for server in self.all_servers:
            await server.start()
        await self.gateway.start()
        config = make_config(
            servers=[
                server_entry(
                    "主服", self.main.port, access_token="tok-main", players={"mode": "auto", "timeout_seconds": 2}
                ),
                server_entry("生存服", self.survival.port),
                server_entry("创造服", self.creative.port),
                server_entry("资源服", self.resource.port, enabled=False),
            ],
            bot={
                "gateway_url": f"ws://127.0.0.1:{self.gateway.port}",
                "group_openid": "GROUP_OPENID_1",
            },
            relay={"dedupe_ttl_seconds": 60, "loop_guard_seconds": 2},
        )
        self.config = config
        holder: dict[str, BridgeRouter] = {}
        router_ready = asyncio.Event()

        async def on_mc(event):
            await router_ready.wait()
            await holder["router"].on_mc_event(event)

        async def on_qq(message):
            await router_ready.wait()
            await holder["router"].on_qq_message(message)

        self.manager = ServerManager(config, on_mc)
        self.http_client = httpx.AsyncClient(transport=httpx.MockTransport(self.http))
        self.qq = QQBotClient(config.bot, config.relay, on_qq, http_client=self.http_client)
        self.router = BridgeRouter(config, self.manager, self.qq)
        holder["router"] = self.router
        router_ready.set()
        await self.manager.start()
        await self.qq.start()
        ready = await wait_until(
            lambda: self.qq.connected
            and self.manager.client("主服").connected
            and self.manager.client("生存服").connected
            and self.manager.client("创造服").connected,
            timeout=5.0,
        )
        assert ready, "QQ 或 QueQiao 未在超时内就绪"
        assert await wait_until(
            lambda: self.manager.client("主服").players == ["Alice", "Bob"], timeout=5.0
        ), "连接后未同步玩家名单"

    async def stop(self) -> None:
        if self.qq is not None:
            await self.qq.close()
        if self.manager is not None:
            await self.manager.stop()
        if self.http_client is not None:
            await self.http_client.aclose()
        for server in self.all_servers:
            await server.stop()
        await self.gateway.stop()


@pytest.fixture
async def world():
    instance = World()
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


def group_event(text: str, message_id: str, nickname: str = "小明") -> dict[str, Any]:
    return {
        "op": 0,
        "s": 10,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "author": {"id": "u1", "member_openid": "m1", "username": nickname, "bot": False},
            "content": text,
            "group_openid": "GROUP_OPENID_1",
            "timestamp": "2026-07-21T10:00:00+08:00",
            "message_type": 0,
        },
    }


async def test_three_enabled_servers_connect_independently_plus_one_disabled(world: World):
    # 3 台启用：每台一条独立连接；1 台禁用：零连接
    assert world.main.connections == 1
    assert world.survival.connections == 1
    assert world.creative.connections == 1
    assert world.resource.connections == 0
    assert world.manager.connected_count == 3

    # QueQiao 对 x-self-name 做 URLDecoder(UTF-8)，所以这里按官方行为解码后比较
    assert unquote(world.main.headers[0]["x-self-name"]) == "主服"
    assert world.main.headers[0]["Authorization"] == "Bearer tok-main"
    assert unquote(world.survival.headers[0]["x-self-name"]) == "生存服"
    assert unquote(world.creative.headers[0]["x-self-name"]) == "创造服"
    assert "Authorization" not in world.survival.headers[0]
    assert "Authorization" not in world.creative.headers[0]

    # 启动时真实探测群内状态（bot_state），成功时不得报告 degraded
    assert await wait_until(lambda: world.http.bot_state_calls >= 1)
    assert world.qq._bot_state.allow_proactive_msg is True
    assert world.qq.degraded_reasons() == []

    # 连接后同步已有玩家（真实 send_rcon_command 往返，每台各自一条连接）
    assert "send_rcon_command" in world.main.apis
    assert await wait_until(lambda: world.manager.client("创造服").players == ["Carol"])
    assert world.manager.client("主服").players == ["Alice", "Bob"]
    assert world.manager.client("生存服").players == []


async def test_list_reflects_online_zero_and_disabled(world: World):
    await world.gateway.push(group_event("/list", "LIST-1"))
    assert await wait_until(lambda: len(world.http.sent) == 1)
    assert world.http.sent[0] == (
        "主服 2人在线：\nAlice\nBob\n\n生存服 0人在线\n\n创造服 1人在线：\nCarol"
    )
    # 禁用的「资源服」既不查询也不出现在结果里
    assert "资源服" not in world.http.sent[0]
    assert world.resource.connections == 0
    # 命令不能被转发到任何 MC 服务器
    assert world.main.broadcasts == []
    assert world.survival.broadcasts == []
    assert world.creative.broadcasts == []


async def test_qq_message_broadcast_to_all_online_servers(world: World):
    await world.gateway.push(group_event("你好", "MSG-1"))
    assert await wait_until(
        lambda: world.main.broadcasts == ["小明：你好"]
        and world.survival.broadcasts == ["小明：你好"]
        and world.creative.broadcasts == ["小明：你好"]
    )
    assert world.resource.broadcasts == []


async def test_mc_events_reach_qq_with_unified_format(world: World):
    await world.main.push_event(
        {
            "timestamp": 1700000000,
            "post_type": "message",
            "event_name": "PlayerChatEvent",
            "server_name": "伪造的名字",
            "sub_type": "player_chat",
            "message_id": "CHAT-1",
            "player": {"nickname": "Alice", "uuid": "uuid-a"},
            "message": "大家好",
        }
    )
    await world.survival.push_event(
        {
            "timestamp": 1700000001,
            "post_type": "notice",
            "event_name": "PlayerJoinEvent",
            "sub_type": "player_join",
            "player": {"nickname": "Bob", "uuid": "uuid-b"},
        }
    )
    assert await wait_until(lambda: len(world.http.sent) == 2)
    assert world.http.sent[0] == "[主服] [Alice] ： 大家好"
    assert world.http.sent[1] == "[生存服] [Bob] ： 进入了服务器"


async def test_disconnect_marks_only_that_server_offline(world: World):
    await world.survival.drop()
    assert await wait_until(lambda: not world.manager.client("生存服").connected)
    assert world.manager.client("生存服").players == []  # 断线立即失效
    assert world.manager.client("主服").connected

    await world.gateway.push(group_event("/list", "LIST-2"))
    assert await wait_until(lambda: len(world.http.sent) >= 1)
    text = world.http.sent[-1]
    assert "主服 2人在线：\nAlice\nBob" in text
    assert "生存服 离线" in text
    assert "创造服 1人在线：\nCarol" in text
    assert "资源服" not in text

    # 其它服务器仍可正常广播
    await world.gateway.push(group_event("还在吗", "MSG-2"))
    assert await wait_until(lambda: world.main.broadcasts == ["小明：还在吗"])


async def test_auto_reconnect_and_resync_players():
    """单服断开后按退避重连，并在重连后重新同步玩家名单。"""

    async def noop(event) -> None:
        return None

    mock = MockQueQiao("主服", ["Alice"])
    await mock.start()
    port = mock.port
    config = make_config(
        servers=[
            server_entry(
                "主服",
                port,
                players={"mode": "rcon", "timeout_seconds": 1},
                reconnect_initial_seconds=0.05,
                reconnect_max_seconds=1,
            )
        ],
        bot={"gateway_url": "ws://127.0.0.1:1/unused"},
    )
    manager = ServerManager(config, noop)
    await manager.start()
    try:
        client = manager.client("主服")
        assert await wait_until(lambda: client.connected and client.players == ["Alice"])
        await mock.stop()
        assert await wait_until(lambda: not client.connected, timeout=3.0)

        # 同一端口重新上线，且玩家名单发生变化
        replacement = MockQueQiao("主服", ["Alice", "Bob"])
        replacement._server = await serve(replacement._handler, "127.0.0.1", port)
        replacement.port = port
        try:
            assert await wait_until(lambda: client.connected, timeout=5.0)
            assert await wait_until(lambda: client.players == ["Alice", "Bob"], timeout=5.0)
            assert replacement.connections == 1
            assert client.reconnect_count >= 1
        finally:
            await replacement.stop()
    finally:
        await manager.stop()
