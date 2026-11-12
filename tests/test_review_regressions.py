import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings


def app(**kwargs):
    return create_app(Settings(_env_file=None, allowed_group_ids=[123456], **kwargs))


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan'), 123456.9, True, [], {}])
def test_invalid_ids_fail_closed(value):
    with TestClient(app(onebot_webhook_token='test')) as c:
        event = dict(post_type='message', message_type='group', group_id=value,
                     user_id=42, message='/mc players')
        r = c.post('/api/v1/onebot/events', content=json.dumps(event),
                   headers={'Authorization': 'Bearer test'})
        assert r.status_code == 202
        assert r.json()['accepted'] is False


def test_non_ascii_signature_rejected():
    with TestClient(app(onebot_webhook_secret='secret')) as c:
        r = c.post('/api/v1/onebot/events', content=b'{}', headers={b'X-Signature': b'sha1=\xff'})
        assert r.status_code == 401


def test_ws_invalid_id_does_not_prevent_next_query():
    a = app(onebot_enabled=True, onebot_transport='websocket', onebot_access_token='test')
    with TestClient(a) as c:
        with c.websocket_connect('/api/v1/onebot/ws', headers={'Authorization':'Bearer test'}) as ws:
            event = dict(post_type='message', message_type='group', group_id=123456,
                         user_id=42, message='/mc players')
            ws.send_text(json.dumps({**event, 'group_id': float('inf')}))
            ws.send_json({**event, 'message': [{'type':'text','data':None}]})
            ws.send_json(event)
            frame = ws.receive_json()
            assert frame['action'] == 'send_group_msg'
            ws.send_json({'echo':frame['echo'], 'status':'ok', 'retcode':0})


def test_accepted_query_distinguishes_failed_delivery():
    with TestClient(app(onebot_webhook_token='test')) as c:
        r = c.post('/api/v1/onebot/events', json=dict(
            post_type='message', message_type='group', group_id=123456,
            user_id=42, message='/mc players'), headers={'Authorization':'Bearer test'})
        assert r.json() == {'accepted': True, 'delivered': False}


def test_state_unknown_stale_and_offline(monkeypatch):
    a = app()
    state = a.state.bridge.state
    with TestClient(a) as c:
        assert c.get('/api/v1/state/status').json()['online'] is None
        assert c.get('/api/v1/state/meta').json()['status']['freshness'] == 'unknown'
        state.update_status({'online': True})
        state.update_players(['Alex'], 20)
        assert c.get('/api/v1/state/meta').json()['status']['freshness'] == 'fresh'
        monkeypatch.setattr('mcqq_bridge.state.time.monotonic', lambda: state.status_monotonic + 121)
        assert c.get('/api/v1/state/meta').json()['status']['freshness'] == 'stale'
        state.update_status({'online': False})
        assert c.get('/api/v1/state/players').json()['players'] == []


@pytest.mark.asyncio
async def test_failed_retcode_logs_warning(caplog):
    from mcqq_bridge.onebot import OneBotHttpAdapter
    adapter = OneBotHttpAdapter(Settings(_env_file=None, onebot_enabled=True, allowed_group_ids=[123456]))
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'status':'failed', 'retcode':100})))
    try:
        assert not await adapter.send_group_message(123456, 'test')
        assert 'not delivered' in caplog.text
    finally:
        await adapter.close()
