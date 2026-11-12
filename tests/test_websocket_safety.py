import asyncio

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from mcqq_bridge.main import create_app
from mcqq_bridge.onebot import OneBotReverseWebSocketHub
from mcqq_bridge.settings import Settings


def settings(**kw):
    return Settings(_env_file=None, allowed_group_ids=[123456], **kw)


def test_websocket_requires_nonempty_token():
    with TestClient(create_app(settings())) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/api/v1/onebot/ws'):
                pass


def test_websocket_rejects_wrong_token():
    with TestClient(create_app(settings(onebot_enabled=True, onebot_access_token='test'))) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect('/api/v1/onebot/ws', headers={'Authorization':'Bearer wrong'}):
                pass


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.headers = {'x-client-role': 'Universal'}

    async def accept(self):
        pass

    async def send_json(self, frame):
        await self.frames.put(frame)

    async def close(self, code=1000):
        pass


@pytest.mark.asyncio
async def test_ws_send_waits_for_matching_successful_api_ack():
    hub = OneBotReverseWebSocketHub(settings(onebot_enabled=True))
    socket = Socket()
    await hub.connect(socket)
    task = asyncio.create_task(hub.send_group_message(123456, 'test'))
    frame = await asyncio.wait_for(socket.frames.get(), 1)
    assert not task.done()
    hub.resolve({'echo': 'wrong', 'status':'ok','retcode':0})
    assert not task.done()
    hub.resolve({'echo': frame['echo'], 'status':'ok','retcode':0})
    assert await asyncio.wait_for(task, 1) is True


@pytest.mark.asyncio
async def test_ws_api_timeout_and_disconnect_do_not_hang():
    hub = OneBotReverseWebSocketHub(settings(onebot_enabled=True, onebot_api_timeout_seconds=0.1))
    socket = Socket()
    await hub.connect(socket)
    assert await hub.send_group_message(123456, 'test') is False
    task = asyncio.create_task(hub.send_group_message(123456, 'test'))
    await asyncio.sleep(0.01)
    await hub.disconnect(socket)
    assert await asyncio.wait_for(task, 1) is False


@pytest.mark.asyncio
async def test_ws_rejects_duplicate_universal_connection():
    hub = OneBotReverseWebSocketHub(settings(onebot_enabled=True))
    assert await hub.connect(Socket()) is True
    assert await hub.connect(Socket()) is False


def test_oversized_body_rejected():
    with TestClient(create_app(settings(onebot_webhook_token='test'))) as c:
        r = c.post('/api/v1/onebot/events', content=b' ' * 70000,
                   headers={'Authorization':'Bearer test'})
        assert r.status_code == 413
