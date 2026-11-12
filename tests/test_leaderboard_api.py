"""只读 JSON 接口：与文本同源、schema_version、404/503、无隐私字段（验收 12）。"""

from __future__ import annotations

import httpx
import pytest

from leaderboard_helpers import (
    UUID_MAIN,
    UUID_MAIN_ALT,
    UUID_OTHER,
    FakeClient,
    FakeClock,
    FakeManager,
    make_instance,
    start_service,
    write_stats,
    write_usercache,
)
from mcqq_bridge.leaderboard_text import render_leaderboard
from mcqq_bridge.leaderboards import LeaderboardService
from mcqq_bridge.runtime import BridgeRuntime, create_status_app

T0 = 1_760_000_000.0


async def get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def build(tmp_path, players: int = 3):
    root_a, stats_a = make_instance(tmp_path, "a")
    root_b, stats_b = make_instance(tmp_path, "b")
    write_usercache(
        root_a,
        [
            ("MUEZORE", UUID_MAIN, "2026-11-02 00:00:00 +0000"),
            ("MUEZORE", UUID_MAIN_ALT, "2026-11-01 00:00:00 +0000"),
            ("yui_Thefish", UUID_OTHER, "2026-11-01 00:00:00 +0000"),
        ],
    )
    write_stats(stats_a, UUID_MAIN, ticks=1_374_540, deaths=40, mtime=T0 - 100)
    write_stats(stats_a, UUID_MAIN_ALT, ticks=0, deaths=42, mtime=T0 - 100)
    write_stats(stats_a, UUID_OTHER, ticks=7, deaths=0, mtime=T0 - 100)
    write_stats(stats_b, UUID_MAIN, ticks=5231, deaths=115, mtime=T0 - 100)

    from helpers import make_config, server_entry

    config = make_config(
        servers=[
            server_entry("齿轮盛宴", 19000, stats_dir=str(stats_a)),
            server_entry("亡者世界", 19001, stats_dir=str(stats_b)),
            server_entry("资源服", 19002, enabled=False, stats_dir=str(stats_a)),
        ],
        stats={"db_path": str(tmp_path / "stats.db")},
    )
    manager = FakeManager([FakeClient("齿轮盛宴", []), FakeClient("亡者世界", [])])
    service = await start_service(config, manager=manager, clock=FakeClock(T0))
    runtime = BridgeRuntime(config)
    runtime.leaderboards = service
    runtime.config = config
    return runtime, service, config


async def test_api_matches_text_source_and_schema_version(tmp_path):
    runtime, service, _ = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        response = await get(app, "/api/v1/leaderboards/deaths")
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema_version"] == 1
        assert payload["kind"] == "deaths"
        assert payload["generated_at"].endswith("Z")
        assert [item["server"] for item in payload["servers"]] == ["齿轮盛宴", "亡者世界"]
        assert payload["servers"][0]["total_players"] == 2
        assert payload["servers"][0]["rows"][0] == {
            "rank": 1,
            "player": "MUEZORE",
            "value": 82,
            "display": "82 次",
            "online": False,
        }
        # 同一 service、同一时钟：文本渲染必须与 JSON 完全一致
        board = await service.get_board("deaths")
        text = "\n".join(render_leaderboard(board, 1500, 5))
        for server in payload["servers"]:
            assert f"{server['server']} 死亡榜：" in text
            for row in server["rows"]:
                assert f"{row['rank']}. {row['player']}  {row['display']}" in text
    finally:
        await service.stop()


async def test_api_single_server_filter(tmp_path):
    runtime, service, _ = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        response = await get(app, "/api/v1/leaderboards/playtime?server=亡者世界")
        payload = response.json()
        assert len(payload["servers"]) == 1
        assert payload["servers"][0]["server"] == "亡者世界"
        assert payload["servers"][0]["rows"][0]["value"] == 261  # 5231 // 20
        assert payload["servers"][0]["rows"][0]["display"] == "04m21s"
    finally:
        await service.stop()


