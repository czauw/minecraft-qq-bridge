"""QQ 官方 adapter：token、网关握手/心跳、事件解析、群消息发送与限流。"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from websockets.asyncio.server import serve

from helpers import bot_entry, make_config
from mcqq_bridge.config import RelayConfig
from mcqq_bridge.events import QQMessage
from mcqq_bridge.qq_official import (
    QQApiError,
    QQBotClient,
    SendResult,
    TokenManager,
    classify_close_code,
    parse_group_message,
)

RELAY = RelayConfig()


def http_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def token_handler(calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"access_token": "T1", "expires_in": "7200"})

    return handler


# --- 消息解析 ----------------------------------------------------------------


def group_payload(**overrides):
    payload = {
        "id": "MSG-1",
        "author": {"id": "u1", "member_openid": "m1", "username": "小明", "bot": False},
        "content": " 你好 ",
        "group_openid": "GROUP_OPENID_1",
        "timestamp": "2026-07-21T10:00:00+08:00",
        "message_type": 0,
    }
    payload.update(overrides)
    return payload


def test_parse_plain_text_message():
    message = parse_group_message(group_payload(), RELAY)
    assert message is not None
    assert message.text == "你好"
    assert message.nickname == "小明"
    assert message.message_id == "MSG-1"
    assert message.image_count == 0


def test_parse_msg_elements_keeps_order_and_images():
    payload = group_payload(
        content=" ",
        msg_elements=[
            {"content": "看这个"},
            {
                "content": "",
                "attachments": [
                    {"content_type": "image/jpeg", "url": "https://x/1.jpg"},
                    {"content_type": "image/png", "url": "https://x/2.png"},
                ],
            },
            {"content": "好看吗"},
        ],
    )
    message = parse_group_message(payload, RELAY)
    assert message is not None
    assert message.text == "看这个 [图片] [图片] 好看吗"
    assert message.image_count == 2


def test_parse_attachments_appended_after_content():
    payload = group_payload(
        content="照片",
        attachments=[
            {"content_type": "image/jpeg", "url": "https://x/1.jpg"},
            {"content_type": "file", "url": "https://x/a.zip"},
        ],
    )
    message = parse_group_message(payload, RELAY)
    assert message is not None
    assert message.text == "照片 [图片] [附件]"
    assert message.image_count == 1
    assert message.attachment_count == 1


def test_parse_bot_author_rejected():
    payload = group_payload(author={"id": "b", "username": "bot", "bot": True})
    assert parse_group_message(payload, RELAY) is None


def test_parse_missing_id_or_group_rejected():
    assert parse_group_message(group_payload(id=""), RELAY) is None
    assert parse_group_message(group_payload(group_openid=""), RELAY) is None
    assert parse_group_message(group_payload(content="  "), RELAY) is None


def test_parse_missing_nickname_uses_configured_fallback():
    message = parse_group_message(group_payload(author={"id": "u1", "member_openid": "m1"}), RELAY)
    assert message is not None
    assert message.nickname == RELAY.nickname_fallback


def test_parse_truncates_long_text():
    message = parse_group_message(group_payload(content="x" * 5000), RELAY, max_chars=100)
    assert message is not None
    assert len(message.text) == 100
    assert message.truncated is True


# --- token -------------------------------------------------------------------


async def test_token_is_cached_and_locked():
    calls: list[httpx.Request] = []
    async with http_client(token_handler(calls)) as client:
        manager = TokenManager(make_config().bot, client)
        results = await asyncio.gather(*(manager.get() for _ in range(5)))
    assert results == ["T1"] * 5
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body["appId"] == "102000000"
    assert calls[0].url.path == "/app/getAppAccessToken"


async def test_token_business_error_raises_without_leaking_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100016, "message": "invalid appid or secret"})

    async with http_client(handler) as client:
        manager = TokenManager(make_config().bot, client)
        with pytest.raises(QQApiError) as excinfo:
            await manager.get()
    assert "100016" in str(excinfo.value)
    assert "test-secret" not in str(excinfo.value)


# --- 发送 --------------------------------------------------------------------


class SendRecorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bot_state_response = {
            "code": 0,
            "data": {
                "allow_proactive_msg": True,
                "recv_msg_setting": "all",
                "member_role": "member",
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "T1", "expires_in": "7200"})
        if path.endswith("/bot_state"):
            return httpx.Response(200, json=self.bot_state_response)
        return self.responses.pop(0)


def make_bot_client(recorder: SendRecorder, events=None, **bot_overrides):
    config = make_config(bot=bot_overrides)
    received: list[QQMessage] = []

    async def on_event(message: QQMessage) -> None:
        received.append(message)

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(recorder))
    return client, received


async def test_send_text_payload_and_headers():
    recorder = SendRecorder([httpx.Response(200, json={"id": "MSG-OUT", "timestamp": 1})])
    client, _ = make_bot_client(recorder)
    result = await client.send_text("你好")
    assert result == SendResult(accepted=True, delivered=True, message_id="MSG-OUT")
    send_request = recorder.requests[-1]
    assert send_request.url.path == "/v2/groups/GROUP_OPENID_1/messages"
    assert send_request.headers["authorization"] == "QQBot T1"
    body = json.loads(send_request.content)
    assert body == {"content": "你好", "msg_type": 0}
    await client.close()


async def test_send_reply_increments_msg_seq():
    recorder = SendRecorder(
        [
            httpx.Response(200, json={"id": "A"}),
            httpx.Response(200, json={"id": "B"}),
        ]
    )
    client, _ = make_bot_client(recorder)
    await client.send_text("一", reply_to="MSG-1")
    await client.send_text("二", reply_to="MSG-1")
    bodies = [json.loads(request.content) for request in recorder.requests[1:]]
    assert bodies[0]["msg_id"] == "MSG-1" and bodies[0]["msg_seq"] == 1
    assert bodies[1]["msg_seq"] == 2
    await client.close()


async def test_send_401_refreshes_token_once():
    recorder = SendRecorder(
        [
            httpx.Response(401, json={"code": 11244, "message": "token invalid"}),
            httpx.Response(200, json={"id": "OK"}),
        ]
    )
    client, _ = make_bot_client(recorder)
    result = await client.send_text("hi")
    assert result.delivered is True
    token_calls = [r for r in recorder.requests if r.url.path == "/app/getAppAccessToken"]
    assert len(token_calls) == 2
    await client.close()


async def test_send_429_retries_once_then_succeeds():
    recorder = SendRecorder(
        [
            httpx.Response(429, json={"code": 22009}, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"id": "OK"}),
        ]
    )
    client, _ = make_bot_client(recorder)
    result = await client.send_text("hi")
    assert result.delivered is True
    assert len([r for r in recorder.requests if r.url.path.endswith("/messages")]) == 2
    await client.close()


async def test_send_business_error_is_not_delivered():
    recorder = SendRecorder([httpx.Response(200, json={"code": 304003, "message": "no permission"})])
    client, _ = make_bot_client(recorder)
    result = await client.send_text("hi")
    assert result.delivered is False
    assert "304003" in result.reason
    await client.close()


async def test_proactive_disabled_blocks_mc_notifications():
    recorder = SendRecorder([])
    client, _ = make_bot_client(recorder, proactive_enabled=False)
    result = await client.send_text("hi", proactive=True)
    assert result == SendResult(accepted=False, delivered=False, reason="proactive_disabled")
    assert recorder.requests == []
    await client.close()


async def test_local_rate_limit():
    recorder = SendRecorder([httpx.Response(200, json={"id": "1"})])
    client, _ = make_bot_client(recorder, send_rate_per_minute=1)
    assert (await client.send_text("a")).delivered is True
    limited = await client.send_text("b")
    assert limited.reason == "rate_limited"
    await client.close()


# --- 网关 --------------------------------------------------------------------


async def test_gateway_handshake_heartbeat_and_dispatch():
    """本地真实 TCP WebSocket 网关模拟：Hello → Identify → READY → 心跳 → 群事件。"""
    identify_seen: list[dict] = []
    heartbeats: list[dict] = []

    async def handler(websocket) -> None:
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 40}}))
        identify = json.loads(await websocket.recv())
        identify_seen.append(identify)
        await websocket.send(
            json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1", "user": {"id": "bot"}}})
        )
        for _ in range(2):
            try:
                frame = json.loads(await websocket.recv())
            except Exception:
                return
            heartbeats.append(frame)
            await websocket.send(json.dumps({"op": 11}))
        await websocket.send(
            json.dumps(
                {
                    "op": 0,
                    "s": 2,
                    "t": "GROUP_MESSAGE_CREATE",
                    "d": group_payload(),
                }
            )
        )
        await asyncio.sleep(0.3)

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    recorder = SendRecorder([])
    config = make_config(bot={"gateway_url": f"ws://127.0.0.1:{port}"})
    received: list[QQMessage] = []

    async def on_event(message: QQMessage) -> None:
        received.append(message)

    client = QQBotClient(
        config.bot, config.relay, on_event, http_client=http_client(recorder)
    )
    await client.start()
    for _ in range(50):
        if received and heartbeats:
            break
        await asyncio.sleep(0.05)
    await client.close()
    server.close()
    await server.wait_closed()

    assert identify_seen and identify_seen[0]["op"] == 2
    body = identify_seen[0]["d"]
    assert body["token"] == "QQBot T1"
    assert body["intents"] == 1 << 25
    assert body["shard"] == [0, 1]
    assert heartbeats and heartbeats[0]["op"] == 1
    assert received and received[0].text == "你好"
    assert received[0].message_id == "MSG-1"


async def test_resume_identify_uses_resume_watermark():
    client, _ = make_bot_client(SendRecorder([]))
    client._session_id = "S1"
    client._resume_seq = 42

    class CaptureWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    ws = CaptureWS()
    await client._send_identify(ws)
    payload = json.loads(ws.sent[0])
    assert payload["op"] == 6
    assert payload["d"]["session_id"] == "S1"
    assert payload["d"]["seq"] == 42 # Resume 必须用「已处理完成的水位」，不是最新收到的 s
    await client.close()


async def test_identify_without_session_sends_op2():
    client, _ = make_bot_client(SendRecorder([]))

    class CaptureWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, data: str) -> None:
            self.sent.append(data)

    ws = CaptureWS()
    await client._send_identify(ws)
    payload = json.loads(ws.sent[0])
    assert payload["op"] == 2
    assert payload["d"]["intents"] == 1 << 25
    await client.close()



# --- M1：官方关闭码 → Resume / 重新 Identify / 终止 ------------------------------


@pytest.mark.parametrize(
    "code,action",
    [
        (4001, "terminate"),
        (4002, "terminate"),
        (4006, "identify"),
        (4007, "identify"),
        (4008, "resume"),
        (4009, "resume"),
        (4010, "terminate"),
        (4011, "terminate"),
        (4012, "terminate"),
        (4013, "terminate"),
        (4014, "terminate"),
        (4900, "identify"),
        (4913, "identify"),
        (4914, "terminate"),
        (4915, "terminate"),
        (None, "resume"),
        (1006, "resume"),
        (4321, "identify"),
    ],
)
def test_close_code_classification_follows_official_table(code, action):
    assert classify_close_code(code) == action


def _gateway_config(port: int, **bot_overrides):
    return make_config(
        bot={
            "gateway_url": f"ws://127.0.0.1:{port}",
            "reconnect_initial_seconds": 0.02,
            "reconnect_max_seconds": 1,
            **bot_overrides,
        }
    )


async def test_close_code_4006_reidentifies_instead_of_resuming():
    """真实本地 TCP WS：READY 后 close(4006) 必须重新 Identify（[2,2]），不能 Resume。"""
    identify_ops: list[int] = []
    connections = 0
    keep_open = asyncio.Event()

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        identify_ops.append(json.loads(await websocket.recv())["op"])
        if connections == 1:
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}})
            )
            await asyncio.sleep(0.05)
            await websocket.close(code=4006, reason="invalid session")
            return
        await keep_open.wait()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port)
    received: list[QQMessage] = []

    async def on_event(message: QQMessage) -> None:
        received.append(message)

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(100):
            if len(identify_ops) >= 2:
                break
            await asyncio.sleep(0.02)
        assert identify_ops == [2, 2]
    finally:
        keep_open.set()
        await client.close()
        server.close()
        await server.wait_closed()


async def test_close_code_4009_resumes_with_processed_watermark():
    """close(4009) 允许 Resume：第二轮必须是 op=6 且 seq=已处理完成的水位。"""
    frames: list[dict] = []
    connections = 0
    keep_open = asyncio.Event()
    event_seen = asyncio.Event()

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        frames.append(json.loads(await websocket.recv()))
        if connections == 1:
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}})
            )
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "GROUP_MESSAGE_CREATE", "d": group_payload()})
            )
            await asyncio.wait_for(event_seen.wait(), timeout=2)
            await asyncio.sleep(0.05)
            await websocket.close(code=4009, reason="session expired")
            return
        await keep_open.wait()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port)

    async def on_event(message: QQMessage) -> None:
        event_seen.set()

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(100):
            if len(frames) >= 2:
                break
            await asyncio.sleep(0.02)
        assert frames[0]["op"] == 2
        assert frames[1]["op"] == 6
        assert frames[1]["d"]["session_id"] == "S1"
        assert frames[1]["d"]["seq"] == 1
    finally:
        keep_open.set()
        await client.close()
        server.close()
        await server.wait_closed()


async def test_fatal_close_code_4915_stops_reconnecting():
    """4915（机器人封禁）不可重试：不得无休止重连。"""
    connections = 0

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        await websocket.recv()
        await websocket.send(json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}}))
        await asyncio.sleep(0.02)
        await websocket.close(code=4915, reason="bot banned")

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port)
    client = QQBotClient(config.bot, config.relay, lambda _m: asyncio.sleep(0), http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(100):
            if client.fatal:
                break
            await asyncio.sleep(0.02)
        assert client.fatal is True
        assert connections == 1
        assert client.last_error == "gateway_close_4915"
        await asyncio.sleep(0.15)
        assert connections == 1
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def test_gateway_401_refreshes_token_once():
    calls = {"token": 0, "gateway": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"T{calls['token']}", "expires_in": "7200"})
        if request.url.path == "/gateway":
            calls["gateway"] += 1
            if calls["gateway"] == 1:
                return httpx.Response(401, json={"code": 11244})
            return httpx.Response(200, json={"url": "ws://127.0.0.1:9/none"})
        return httpx.Response(404, json={"code": 404})

    config = make_config(bot={"gateway_url": ""})
    client = QQBotClient(config.bot, config.relay, lambda _m: asyncio.sleep(0), http_client=http_client(handler))
    try:
        url = await client._resolve_gateway()
    finally:
        await client.close()
    assert url == "ws://127.0.0.1:9/none"
    assert calls == {"token": 2, "gateway": 2}


# --- M5：receive_mode 过滤 ----------------------------------------------------


async def test_mention_mode_only_consumes_at_events():
    client, received = make_bot_client(SendRecorder([]), receive_mode="mention")
    try:
        await client._dispatch_dispatch(
            {"op": 0, "s": 1, "t": "GROUP_MESSAGE_CREATE", "d": group_payload()}
        )
        await client._dispatch_dispatch(
            {"op": 0, "s": 2, "t": "GROUP_AT_MESSAGE_CREATE", "d": group_payload(id="MSG-AT")}
        )
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.02)
    finally:
        await client.close()
    assert [message.message_id for message in received] == ["MSG-AT"]
    assert received[0].is_at_bot is True


async def test_all_mode_consumes_both_and_dedupes_by_message_id():
    client, received = make_bot_client(SendRecorder([]), receive_mode="all")
    payload = group_payload()
    try:
        await client._dispatch_dispatch({"op": 0, "s": 1, "t": "GROUP_MESSAGE_CREATE", "d": payload})
        await client._dispatch_dispatch({"op": 0, "s": 2, "t": "GROUP_AT_MESSAGE_CREATE", "d": payload})
        await client._dispatch_dispatch({"op": 0, "s": 3, "t": "GROUP_MESSAGE_CREATE", "d": group_payload(id="MSG-2")})
        for _ in range(50):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        await client.close()
    assert [message.message_id for message in received] == ["MSG-1", "MSG-2"]


# --- M6：bot_state 探测与降级 --------------------------------------------------


async def test_bot_state_false_blocks_proactive_but_not_replies():
    recorder = SendRecorder([httpx.Response(200, json={"id": "OUT"})])
    client, _ = make_bot_client(recorder)
    recorder.bot_state_response = {
        "code": 0,
        "data": {"allow_proactive_msg": False, "recv_msg_setting": "only_mention", "member_role": "member"},
    }
    try:
        state = await client.refresh_bot_state()
        assert state.allow_proactive_msg is False
        assert "proactive_not_allowed" in client.degraded_reasons()
        assert "recv_msg_setting:only_mention" in client.degraded_reasons()

        blocked = await client.send_text("mc 事件", proactive=True)
        assert blocked.delivered is False and blocked.reason == "proactive_not_allowed"
        assert not [r for r in recorder.requests if r.url.path.endswith("/messages")]

        reply = await client.send_text("回复", reply_to="MSG-1")
        assert reply.delivered is True
    finally:
        await client.close()


async def test_bot_state_11253_is_unknown_but_not_fatal():
    recorder = SendRecorder([httpx.Response(200, json={"id": "OUT"})])
    client, _ = make_bot_client(recorder)
    recorder.bot_state_response = {"code": 11253, "message": "no access"}
    try:
        state = await client.refresh_bot_state()
        assert state.checked is False
        assert state.reason == "whitelist_only"
        assert "proactive_state_unknown" in client.degraded_reasons()
        result = await client.send_text("mc 事件", proactive=True)
        assert result.delivered is True  # unknown 时仍尝试，但在 status/readyz 暴露 degraded
    finally:
        await client.close()


async def test_bot_state_true_clears_cooldown():
    recorder = SendRecorder([])
    client, _ = make_bot_client(recorder)
    client._cooldown_until = time.monotonic() + 100
    recorder.bot_state_response = {
        "code": 0,
        "data": {"allow_proactive_msg": True, "recv_msg_setting": "all", "member_role": "member"},
    }
    try:
        await client.refresh_bot_state()
        assert client._cooldown_until == 0.0
        assert "proactive_cooldown" not in client.degraded_reasons()
    finally:
        await client.close()


async def test_permission_error_sets_bounded_cooldown_then_recovers():
    recorder = SendRecorder(
        [
            httpx.Response(200, json={"code": 40034105, "message": "no permission"}),
            httpx.Response(200, json={"id": "OK"}),
        ]
    )
    client, _ = make_bot_client(
        recorder, proactive_cooldown_initial_seconds=0.2, proactive_cooldown_max_seconds=0.5
    )
    try:
        first = await client.send_text("a", proactive=True)
        assert first.delivered is False
        assert "40034105" in first.reason

        before = len(recorder.requests)
        for _ in range(3):
            blocked = await client.send_text("b", proactive=True)
            assert blocked.reason == "proactive_cooldown"
        assert len(recorder.requests) == before  # 冷却期内没有再打被拒绝的接口

        await asyncio.sleep(0.25)
        recovered = await client.send_text("c", proactive=True)
        assert recovered.delivered is True
        assert client._cooldown_until == 0.0
    finally:
        await client.close()


async def test_429_sets_cooldown_after_bounded_retry():
    recorder = SendRecorder(
        [
            httpx.Response(429, json={"code": 22009}, headers={"Retry-After": "0"}),
            httpx.Response(429, json={"code": 22009}, headers={"Retry-After": "0"}),
        ]
    )
    client, _ = make_bot_client(
        recorder, proactive_cooldown_initial_seconds=0.5, proactive_cooldown_max_seconds=1
    )
    try:
        result = await client.send_text("a", proactive=True)
        assert result.delivered is False
        assert result.reason == "http_429_code_22009"
        assert client._cooldown_until > time.monotonic()
        blocked = await client.send_text("b", proactive=True)
        assert blocked.reason == "proactive_cooldown"
    finally:
        await client.close()


# --- M7：非法成功响应不得算投递 -------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, content=b""),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"code": 0, "data": {}}),
        httpx.Response(200, json={"id": ""}),
    ],
)
async def test_invalid_success_response_is_not_delivered(response):
    recorder = SendRecorder([response])
    client, _ = make_bot_client(recorder)
    try:
        result = await client.send_text("hi", reply_to="MSG-1")
    finally:
        await client.close()
    assert result.accepted is False
    assert result.delivered is False
    assert result.reason == "invalid_response"
    assert result.message_id is None


async def test_valid_nested_success_response_is_delivered():
    recorder = SendRecorder([httpx.Response(200, json={"code": 0, "data": {"id": "OUT-9"}})])
    client, _ = make_bot_client(recorder)
    try:
        result = await client.send_text("hi")
    finally:
        await client.close()
    assert result == SendResult(accepted=True, delivered=True, message_id="OUT-9")


# --- M8：日志/状态不含上游原文或凭据 -------------------------------------------


async def test_upstream_message_never_reaches_logs_or_status(caplog):
    canary = "CANARY_SECRET_ONLY_TEST"
    recorder = SendRecorder([httpx.Response(200, json={"code": 304003, "message": canary})])
    client, _ = make_bot_client(recorder)
    recorder.bot_state_response = {"code": 11253, "message": canary}
    try:
        with caplog.at_level(logging.DEBUG, logger="mcqq_bridge.qq"):
            result = await client.send_text("hi")
            await client.refresh_bot_state()
            snapshot = json.dumps(client.status_payload(), ensure_ascii=False)
    finally:
        await client.close()
    assert result.delivered is False
    assert canary not in caplog.text
    assert canary not in snapshot
    assert canary not in json.dumps(client.status_payload(), ensure_ascii=False)


async def test_network_error_text_with_credentials_is_scrubbed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            raise httpx.ConnectError(
                "boom https://user:pw@api.test/x?access_token=CANARY_TOKEN", request=request
            )
        return httpx.Response(500)

    client, _ = make_bot_client(SendRecorder([]))
    await client._http.aclose()
    client._http = http_client(handler)
    client.tokens = TokenManager(client.bot, client._http)
    try:
        result = await client.send_text("hi")
        status = json.dumps(client.status_payload(), ensure_ascii=False)
    finally:
        await client._http.aclose()
    assert result.delivered is False
    assert "CANARY_TOKEN" not in result.reason
    assert "CANARY_TOKEN" not in status
    assert "user:pw" not in result.reason


# --- M2：有界队列与恢复水位 ----------------------------------------------------


def _dispatch_frame(seq: int, text: str, message_id: str) -> dict:
    return {
        "op": 0,
        "s": seq,
        "t": "GROUP_MESSAGE_CREATE",
        "d": group_payload(id=message_id, content=text),
    }


async def test_watermark_advances_only_after_contiguous_completion():
    """乱序完成：后到的事件先完成也不能推进水位。"""
    gate = asyncio.Event()
    client, received = make_bot_client(SendRecorder([]), event_workers=4)
    order: list[str] = []

    async def on_event(message: QQMessage) -> None:
        if message.text == "slow":
            await gate.wait()
        order.append(message.text)

    client._on_event = on_event
    try:
        await client._dispatch_dispatch(_dispatch_frame(1, "slow", "M1"))
        await client._dispatch_dispatch(_dispatch_frame(2, "fast", "M2"))
        for _ in range(100):
            if "fast" in order:
                break
            await asyncio.sleep(0.01)
        assert "fast" in order
        assert client._resume_seq is None  # s=1 未完成 → 水位不能跳到 2
        assert client._seq_received == 2  # 心跳用的是「最新收到」

        gate.set()
        for _ in range(100):
            if client._resume_seq == 2:
                break
            await asyncio.sleep(0.01)
        assert client._resume_seq == 2
    finally:
        gate.set()
        await client.close()


async def test_queue_overflow_does_not_advance_watermark_and_triggers_resume():
    """队列满：不把未处理事件标成完成，主动 Resume 且 seq 不越过缺口。"""
    frames: list[dict] = []
    connections = 0
    keep_open = asyncio.Event()
    block = asyncio.Event()

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        frames.append(json.loads(await websocket.recv()))
        if connections == 1:
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S-Q"}})
            )
            for seq in (2, 3, 4):
                await websocket.send(json.dumps(_dispatch_frame(seq, "BLOCK", f"M{seq}")))
                await asyncio.sleep(0.06)
            await asyncio.sleep(0.3)
            return
        await keep_open.wait()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port, event_queue_size=1, event_workers=1)

    async def on_event(message: QQMessage) -> None:
        await block.wait()

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(200):
            if len(frames) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(frames) >= 2
        assert frames[1]["op"] == 6
        # s=2 的处理被阻塞、s=3 还在队列里、s=4 因队列满被拒：
        # 水位必须停在「没有任何连续完成」的状态，Resume 不能声称已处理到 4
        assert frames[1]["d"]["seq"] == 0
        assert client.overflow_events >= 1
        assert client._resume_seq is None
    finally:
        block.set()
        await client.close()
        keep_open.set()
        server.close()
        await server.wait_closed()


async def test_queue_overflow_keeps_id_out_of_dedupe_table():
    """溢出事件不得进入去重表：只有成功入队的事件才算「已见」。"""
    gate = asyncio.Event()
    client, received = make_bot_client(SendRecorder([]), event_queue_size=1, event_workers=1)

    async def on_event(message: QQMessage) -> None:
        await gate.wait()

    client._on_event = on_event
    try:
        await client._dispatch_dispatch(_dispatch_frame(2, "a", "M-A"))
        await asyncio.sleep(0.02)  # 让 worker 取走 s=2 并阻塞，队列腾空
        await client._dispatch_dispatch(_dispatch_frame(3, "b", "M-B"))
        await client._dispatch_dispatch(_dispatch_frame(4, "c", "M-C"))
        assert client.overflow_events == 1
        assert "GROUP_OPENID_1:M-B" in client._seen_ids  # 已入队 → 已见
        assert "GROUP_OPENID_1:M-C" not in client._seen_ids  # 溢出 → 可重投
        assert 4 in client._inflight
        assert client._resume_seq is None
    finally:
        gate.set()
        await client.close()


async def test_queue_overflow_replay_delivers_event_exactly_once():
    """真实 WS：队列满 → 主动 Resume → 网关重投同一 message_id → 回调恰好一次且水位推进。"""
    frames: list[dict] = []
    connections = 0
    keep_open = asyncio.Event()
    block = asyncio.Event()
    received: list[QQMessage] = []
    holder: dict[str, QQBotClient] = {}

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        frames.append(json.loads(await websocket.recv()))
        if connections == 1:
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S-R"}})
            )
            for seq in (2, 3, 4):
                await websocket.send(json.dumps(_dispatch_frame(seq, "BUSY", f"M{seq}")))
                await asyncio.sleep(0.06)
            await keep_open.wait()
            return
        await websocket.send(json.dumps({"op": 0, "s": 1, "t": "RESUMED", "d": {}}))
        # 第 1 轮被阻塞的处理放行；等积压事件处理完，再按网关语义重投未确认事件
        block.set()
        client = holder["client"]
        for _ in range(300):
            if client._event_queue.qsize() == 0 and not client._queued_seqs:
                break
            await asyncio.sleep(0.01)
        for seq in (2, 3, 4):
            await websocket.send(json.dumps(_dispatch_frame(seq, "REPLAY", f"M{seq}")))
        await keep_open.wait()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port, event_queue_size=1, event_workers=1)

    async def on_event(message: QQMessage) -> None:
        await block.wait()
        received.append(message)

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    holder["client"] = client
    await client.start()
    try:
        for _ in range(300):
            if len(frames) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(frames) >= 2
        assert frames[1]["op"] == 6
        # 溢出时没有任何连续完成，Resume 不能声称已确认任何事件
        assert frames[1]["d"]["seq"] == 0
        assert client.overflow_events >= 1

        for _ in range(300):
            if len(received) >= 3:
                break
            await asyncio.sleep(0.01)
        ids = [message.message_id for message in received]
        assert ids.count("M4") == 1  # 溢出事件经重投后仍被投递，且只投递一次
        assert sorted(ids) == ["M2", "M3", "M4"]
        assert client._resume_seq == 4
    finally:
        block.set()
        keep_open.set()
        await client.close()
        server.close()
        await server.wait_closed()
    assert sorted(message.message_id for message in received) == ["M2", "M3", "M4"]


async def test_resume_after_completed_event_does_not_redeliver():
    """已处理完的事件被网关 Resume 重投：不得再次回调，但水位继续推进。"""
    frames: list[dict] = []
    connections = 0
    keep_open = asyncio.Event()
    delivered: list[str] = []

    def wait_for(item_count: int):
        async def waiter() -> None:
            for _ in range(300):
                if len(delivered) >= item_count:
                    return
                await asyncio.sleep(0.01)

        return waiter()

    async def handler(websocket) -> None:
        nonlocal connections
        connections += 1
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        frames.append(json.loads(await websocket.recv()))
        if connections == 1:
            await websocket.send(
                json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S-C"}})
            )
            await websocket.send(json.dumps(_dispatch_frame(2, "hello", "M-1")))
            await wait_for(1)
            await asyncio.sleep(0.05)
            await websocket.close(code=4009, reason="session expired")
            return
        await websocket.send(json.dumps({"op": 0, "s": 2, "t": "RESUMED", "d": {}}))
        # 网关重投已确认的 s=2，再补一条新事件 s=3
        await websocket.send(json.dumps(_dispatch_frame(2, "hello", "M-1")))
        await websocket.send(json.dumps(_dispatch_frame(3, "world", "M-2")))
        await keep_open.wait()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port)

    async def on_event(message: QQMessage) -> None:
        delivered.append(message.message_id)

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(300):
            if len(frames) >= 2:
                break
            await asyncio.sleep(0.01)
        assert frames[0]["op"] == 2
        assert frames[1]["op"] == 6
        assert frames[1]["d"]["seq"] == 2

        for _ in range(300):
            if len(delivered) >= 2:
                break
            await asyncio.sleep(0.01)
        assert delivered == ["M-1", "M-2"]  # 重投的 M-1 没有第二次回调
        assert client._resume_seq == 3
    finally:
        keep_open.set()
        await client.close()
        server.close()
        await server.wait_closed()


async def test_all_mode_duplicate_dispatch_dedupes_while_copy_still_pending():
    """all 模式双投：副本仍在队列中时，@ 事件被去重且不提前推进水位。"""
    gate = asyncio.Event()
    seen: list[str] = []
    client, _ = make_bot_client(
        SendRecorder([]), receive_mode="all", event_queue_size=1, event_workers=1
    )

    async def on_event(message: QQMessage) -> None:
        await gate.wait()
        seen.append(message.message_id)

    client._on_event = on_event
    try:
        await client._dispatch_dispatch(
            {"op": 0, "s": 2, "t": "GROUP_MESSAGE_CREATE", "d": group_payload()}
        )
        await asyncio.sleep(0.02)  # worker 取走 s=2 并阻塞
        await client._dispatch_dispatch(
            {"op": 0, "s": 3, "t": "GROUP_AT_MESSAGE_CREATE", "d": group_payload()}
        )
        assert 3 not in client._inflight
        assert client._resume_seq is None  # s=2 未完成 → 水位不能推进
        await client._dispatch_dispatch(
            {"op": 0, "s": 4, "t": "GROUP_MESSAGE_CREATE", "d": group_payload(id="MSG-2")}
        )
        gate.set()
        for _ in range(300):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.01)
        assert seen == ["MSG-1", "MSG-2"]
        assert client._resume_seq == 4
    finally:
        gate.set()
        await client.close()


async def test_heartbeat_keeps_running_while_event_handler_blocked():
    """事件处理被阻塞时心跳必须继续（心跳与事件在同一事件循环里但互不阻塞）。"""
    heartbeats = 0
    block = asyncio.Event()

    async def handler(websocket) -> None:
        nonlocal heartbeats
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30}}))
        await websocket.recv()
        await websocket.send(json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S"}}))
        await websocket.send(json.dumps(_dispatch_frame(2, "BLOCK", "M-BLOCK")))
        try:
            async for raw in websocket:
                if json.loads(raw).get("op") == 1:
                    heartbeats += 1
                    await websocket.send(json.dumps({"op": 11}))
        except Exception:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = _gateway_config(port, receive_mode="all", event_workers=1)

    async def on_event(message: QQMessage) -> None:
        await block.wait()

    client = QQBotClient(config.bot, config.relay, on_event, http_client=http_client(SendRecorder([])))
    await client.start()
    try:
        for _ in range(100):
            if heartbeats >= 2:
                break
            await asyncio.sleep(0.02)
        assert heartbeats >= 2
        assert client.connected is True
    finally:
        block.set()
        await client.close()
        server.close()
        await server.wait_closed()
