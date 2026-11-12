import pytest
from httpx import ASGITransport, AsyncClient

from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings


@pytest.fixture
def settings():
    return Settings(
        mc_webhook_token="test-mc-token",
        mc_webhook_require_token=True,
        onebot_webhook_token="test-onebot-token",
        allowed_group_ids=[123456],
        onebot_enabled=False,
        qq_to_mc_enabled=True,
        mc_notifications={"chat": True, "death": True, "join": True, "leave": True},
        mc_commands=["players", "status"],
    )


@pytest.mark.asyncio
async def test_health_and_openapi_are_available(settings):
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health = await client.get("/healthz")
        docs = await client.get("/openapi.json")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert docs.status_code == 200
    assert "/api/v1/minecraft/events/{event_type}" in docs.json()["paths"]


@pytest.mark.asyncio
async def test_minecraft_chat_requires_bearer_and_updates_state(settings):
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(
            "/api/v1/minecraft/events/chat",
            json={"player": "Steve", "message": "hello"},
        )
        accepted = await client.post(
            "/api/v1/minecraft/events/chat",
            headers={"Authorization": "Bearer test-mc-token"},
            json={"player": "Steve", "message": "hello"},
        )
        players = await client.get("/api/v1/state/players")
    assert denied.status_code == 401
    assert accepted.status_code == 202
    assert players.json()["players"] == []


@pytest.mark.asyncio
async def test_players_event_is_queryable(settings):
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/minecraft/events/players",
            headers={"Authorization": "Bearer test-mc-token"},
            json={"players": ["Alex", "Steve"], "max_players": 20},
        )
        query = await client.get("/api/v1/state/players")
    assert response.status_code == 202
    assert query.json() == {"players": ["Alex", "Steve"], "count": 2, "max_players": 20}


@pytest.mark.asyncio
async def test_qq_command_is_group_allowlisted_and_only_queries(settings):
    app = create_app(settings)
    app.state.bridge.state.update_players(["Alex"], 20)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unauthorized = await client.post(
            "/api/v1/onebot/events",
            headers={"Authorization": "Bearer test-onebot-token"},
            json={
                "post_type": "message", "message_type": "group", "group_id": 999,
                "raw_message": "/mc players", "message": "/mc players",
                "user_id": 42,
            },
        )
        authorized = await client.post(
            "/api/v1/onebot/events",
            headers={"Authorization": "Bearer test-onebot-token"},
            json={
                "post_type": "message", "message_type": "group", "group_id": 123456,
                "raw_message": "/mc players", "message": "/mc players",
                "user_id": 42,
            },
        )
    assert unauthorized.status_code == 202
    assert unauthorized.json()["accepted"] is False
    assert authorized.status_code == 202
    assert authorized.json()["accepted"] is True


@pytest.mark.asyncio
async def test_unallowlisted_and_arbitrary_commands_are_rejected(settings):
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/onebot/events",
            headers={"Authorization": "Bearer test-onebot-token"},
            json={
                "post_type": "message", "message_type": "group", "group_id": 123456,
                "raw_message": "/mc shell whoami", "message": "/mc shell whoami",
                "user_id": 42,
            },
        )
    assert response.status_code == 202
    assert response.json()["accepted"] is False


@pytest.mark.asyncio
async def test_qq_to_mc_uses_callback_and_is_independently_switchable(settings, monkeypatch):
    app = create_app(settings)
    calls = []

    async def fake_callback(payload):
        calls.append(payload)
        return True

    app.state.bridge.mc_callback.send = fake_callback
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/onebot/events",
            headers={"Authorization": "Bearer test-onebot-token"},
            json={
                "post_type": "message", "message_type": "group", "group_id": 123456,
                "raw_message": "/mc say hello Minecraft", "message": "/mc say hello Minecraft",
                "user_id": 42, "sender": {"user_id": 42, "nickname": "tester"},
            },
        )
    assert response.json()["accepted"] is False
    assert calls == []

    settings.mc_commands.append("say")
    settings.mc_say_enabled = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/onebot/events",
            headers={"Authorization": "Bearer test-onebot-token"},
            json={
                "post_type": "message", "message_type": "group", "group_id": 123456,
                "raw_message": "/mc say hello Minecraft", "message": "/mc say hello Minecraft",
                "user_id": 42, "sender": {"user_id": 42, "nickname": "tester"},
            },
        )
    assert response.json()["accepted"] is True
    assert calls[0]["event"] == "chat"
    assert calls[0]["message"] == "hello Minecraft"