async def test_api_unknown_and_disabled_server_return_404(tmp_path):
    runtime, service, _ = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        for name in ("MUEZORE", "资源服"):
            response = await get(app, f"/api/v1/leaderboards/deaths?server={name}")
            assert response.status_code == 404
            assert response.json()["detail"] == f"未找到服务器：{name}"
    finally:
        await service.stop()


async def test_api_invalid_kind_returns_404(tmp_path):
    runtime, service, _ = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        assert (await get(app, "/api/v1/leaderboards/kills")).status_code == 404
    finally:
        await service.stop()


async def test_api_returns_503_when_leaderboard_unavailable(tmp_path):
    from mcqq_bridge.leaderboard_models import LeaderboardUnavailable

    runtime, service, _ = await build(tmp_path)

    async def boom(kind, server=None):
        raise LeaderboardUnavailable("排行榜数据暂不可用")

    try:
        service.get_board = boom  # type: ignore[assignment]
        app = create_status_app(runtime)
        response = await get(app, "/api/v1/leaderboards/deaths")
        assert response.status_code == 503
        assert response.json()["detail"] == "排行榜数据暂不可用"
    finally:
        await service.stop()


async def test_api_has_no_privacy_fields(tmp_path):
    runtime, service, config = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        body = (await get(app, "/api/v1/leaderboards/playtime")).text
        for secret in (UUID_MAIN, UUID_MAIN_ALT, UUID_OTHER, str(tmp_path), "19000", "GROUP_OPENID"):
            assert secret not in body
        assert set((await get(app, "/api/v1/leaderboards/playtime")).json()) == {
            "kind",
            "schema_version",
            "generated_at",
            "servers",
        }
    finally:
        await service.stop()


async def test_api_is_not_truncated(tmp_path):
    # 接口必须全量返回（截断只属于文本渲染层）
    from helpers import make_config, server_entry

    root, stats_dir = make_instance(tmp_path, "many")
    entries = []
    for index in range(120):
        uuid_value = f"11111111-2222-3333-4444-{index:012d}"
        write_stats(stats_dir, uuid_value, ticks=(index + 1) * 20, deaths=index)
        entries.append((f"P{index:03d}", uuid_value, "2026-11-01 00:00:00 +0000"))
    write_usercache(root, entries)
    config = make_config(
        servers=[server_entry("大服", 19010, stats_dir=str(stats_dir))],
        stats={"db_path": str(tmp_path / "stats.db")},
    )
    service = await start_service(config, manager=FakeManager([FakeClient("大服", [])]), clock=FakeClock(T0))
    runtime = BridgeRuntime(config)
    runtime.leaderboards = service
    try:
        app = create_status_app(runtime)
        payload = (await get(app, "/api/v1/leaderboards/deaths")).json()
        assert payload["servers"][0]["total_players"] == 120
        assert len(payload["servers"][0]["rows"]) == 120
        assert payload["servers"][0]["rows"][-1]["rank"] == 120
    finally:
        await service.stop()


async def test_api_empty_board_is_200_with_empty_rows(tmp_path):
    from helpers import make_config, server_entry

    _, stats_dir = make_instance(tmp_path, "empty")
    config = make_config(
        servers=[server_entry("空服", 19011, stats_dir=str(stats_dir))],
        stats={"db_path": str(tmp_path / "stats.db")},
    )
    service = await start_service(config, manager=FakeManager([FakeClient("空服", [])]), clock=FakeClock(T0))
    runtime = BridgeRuntime(config)
    runtime.leaderboards = service
    try:
        app = create_status_app(runtime)
        response = await get(app, "/api/v1/leaderboards/deaths")
        assert response.status_code == 200
        assert response.json()["servers"][0]["rows"] == []
    finally:
        await service.stop()


async def test_status_and_health_endpoints_unchanged(tmp_path):
    runtime, service, _ = await build(tmp_path)
    try:
        app = create_status_app(runtime)
        assert (await get(app, "/healthz")).json() == {"status": "ok"}
        ready = (await get(app, "/readyz")).json()
        assert ready["status"] == "degraded"  # 未连接：既有语义不变
        assert "servers" in (await get(app, "/api/v1/status")).json()
    finally:
        await service.stop()