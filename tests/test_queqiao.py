"""QueQiao 协议编解码、echo 关联、重连语义与玩家名单解析。"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from helpers import make_config, server_entry
from mcqq_bridge.events import MCEvent
from mcqq_bridge.queqiao import (
    PlayerQueryError,
    QueQiaoClient,
    parse_event,
    parse_player_list_output,
)


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


def make_client(**overrides):
    config = make_config(servers=[server_entry("主服", 18080, **overrides)])
    events: list[MCEvent] = []

    async def on_event(event: MCEvent) -> None:
        events.append(event)

    return QueQiaoClient(config.servers[0], on_event), events


def attach_connected(client: QueQiaoClient) -> FakeWS:
    ws = FakeWS()
    client._ws = ws
    client.connected = True
    client._generation += 1
    return ws


# --- list 输出解析 ------------------------------------------------------------


def test_parse_english_list_with_players():
    snapshot = parse_player_list_output("There are 2 of a max of 20 players online: Alice, Bob")
    assert snapshot.players == ["Alice", "Bob"]
    assert snapshot.max_players == 20
    assert snapshot.authoritative is True
    assert snapshot.source == "rcon"


def test_parse_english_list_zero_players():
    snapshot = parse_player_list_output("There are 0 of a max of 20 players online: ")
    assert snapshot.players == []
    assert snapshot.max_players == 20


def test_parse_chinese_slash_and_count_forms():
    slash = parse_player_list_output("当前有 2/20 名玩家在线：小明，小红")
    assert slash.players == ["小明", "小红"]
    assert slash.max_players == 20

    count = parse_player_list_output("当前有 1 名玩家在线，最多 8 名：Steve")
    assert count.players == ["Steve"]
    assert count.max_players == 8


def test_parse_strips_color_codes():
    snapshot = parse_player_list_output("§aThere are 1 of a max of 5 players online: §bAlex")
    assert snapshot.players == ["Alex"]


def test_parse_inconsistent_count_is_error():
    with pytest.raises(PlayerQueryError):
        parse_player_list_output("There are 5 of a max of 20 players online: Alice")


def test_parse_garbage_and_empty_are_errors():
    with pytest.raises(PlayerQueryError):
        parse_player_list_output("Rcon is not enabled")
    with pytest.raises(PlayerQueryError):
        parse_player_list_output("")


# --- 事件解析 -----------------------------------------------------------------


def chat_payload(**overrides):
    payload = {
        "timestamp": 1700000000,
        "post_type": "message",
        "event_name": "PlayerChatEvent",
        "server_name": "负载里的假名字",
        "sub_type": "player_chat",
        "message_id": "m-1",
        "player": {"nickname": "Alice", "uuid": "uuid-a"},
        "message": "你好",
    }
    payload.update(overrides)
    return payload


def test_parse_chat_event_uses_connection_name():
    event = parse_event(chat_payload(), "主服")
    assert event is not None
    assert event.type == "chat"
    assert event.player == "Alice"
    assert event.text == "你好"
    assert event.server == "主服"  # 不能采用负载里的 server_name
    assert event.event_id == "m-1"


def test_parse_join_quit_events():
    join = parse_event(chat_payload(post_type="notice", sub_type="player_join", message_id=None), "主服")
    assert join is not None and join.type == "join" and join.text == ""
    quit_event = parse_event(chat_payload(post_type="notice", sub_type="player_quit", message_id=None), "主服")
    assert quit_event is not None and quit_event.type == "leave"


def test_parse_death_event_keeps_original_reason():
    payload = chat_payload(
        post_type="notice",
        sub_type="player_death",
        death={"key": "death.attack.mob", "args": ["Zombie"], "text": "Alice was slain by Zombie"},
    )
    event = parse_event(payload, "主服")
    assert event is not None and event.type == "death"
    assert event.text == "Alice was slain by Zombie"

    key_only = parse_event(
        chat_payload(post_type="notice", sub_type="player_death", death={"key": "death.fell.accident"}),
        "主服",
    )
    assert key_only is not None
    assert "death.fell.accident" in key_only.text


def test_unknown_and_incomplete_events_ignored():
    assert parse_event(chat_payload(sub_type="player_command"), "主服") is None
    assert parse_event({"post_type": "notice", "sub_type": "player_join", "player": {}}, "主服") is None
    assert parse_event({"post_type": "meta", "sub_type": "x"}, "主服") is None
    assert parse_event(chat_payload(message="   "), "主服") is None


# --- 请求/响应 ----------------------------------------------------------------


async def test_broadcast_uses_protocol_v2_shape():
    client, _ = make_client()
    ws = attach_connected(client)
    task = asyncio.create_task(client.broadcast("小明：你好"))
    await asyncio.sleep(0)
    frame = json.loads(ws.sent[0])
    assert frame["api"] == "broadcast"
    assert frame["data"] == {"message": {"text": "小明：你好"}}
    assert frame["echo"]
    await client._handle_frame(
        json.dumps(
            {
                "code": 200,
                "api": "broadcast",
                "post_type": "response",
                "status": "SUCCESS",
                "message": "success",
                "echo": frame["echo"],
            }
        )
    )
    assert await task is True


async def test_broadcast_failure_returns_false():
    client, _ = make_client()
    ws = attach_connected(client)
    task = asyncio.create_task(client.broadcast("x"))
    await asyncio.sleep(0)
    echo = json.loads(ws.sent[0])["echo"]
    await client._handle_frame(
        json.dumps({"code": 500, "status": "FAILED", "message": "rcon error", "echo": echo})
    )
    assert await task is False


async def test_request_timeout_raises():
    client, _ = make_client()
    attach_connected(client)
    with pytest.raises(PlayerQueryError):
        await client._request("get_status", {}, 0.05)


async def test_request_without_connection_raises():
    client, _ = make_client()
    with pytest.raises(PlayerQueryError):
        await client._request("broadcast", {}, 0.1)


async def test_ignores_bad_json_and_unknown_response():
    client, events = make_client()
    attach_connected(client)
    await client._handle_frame("{not json")
    await client._handle_frame(json.dumps({"post_type": "response", "echo": "nobody", "code": 200}))
    assert events == []


# --- 玩家查询 ----------------------------------------------------------------


def stub_request(client: QueQiaoClient, responses: dict[str, dict]):
    calls: list[str] = []

    async def fake_request(api: str, data: dict, timeout: float) -> dict:
        calls.append(api)
        if api not in responses:
            raise PlayerQueryError(f"{api} 不被该 QueQiao 版本支持（404）")
        return responses[api]

    client._request = fake_request  # type: ignore[method-assign]
    return calls


async def test_query_players_via_rcon():
    client, _ = make_client()
    calls = stub_request(
        client,
        {
            "send_rcon_command": {
                "code": 200,
                "status": "SUCCESS",
                "data": "There are 2 of a max of 20 players online: A, B",
            }
        },
    )
    snapshot = await client.query_players()
    assert snapshot.players == ["A", "B"]
    assert snapshot.source == "rcon"
    assert calls == ["send_rcon_command"]
    assert client.players == ["A", "B"]


async def test_query_auto_falls_back_to_status_when_rcon_missing():
    client, _ = make_client()
    calls = stub_request(
        client,
        {
            "get_status": {
                "code": 200,
                "status": "SUCCESS",
                "data": {
                    "server_list_ping": {
                        "available": True,
                        "reason": "ok",
                        "players": {"online": 2, "max": 20, "sample": [{"name": "A"}, {"name": "B"}]},
                    }
                },
            }
        },
    )
    snapshot = await client.query_players()
    assert snapshot.source == "status"
    assert snapshot.players == ["A", "B"]
    assert calls == ["send_rcon_command", "get_status"]


async def test_query_auto_rejects_incomplete_ping_sample():
    client, _ = make_client()
    stub_request(
        client,
        {
            "get_status": {
                "code": 200,
                "status": "SUCCESS",
                "data": {
                    "server_list_ping": {
                        "available": True,
                        "reason": "ok",
                        "players": {"online": 12, "max": 20, "sample": [{"name": "A"}]},
                    }
                },
            }
        },
    )
    with pytest.raises(PlayerQueryError) as excinfo:
        await client.query_players()
    assert "采样" in str(excinfo.value)
    assert client.players == []


async def test_query_status_only_mode_uses_ping():
    client, _ = make_client(players={"mode": "status", "timeout_seconds": 1})
    stub_request(
        client,
        {
            "get_status": {
                "code": 200,
                "status": "SUCCESS",
                "data": {
                    "server_list_ping": {
                        "available": True,
                        "reason": "ok",
                        "players": {"online": 0, "max": 20, "sample": []},
                    }
                },
            }
        },
    )
    snapshot = await client.query_players()
    assert snapshot.players == []
    assert snapshot.authoritative is True


async def test_query_rcon_only_mode_does_not_fall_back():
    client, _ = make_client(players={"mode": "rcon", "timeout_seconds": 1})
    calls = stub_request(client, {})
    with pytest.raises(PlayerQueryError):
        await client.query_players()
    assert calls == ["send_rcon_command"]


# --- 断线语义 ----------------------------------------------------------------


async def test_teardown_clears_players_and_rejects_pending():
    client, _ = make_client()
    attach_connected(client)
    client.players = ["A"]
    client.max_players = 20
    pending = asyncio.get_running_loop().create_future()
    client._pending["echo-1"] = pending
    await client._teardown_connection()
    assert client.connected is False
    assert client.players == []
    assert client.max_players is None
    with pytest.raises(PlayerQueryError):
        await pending


async def test_join_leave_increment_only_after_authoritative_snapshot():
    client, _ = make_client()
    # 没有权威快照时，增量事件不能凭空虚造名单
    client._apply_incremental(MCEvent(server="主服", type="join", player="A", event_id="e0"))
    assert client.players == []

    client.player_source = "rcon"
    client._apply_incremental(MCEvent(server="主服", type="join", player="A", event_id="e1"))
    client._apply_incremental(MCEvent(server="主服", type="join", player="A", event_id="e2"))
    assert client.players == ["A"]

    client._apply_incremental(MCEvent(server="主服", type="leave", player="A", event_id="e3"))
    assert client.players == []


# --- M4：查询与 join/leave 合并 / UUID 优先 / 并发与 generation 保护 --------------


def list_response(players: list[str]) -> dict:
    return {
        "code": 200,
        "status": "SUCCESS",
        "data": f"There are {len(players)} of a max of 20 players online: " + ", ".join(players),
    }


async def test_query_merges_join_that_arrived_during_query():
    """旧快照不能覆盖查询期间发生的 join。"""
    client, _ = make_client(players={"mode": "rcon", "timeout_seconds": 1})
    attach_connected(client)
    client.players = ["Alice"]
    client.player_source = "rcon"
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_request(api: str, data: dict, timeout: float) -> dict:
        started.set()
        await release.wait()
        return list_response(["Alice"])

    client._request = fake_request  # type: ignore[method-assign]

    task = asyncio.create_task(client.query_players())
    await asyncio.wait_for(started.wait(), timeout=2)
    client._apply_incremental(
        MCEvent(server="主服", type="join", player="Bob", uuid="uuid-b", event_id="e1")
    )
    assert client.players == ["Alice", "Bob"]
    release.set()
    snapshot = await asyncio.wait_for(task, timeout=2)
    assert snapshot.players == ["Alice", "Bob"]
    assert client.players == ["Alice", "Bob"]


async def test_query_merges_leave_that_arrived_during_query():
    client, _ = make_client(players={"mode": "rcon", "timeout_seconds": 1})
    attach_connected(client)
    client.players = ["Alice", "Bob"]
    client.player_source = "rcon"
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_request(api: str, data: dict, timeout: float) -> dict:
        started.set()
        await release.wait()
        return list_response(["Alice", "Bob"])

    client._request = fake_request  # type: ignore[method-assign]

    task = asyncio.create_task(client.query_players())
    await asyncio.wait_for(started.wait(), timeout=2)
    client._apply_incremental(
        MCEvent(server="主服", type="leave", player="Alice", uuid="uuid-a", event_id="e2")
    )
    assert client.players == ["Bob"]
    release.set()
    snapshot = await asyncio.wait_for(task, timeout=2)
    assert snapshot.players == ["Bob"]
    assert client.players == ["Bob"]


async def test_uuid_priority_prevents_duplicate_entry_and_removes_by_name():
    client, _ = make_client()
    client.players = ["Alice"]
    client.player_source = "rcon"
    client._apply_incremental(
        MCEvent(server="主服", type="join", player="Alice", uuid="uuid-a", event_id="e1")
    )
    assert client.players == ["Alice"]  # 同名同 UUID 不能重复计入
    assert client._entries[0].uuid == "uuid-a"  # 快照没 UUID 时用事件补齐
    client._apply_incremental(
        MCEvent(server="主服", type="join", player="ALICE", uuid="uuid-a", event_id="e2")
    )
    assert client.players == ["Alice"]  # UUID 相同、大小写不同的名字也视为同一人

    client._apply_incremental(
        MCEvent(server="主服", type="leave", player="alice", uuid="uuid-a", event_id="e3")
    )
    assert client.players == []


async def test_query_result_discarded_when_connection_rebuilt_during_query():
    client, _ = make_client(players={"mode": "rcon", "timeout_seconds": 1})
    attach_connected(client)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_request(api: str, data: dict, timeout: float) -> dict:
        started.set()
        await release.wait()
        return list_response(["Alice"])

    client._request = fake_request  # type: ignore[method-assign]

    task = asyncio.create_task(client.query_players())
    await asyncio.wait_for(started.wait(), timeout=2)
    await client._teardown_connection()  # 连接重建：代次 +1
    release.set()
    with pytest.raises(PlayerQueryError) as excinfo:
        await asyncio.wait_for(task, timeout=2)
    assert "重建" in str(excinfo.value)
    assert client.players == []


async def test_concurrent_queries_are_serialized_and_last_snapshot_wins():
    client, _ = make_client(players={"mode": "rcon", "timeout_seconds": 1})
    attach_connected(client)
    active = 0
    peak = 0
    payloads = [["Alice"], ["Alice", "Bob"]]

    async def fake_request(api: str, data: dict, timeout: float) -> dict:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return list_response(payloads.pop(0))

    client._request = fake_request  # type: ignore[method-assign]

    first, second = await asyncio.gather(client.query_players(), client.query_players())
    assert peak == 1  # 同一连接上的查询串行化，不会互相覆盖
    assert first.players == ["Alice"]
    assert second.players == ["Alice", "Bob"]
    assert client.players == ["Alice", "Bob"]


# --- M8：QueQiao 日志/状态不泄露上游原文 ----------------------------------------


async def test_upstream_error_message_never_logged_or_in_status(caplog):
    canary = "CANARY_SECRET_ONLY_TEST"
    client, _ = make_client()
    ws = attach_connected(client)
    with caplog.at_level(logging.DEBUG, logger="mcqq_bridge"):
        task = asyncio.create_task(client.broadcast("hi"))
        await asyncio.sleep(0)
        echo = json.loads(ws.sent[-1])["echo"]
        await client._handle_frame(
            json.dumps({"code": 500, "status": "FAILED", "message": canary, "echo": echo})
        )
        assert await asyncio.wait_for(task, timeout=2) is False
        snapshot = json.dumps(client.status_payload(), ensure_ascii=False)
    assert canary not in caplog.text
    assert canary not in snapshot


async def test_connect_error_is_controlled():
    from mcqq_bridge.queqiao import _connect_error

    class Boom(Exception):
        def __init__(self) -> None:
            super().__init__("wss://user:pass@queqiao.example/ws CANARY")
            self.response = type("R", (), {"status_code": 403})()

    message = _connect_error(Boom())
    assert message == "connect_failed:Boom:http_403"
    assert "CANARY" not in message and "user:pass" not in message
