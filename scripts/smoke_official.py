#!/usr/bin/env python3
"""端到端协议 smoke：一条命令跑完 QQ 官方 × QueQiao 多服务器链路。

不依赖 pytest，也不连接真实 QQ/真实 Minecraft：
* 4 台本地真实 TCP WebSocket 服务器模拟 QueQiao（Protocol V2，含握手头校验、
  ``send_rcon_command``/``get_status``/``broadcast`` 应答、事件推送）；
* 其中 3 台启用（各一条独立连接）、1 台配置为禁用，验证「不连接」；
* 1 个本地真实 TCP WebSocket 网关模拟 QQ 官方网关（Hello/Identify/READY/心跳）；
* QQ HTTP 用 ``httpx.MockTransport`` 记录真实请求体，不联网。

运行：``.venv/bin/python scripts/smoke_official.py``
通过时打印 ``SMOKE PASS`` 并返回 0；任何一步失败返回 1。
"""

from __future__ import annotations

import asyncio
import json

import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

import httpx
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcqq_bridge.config import AppConfig  # noqa: E402
from mcqq_bridge.leaderboards import LeaderboardService  # noqa: E402
from mcqq_bridge.manager import ServerManager  # noqa: E402
from mcqq_bridge.qq_official import QQBotClient  # noqa: E402
from mcqq_bridge.router import BridgeRouter, format_list  # noqa: E402
from mcqq_bridge.runtime import BridgeRuntime, create_status_app  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class MockQueQiao:
    def __init__(self, name: str, players: list[str], *, rcon_enabled: bool = True) -> None:
        self.name = name
        self.players = players
        self.rcon_enabled = rcon_enabled
        self.connections = 0
        self.self_names: list[str] = []
        self.broadcasts: list[str] = []
        self.ws: Any = None
        self.port = 0
        self.server: Any = None

    async def start(self) -> None:
        self.server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, websocket) -> None:
        self.connections += 1
        self.self_names.append(unquote(websocket.request.headers.get("x-self-name", "")))
        self.ws = websocket
        try:
            async for raw in websocket:
                message = json.loads(raw)
                await websocket.send(json.dumps(self._respond(message), ensure_ascii=False))
        except Exception:
            pass

    def _respond(self, message: dict[str, Any]) -> dict[str, Any]:
        api = message.get("api")
        echo = message.get("echo")
        base = {"api": api, "post_type": "response", "echo": echo}
        if api == "send_rcon_command":
            if not self.rcon_enabled:
                return {**base, "code": 400, "status": "FAILED", "message": "rcon disabled", "data": None}
            data = (
                f"There are {len(self.players)} of a max of 20 players online: "
                + ", ".join(self.players)
            )
            return {**base, "code": 200, "status": "SUCCESS", "message": "success", "data": data}
        if api == "get_status":
            return {
                **base,
                "code": 200,
                "status": "SUCCESS",
                "message": "success",
                "data": {
                    "server_list_ping": {
                        "available": True,
                        "reason": "ok",
                        "players": {
                            "online": len(self.players),
                            "max": 20,
                            "sample": [{"name": player} for player in self.players],
                        },
                    }
                },
            }
        if api == "broadcast":
            self.broadcasts.append(message["data"]["message"]["text"])
            return {**base, "code": 200, "status": "SUCCESS", "message": "success", "data": None}
        return {**base, "code": 404, "status": "FAILED", "message": f"Unknown API {api}", "data": None}

    async def push(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload, ensure_ascii=False))


class MockGateway:
    def __init__(self) -> None:
        self.ws: Any = None
        self.identify: dict[str, Any] | None = None
        self.port = 0
        self.server: Any = None

    async def start(self) -> None:
        self.server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, websocket) -> None:
        self.ws = websocket
        await websocket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        self.identify = json.loads(await websocket.recv())
        await websocket.send(json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S"}}))
        try:
            async for raw in websocket:
                if json.loads(raw).get("op") == 1:
                    await websocket.send(json.dumps({"op": 11}))
        except Exception:
            pass

    async def push(self, payload: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(payload, ensure_ascii=False))


class HttpRecorder:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.bot_state_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "SMOKE-TOKEN", "expires_in": "7200"})
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
        self.bodies.append(body)
        self.sent.append(body.get("content", ""))
        return httpx.Response(200, json={"id": f"OUT-{len(self.sent)}"})


