import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings


def config(**kwargs):
    return Settings(_env_file=None, allowed_group_ids=[123456], **kwargs)


def test_env_example_loads_without_validation_error():
    # 2026-11-11 迁移：.env.example 现在服务于官方 QQ + QueQiao 新运行时（config.yaml + ${ENV}），
    # 旧 OneBot Settings 的模板归档到 docs/LEGACY_ONEBOT_ENV.example，本回归测试改读该归档，
    # 断言意图不变：旧 Settings 能无异常加载一份不含非法值的模板。
    s = Settings(_env_file=Path(__file__).parents[1] / 'docs' / 'LEGACY_ONEBOT_ENV.example')
    assert s.allowed_group_ids == [123456789]
    assert s.mc_commands == ['players', 'status']
    assert s.onebot_self_id is None


def test_no_unauthenticated_onebot_ingress():
    with TestClient(create_app(config())) as client:
        assert client.post('/api/v1/onebot/events', json={}).status_code == 401


@pytest.mark.parametrize('body', [[], None, {'sender': 'bad'}, {'message': [{'type': 'image'}]}])
def test_invalid_onebot_shapes_do_not_crash(body):
    with TestClient(create_app(config(onebot_webhook_token='test'))) as client:
        response = client.post('/api/v1/onebot/events', content=json.dumps(body),
                               headers={'Authorization': 'Bearer test'})
        assert response.status_code in {202, 422}


def test_bot_self_id_does_not_drop_all_messages():
    s = config(onebot_self_id=100, onebot_webhook_token='test')
    with TestClient(create_app(s)) as client:
        event = dict(post_type='message', message_type='group', self_id=100,
                     user_id=42, group_id=123456, message='/mc players')
        response = client.post('/api/v1/onebot/events', json=event,
                               headers={'Authorization': 'Bearer test'})
        assert response.json()['accepted'] is True
        event['user_id'] = 100
        assert client.post('/api/v1/onebot/events', json=event,
                           headers={'Authorization': 'Bearer test'}).json()['accepted'] is False


def test_exact_prefix_required():
    with TestClient(create_app(config(onebot_webhook_token='test'))) as client:
        response = client.post('/api/v1/onebot/events', json=dict(
            post_type='message', message_type='group', user_id=42,
            group_id=123456, message='/mcevil players'), headers={'Authorization': 'Bearer test'})
        assert response.json()['accepted'] is False


@pytest.mark.asyncio
async def test_outbound_cq_is_plain_text():
    from mcqq_bridge.onebot import OneBotHttpAdapter
    adapter = OneBotHttpAdapter(config(onebot_enabled=True))
    sent = []
    def receive(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'ok', 'retcode': 0})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        assert await adapter.send_group_message(123456, '[CQ:at,qq=all]')
        assert sent[0]['auto_escape'] is True
    finally:
        await adapter.close()


def test_websocket_universal_reply_and_ack():
    s = config(onebot_enabled=True, onebot_transport='websocket', onebot_access_token='test')
    with TestClient(create_app(s)) as client:
        with client.websocket_connect(s.onebot_ws_path, headers={
            'Authorization': 'Bearer test', 'X-Client-Role': 'Universal', 'X-Self-ID': '100'
        }) as socket:
            socket.send_json(dict(post_type='message', message_type='group', self_id=100,
                                  user_id=42, group_id=123456, message='/mc players'))
            frame = socket.receive_json()
            assert frame['action'] == 'send_group_msg'
            assert frame['params']['group_id'] == 123456
            assert frame['params']['auto_escape'] is True
            socket.send_json({'status': 'ok', 'retcode': 0, 'echo': frame['echo']})


@pytest.mark.asyncio
async def test_callback_failure_returns_false():
    from mcqq_bridge.mc_adapter import HttpMcCallback
    adapter = HttpMcCallback(config())
    def receive(request):
        raise httpx.ConnectError('test offline')
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        assert await adapter.send(dict(event='chat', message='hi', group_id=123456, user_id=42)) is False
    finally:
        await adapter.close()
