import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mcqq_bridge.hermes import HermesApiExecutor
from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings


class FakeOneBot:
    def __init__(self):
        self.sent = []

    async def send_group_message(self, group_id, message):
        self.sent.append((group_id, message))
        return True


class FakeHermes:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"accepted": True, "delivered": True, "reply": "已完成"}

    async def execute(self, prompt, *, actor_id):
        self.calls.append((prompt, actor_id))
        return self.result


def make_settings(**overrides):
    values = dict(
        _env_file=None,
        onebot_webhook_token="qq-token",
        allowed_group_ids=[123456],
        hermes_admin_enabled=False,
        hermes_admin_ids=[42],
    )
    values.update(overrides)
    return Settings(**values)


def event(message, *, group_id=123456, user_id=42):
    return {
        "post_type": "message", "message_type": "group",
        "group_id": group_id, "user_id": user_id, "message": message,
    }


def test_hermes_is_disabled_by_default_and_does_not_call_executor():
    app = create_app(make_settings())
    fake = FakeHermes()
    app.state.bridge.hermes_admin = fake
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/onebot/events", json=event("/mc hermes list files"),
            headers={"Authorization": "Bearer qq-token"},
        )
    assert response.json() == {"accepted": False, "delivered": False}
    assert fake.calls == []


def test_hermes_requires_admin_and_group_allowlist():
    app = create_app(make_settings(hermes_admin_enabled=True))
    fake = FakeHermes()
    app.state.bridge.hermes_admin = fake
    with TestClient(app) as client:
        unauthorized_user = client.post(
            "/api/v1/onebot/events", json=event("/mc hermes do task", user_id=99),
            headers={"Authorization": "Bearer qq-token"},
        )
        unauthorized_group = client.post(
            "/api/v1/onebot/events", json=event("/mc hermes do task", group_id=999),
            headers={"Authorization": "Bearer qq-token"},
        )
    assert unauthorized_user.json() == {"accepted": False, "delivered": False}
    assert unauthorized_group.json() == {"accepted": False, "delivered": False}
    assert fake.calls == []


def test_authorized_hermes_response_is_sent_to_same_group_without_shell_parsing():
    app = create_app(make_settings(hermes_admin_enabled=True))
    fake = FakeHermes()
    app.state.bridge.hermes_admin = fake
    outbound = FakeOneBot()
    app.state.bridge.onebot = outbound
    prompt = "检查文件 $(touch /tmp/should-not-exist); `whoami`"
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/onebot/events", json=event("/mc hermes " + prompt),
            headers={"Authorization": "Bearer qq-token"},
        )
    assert response.json() == {"accepted": True, "delivered": True}
    assert fake.calls == [(prompt, 42)]
    assert outbound.sent == [(123456, "已完成")]


@pytest.mark.asyncio
async def test_hermes_api_executor_uses_documented_endpoint_and_bearer_token():
    settings = make_settings(
        hermes_admin_enabled=True, hermes_api_token="separate-api-key",
        hermes_api_url="http://hermes.test/v1", hermes_model="existing-profile",
    )
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "reply from Hermes"}}]
        })

    executor = HermesApiExecutor(settings)
    await executor.client.aclose()
    executor.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        result = await executor.execute("do the task", actor_id=42)
    finally:
        await executor.close()
    assert result == {"accepted": True, "delivered": True, "reply": "reply from Hermes"}
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer separate-api-key"
    assert json.loads(requests[0].content) == {
        "model": "existing-profile",
        "messages": [{"role": "user", "content": "do the task"}],
        "stream": False,
    }


@pytest.mark.asyncio
async def test_hermes_api_executor_fail_closed_for_wrong_actor_and_upstream_timeout():
    settings = make_settings(
        hermes_admin_enabled=True, hermes_api_token="key", hermes_timeout_seconds=0.1,
    )
    executor = HermesApiExecutor(settings)
    assert (await executor.execute("task", actor_id=99))["accepted"] is False
    await executor.client.aclose()

    async def timeout_handler(request):
        raise httpx.ReadTimeout("offline")

    executor.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    try:
        result = await executor.execute("task", actor_id=42)
    finally:
        await executor.close()
    assert result == {"accepted": True, "delivered": False, "reason": "upstream_failure"}


@pytest.mark.asyncio
async def test_hermes_api_executor_rejects_when_concurrency_is_full():
    settings = make_settings(
        hermes_admin_enabled=True, hermes_api_token="key", hermes_max_concurrency=1,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def receive(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "done"}}]
        })

    executor = HermesApiExecutor(settings)
    await executor.client.aclose()
    executor.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    first = asyncio.create_task(executor.execute("first", actor_id=42))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        second = await executor.execute("second", actor_id=42)
        release.set()
        first_result = await asyncio.wait_for(first, 1)
    finally:
        await executor.close()
    assert second == {"accepted": False, "delivered": False, "reason": "concurrency_limit"}
    assert first_result == {"accepted": True, "delivered": True, "reply": "done"}