def group_event(text: str, message_id: str) -> dict[str, Any]:
    return {
        "op": 0,
        "s": 2,
        "t": "GROUP_MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "author": {"id": "u1", "member_openid": "m1", "username": "小明", "bot": False},
            "content": text,
            "group_openid": "GROUP_OPENID_1",
            "message_type": 0,
        },
    }


# 本地模拟的真实 stats/usercache 格式：主服 6 人（含 0 值），创造服 1 人，生存服不采集
DEFEAT_PLAYERS: list[tuple[str, str, int, int]] = [
    ("Alice", "11111111-1111-3111-8111-000000000001", 60, 3),
    ("Bob", "11111111-1111-3111-8111-000000000002", 20, 1),
    ("Carol", "11111111-1111-3111-8111-000000000003", 0, 0),
    ("Dave", "11111111-1111-3111-8111-000000000004", 400, 7),
    ("Eve", "11111111-1111-3111-8111-000000000005", 200, 2),
    ("Frank", "11111111-1111-3111-8111-000000000006", 1000, 5),
]


def build_stats_fixtures(root: Path) -> dict[str, str]:
    """建两套实例目录（world/stats + usercache.json），返回 {服务器名: stats_dir}。"""
    created: dict[str, str] = {}
    for label, players in (("main", DEFEAT_PLAYERS), ("creative", DEFEAT_PLAYERS[:1])):
        instance = root / label
        stats_dir = instance / "world" / "stats"
        stats_dir.mkdir(parents=True, exist_ok=True)
        usercache = []
        for name, uuid_value, ticks, deaths in players:
            (stats_dir / f"{uuid_value}.json").write_text(
                json.dumps(
                    {
                        "stats": {
                            "minecraft:custom": {
                                "minecraft:play_time": ticks,
                                "minecraft:deaths": deaths,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            usercache.append(
                {"name": name, "uuid": uuid_value, "expiresOn": "2026-11-01 00:00:00 +0000"}
            )
        (instance / "usercache.json").write_text(json.dumps(usercache), encoding="utf-8")
        created[label] = str(stats_dir)
    return created


class _StatusView:
    """给 create_status_app 用的最小运行时视图（只读状态 + 排行榜）。"""

    def __init__(self, config, manager, qq, service) -> None:
        self.config = config
        self.manager = manager
        self.qq = qq
        self.leaderboards = service

    def status_payload(self) -> dict[str, Any]:
        return {"qq": self.qq.status_payload(), "servers": self.manager.status_payload()["servers"]}


async def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="mcqq-smoke-"))
    fixtures = build_stats_fixtures(tmp_root)

    main_server = MockQueQiao("主服", ["Alice", "Bob"])
    second = MockQueQiao("生存服", [])
    third = MockQueQiao("创造服", ["Carol"])
    disabled = MockQueQiao("资源服", ["Dave"])
    gateway = MockGateway()
    recorder = HttpRecorder()

    for server in (main_server, second, third, disabled):
        await server.start()
    await gateway.start()

    config = AppConfig.model_validate(
        {
            "bot": {
                "app_id": "102000000",
                "client_secret": "smoke-secret",
                "group_openid": "GROUP_OPENID_1",
                "api_base": "http://api.smoke",
                "gateway_url": f"ws://127.0.0.1:{gateway.port}",
                "send_rate_per_minute": 600,
                "max_message_chars": 60,
                "request_timeout_seconds": 2,
            },
            "servers": [
                {
                    "name": "主服",
                    "host": "127.0.0.1",
                    "port": main_server.port,
                    "players": {"mode": "auto", "timeout_seconds": 2},
                    "stats_dir": fixtures["main"],
                },
                {
                    "name": "生存服",
                    "host": "127.0.0.1",
                    "port": second.port,
                    "players": {"mode": "auto", "timeout_seconds": 2},
                },
                {
                    "name": "创造服",
                    "host": "127.0.0.1",
                    "port": third.port,
                    "stats_dir": fixtures["creative"],
                },
                {
                    "name": "资源服",
                    "host": "127.0.0.1",
                    "port": disabled.port,
                    "enabled": False,
                    "stats_dir": fixtures["main"],
                },
            ],
            "relay": {"dedupe_ttl_seconds": 60, "loop_guard_seconds": 2},
            "stats": {"db_path": str(tmp_root / "stats.db")},
            "runtime": {"status_http": {"enabled": False}},
            "logging": {"level": "WARNING"},
        }
    )

    holder: dict[str, Any] = {}
    ready = asyncio.Event()

    async def on_mc(event) -> None:
        await ready.wait()
        # 与 runtime 相同顺序：统计钩子在 relay 开关之前，先投统计再转发
        holder["leaderboards"].offer_event(event)
        await holder["router"].on_mc_event(event)

    async def on_qq(message) -> None:
        await ready.wait()
        await holder["router"].on_qq_message(message)

    manager = ServerManager(config, on_mc)
    leaderboards = LeaderboardService(config, manager=manager, loops=False)
    await leaderboards.start()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    qq = QQBotClient(config.bot, config.relay, on_qq, http_client=http_client)
    router = BridgeRouter(config, manager, qq, leaderboards=leaderboards)
    holder["router"] = router
    holder["leaderboards"] = leaderboards
    ready.set()

    try:
        await manager.start()
        await qq.start()
        connected = await wait_until(
            lambda: qq.connected
            and manager.client("主服").connected
            and manager.client("生存服").connected
            and manager.client("创造服").connected
        )
        check("QQ 网关与本机 3 台启用 QueQiao 均已连接", connected and manager.connected_count == 3)

        synced = await wait_until(lambda: manager.client("主服").players == ["Alice", "Bob"])
        check("连接后玩家名单同步（send_rcon_command → list）", synced,
              str(manager.client("主服").players))
        check(
            "x-self-name 握手头按 QueQiao 规则 URL 解码后匹配",
            main_server.self_names == ["主服"]
            and second.self_names == ["生存服"]
            and third.self_names == ["创造服"],
            f"{main_server.self_names} / {second.self_names} / {third.self_names}",
        )
        check(
            "3 台启用各自一条独立连接，禁用服务器零连接",
            main_server.connections == 1
            and second.connections == 1
            and third.connections == 1
            and disabled.connections == 0,
            f"connections={[main_server.connections, second.connections, third.connections, disabled.connections]}",
        )

        await gateway.push(group_event("/list", "SMOKE-LIST"))
        listed = await wait_until(lambda: len(recorder.sent) == 1)
        expected = (
            "主服 2人在线：\nAlice\nBob\n\n生存服 0人在线\n\n创造服 1人在线：\nCarol"
        )
        check("/list 只返回启用服务器（禁用项不查询不展示）且命令不下发 MC",
              listed and recorder.sent[0] == expected and "资源服" not in recorder.sent[0],
              repr(recorder.sent[0] if recorder.sent else ""))
        check("/list 未广播到任何 MC 服务器",
              main_server.broadcasts == [] and second.broadcasts == [] and third.broadcasts == [])

        # ---- 排行榜：真实 stats 文件 → service → router → QQ HTTP 全链路 ----
        mark = len(recorder.sent)
        await gateway.push(group_event("/death", "SMOKE-DEATH"))
        got = await wait_until(lambda: len(recorder.sent) > mark)
        await asyncio.sleep(0.3)  # 等分片逐条发完
        parts = list(recorder.sent[mark:])
        bodies = list(recorder.bodies[mark:])
        death_text = "\n".join(parts)
        check(
            "无参 /death 输出全部启用服 × 全部玩家（含 0 值，禁用服整段不出现）",
            got
            and "主服 死亡榜：" in death_text
            and "1. Dave  7 次" in death_text
            and "Carol  0 次" in death_text
            and "Frank" in death_text
            and "生存服 暂无数据" in death_text
            and "资源服" not in death_text,
            repr(parts),
        )
        check(
            "排行榜回复全部是被动回复（携带同一 msg_id，msg_seq 从 1 递增）",
            bool(bodies)
            and all(body.get("msg_id") == "SMOKE-DEATH" for body in bodies)
            and [body.get("msg_seq") for body in bodies] == list(range(1, len(bodies) + 1)),
            str([(body.get("msg_id"), body.get("msg_seq")) for body in bodies]),
        )
        names = {name for name, _, _, _ in DEFEAT_PLAYERS}
        row_lines = [
            line
            for part in parts
            for line in part.splitlines()
            if line[:1].isdigit()
        ]
        check(
            "长名单按服务器/玩家行边界分片，不切断玩家行且不超单片上限",
            bool(row_lines)
            and all(len(part) <= 60 for part in parts)
            and all("次" in line for line in row_lines)
            and all(line.split("  ")[0].split(". ", 1)[-1] in names for line in row_lines),
            f"parts={len(parts)} rows={len(row_lines)}",
        )

        mark = len(recorder.sent)
        await gateway.push(group_event("<@member_openid> /Playtime 主服 ignored", "SMOKE-PT"))
        await wait_until(lambda: len(recorder.sent) > mark)
        await asyncio.sleep(0.2)
        pt_text = "\n".join(recorder.sent[mark:])
        check(
            "@ 前缀剥离 + 只匹配服务器名 + 多余参数忽略 + 秒级格式",
            "主服 在线时长榜：" in pt_text
            and "1. Frank  50s" in pt_text
            and "2. Dave  20s" in pt_text
            and "03s" in pt_text
            and "生存服" not in pt_text
            and "创造服" not in pt_text,
            repr(recorder.sent[mark:]),
        )

        mark = len(recorder.sent)
        await gateway.push(group_event("/death 资源服", "SMOKE-DISABLED"))
        await wait_until(lambda: len(recorder.sent) > mark)
        check(
            "禁用服务器参数回「未找到服务器」且不查询",
            recorder.sent[mark:] == ["未找到服务器：资源服"],
            repr(recorder.sent[mark:]),
        )

        mark = len(recorder.sent)
        await gateway.push(group_event("/death MUEZORE", "SMOKE-PLAYER"))
        await wait_until(lambda: len(recorder.sent) > mark)
        check(
            "参数只匹配服务器：玩家名不得回退成玩家查询",
            recorder.sent[mark:] == ["未找到服务器：MUEZORE"],
            repr(recorder.sent[mark:]),
        )

        check(
            "排行榜命令不下发 MC",
            main_server.broadcasts == [] and second.broadcasts == [] and third.broadcasts == [],
        )

        # 结构化接口：与文本同源、带 schema_version、全量不截断
        view = _StatusView(config, manager, qq, leaderboards)
        transport = httpx.ASGITransport(app=create_status_app(cast("BridgeRuntime", view)))
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as api:
            response = await api.get("/api/v1/leaderboards/deaths")
            payload = response.json()
            forbidden = await api.get("/api/v1/leaderboards/deaths?server=资源服")
        dto = await leaderboards.get_board("deaths")
        expected_rows = [
            [row.to_dict() for row in server_board.rows] for server_board in dto.servers
        ]
        check(
            "GET /api/v1/leaderboards/deaths 与文本同源、带 schema_version、禁用服 404",
            response.status_code == 200
            and payload["schema_version"] == 1
            and [item["rows"] for item in payload["servers"]] == expected_rows
            and [item["server"] for item in payload["servers"]] == ["主服", "生存服", "创造服"]
            and forbidden.status_code == 404
            and "11111111-1111" not in response.text
            and str(tmp_root) not in response.text,
            f"servers={[item['server'] for item in payload.get('servers', [])]} rows={len(expected_rows)}",
        )

        await gateway.push(group_event("你好", "SMOKE-MSG"))
        broadcasted = await wait_until(
            lambda: main_server.broadcasts == ["小明：你好"]
            and second.broadcasts == ["小明：你好"]
            and third.broadcasts == ["小明：你好"]
        )
        check("QQ 消息按「昵称：内容」广播到所有在线服务器", broadcasted)

        await main_server.push(
            {
                "timestamp": 1700000000,
                "post_type": "message",
                "sub_type": "player_chat",
                "message_id": "SMOKE-CHAT",
                "player": {"nickname": "Alice", "uuid": "uuid-a"},
                "message": "大家好",
            }
        )
        forwarded = await wait_until(lambda: "[主服] [Alice] ： 大家好" in recorder.sent)
        check("MC 聊天按「[服务器] [玩家] ： 内容」转发 QQ", forwarded,
              repr(recorder.sent[-1] if recorder.sent else ""))

        results = await manager.query()
        check("manager.query 只返回启用服务器（3 台，禁用项被跳过）",
              [result.name for result in results] == ["主服", "生存服", "创造服"],
              format_list(results).replace("\n", " / "))

        check(
            "启动时真实探测 bot_state（allow_proactive_msg=true，无 degraded）",
            recorder.bot_state_calls >= 1
            and qq._bot_state.allow_proactive_msg is True
            and qq.degraded_reasons() == [],
            f"calls={recorder.bot_state_calls} degraded={qq.degraded_reasons()}",
        )
    finally:
        await leaderboards.stop()
        await qq.close()
        await manager.stop()
        await http_client.aclose()
        for server in (main_server, second, third, disabled):
            await server.stop()
        await gateway.stop()
        shutil.rmtree(tmp_root, ignore_errors=True)

    failures = [name for name, ok, _ in CHECKS if not ok]
    print()
    if failures:
        print(f"SMOKE FAIL: {len(failures)}/{len(CHECKS)} 项失败")
        return 1
    print(f"SMOKE PASS: {len(CHECKS)}/{len(CHECKS)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
