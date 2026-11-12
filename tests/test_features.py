import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from mcqq_bridge.main import create_app
from mcqq_bridge.settings import Settings


def make(**kw):
    return create_app(Settings(_env_file=None, mc_webhook_token='test',
                               onebot_webhook_token='qq', allowed_group_ids=[123456], **kw))


@pytest.mark.parametrize('event,body', [
    ('chat', {'player':'Steve','message':'hello'}),
    ('death', {'player':'Steve','message':'Steve died'}),
    ('join', {'player':'Steve'}), ('leave', {'player':'Steve'}),
])
def test_each_mc_notification_switch_is_independent(event, body):
    app = make(mc_notifications={e: e != event for e in ['chat','death','join','leave']})
    sent = []
    async def send(group, text):
        sent.append((group, text))
        return True
    app.state.bridge.onebot.send_group_message = send
    with TestClient(app) as c:
        r = c.post('/api/v1/minecraft/events/' + event, json=body,
                   headers={'Authorization':'Bearer test'})
        assert r.status_code == 202
        assert sent == []
        app.state.bridge.settings.mc_notifications[event] = True
        assert c.post('/api/v1/minecraft/events/' + event, json=body,
                      headers={'Authorization':'Bearer test'}).status_code == 202
        assert sent[0][0] == 123456
        assert '[MC]' in sent[0][1]


def test_status_and_join_leave_state_snapshot():
    with TestClient(make()) as c:
        for event, data in [
            ('players', {'players':['Alex','Alex'], 'max_players':20}),
            ('join', {'player':'Steve'}), ('leave', {'player':'Alex'}),
            ('status', {'online':True, 'version':'test'}),
        ]:
            assert c.post('/api/v1/minecraft/events/' + event, json=data,
                          headers={'Authorization':'Bearer test'}).status_code == 202
        assert c.get('/api/v1/state/players').json()['players'] == ['Steve']
        assert c.get('/api/v1/state/status').json()['online'] is True


def test_qq_to_mc_disabled_has_no_callback_even_if_say_allowed():
    app = make(qq_to_mc_enabled=False, mc_say_enabled=True, mc_commands=['say'])
    calls = []
    async def callback(payload):
        calls.append(payload)
        return True
    app.state.bridge.mc_callback.send = callback
    with TestClient(app) as c:
        r = c.post('/api/v1/onebot/events', json=dict(
            post_type='message', message_type='group', group_id=123456,
            user_id=42, message='/mc say hello'), headers={'Authorization':'Bearer qq'})
        assert r.json()['accepted'] is False
        assert calls == []


def test_onebot_hmac_signature_is_checked_over_exact_bytes():
    app = create_app(Settings(_env_file=None, onebot_webhook_secret='test-secret'))
    body = b'{"post_type":"meta_event"}'
    signature = 'sha1=' + hmac.new(b'test-secret', body, hashlib.sha1).hexdigest()
    with TestClient(app) as c:
        assert c.post('/api/v1/onebot/events', content=body).status_code == 401
        assert c.post('/api/v1/onebot/events', content=body,
                      headers={'X-Signature':signature}).status_code == 202
        assert c.post('/api/v1/onebot/events', content=body + b' ',
                      headers={'X-Signature':signature}).status_code == 401


def test_malformed_text_segment_is_rejected():
    with TestClient(make()) as c:
        r = c.post('/api/v1/onebot/events', json=dict(
            post_type='message', message_type='group', group_id=123456,
            user_id=42, message=[{'type':'text','data':None}]),
            headers={'Authorization':'Bearer qq'})
        assert r.status_code == 202
        assert r.json()['accepted'] is False


@pytest.mark.asyncio
async def test_hermes_and_llm_are_never_executed():
    from mcqq_bridge.ports import DisabledHermesAdminExecutor
    from mcqq_bridge.personality import PersonalityProvider
    assert (await DisabledHermesAdminExecutor().execute('whoami', actor_id=42))['accepted'] is False
    assert await PersonalityProvider().reply(text='hi', context={}) is None
    assert PersonalityProvider.enabled is False


def test_openapi_describes_all_event_payloads():
    with TestClient(make()) as c:
        schema = c.get('/openapi.json').json()
        body = schema['paths']['/api/v1/minecraft/events/{event_type}']['post']['requestBody']
        assert len(body['content']['application/json']['schema']['oneOf']) == 6
