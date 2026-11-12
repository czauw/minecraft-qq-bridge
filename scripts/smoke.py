"""Real TCP smoke test with explicitly simulated OneBot/MC peers; no credentials.

Run with: .venv/bin/python scripts/smoke.py
This is protocol verification, NOT verification against a real QQ/MC account.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx


class Peer(BaseHTTPRequestHandler):
    messages = []
    callbacks = []

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/send_group_msg':
            assert self.headers.get('Authorization') == 'Bearer smoke-api'
            assert data['group_id'] == 123456
            assert data['auto_escape'] is True
            self.messages.append(data)
            body = b'{"status":"ok","retcode":0,"data":{"message_id":1}}'
        elif self.path == '/callback':
            assert self.headers.get('Authorization') == 'Bearer smoke-callback'
            self.callbacks.append(data)
            body = b'{}'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


def main():
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.close()
    env = {**os.environ,
           'MC_WEBHOOK_TOKEN':'smoke-mc', 'MC_WEBHOOK_REQUIRE_TOKEN':'true',
           'ALLOWED_GROUP_IDS':'123456', 'ONEBOT_ENABLED':'true',
           'ONEBOT_TRANSPORT':'http', 'ONEBOT_ACCESS_TOKEN':'smoke-api',
           'ONEBOT_WEBHOOK_TOKEN':'smoke-events', 'ONEBOT_WEBHOOK_SECRET':'',
           'ONEBOT_API_URL':f'http://127.0.0.1:{peer.server_port}',
           'QQ_TO_MC_ENABLED':'true', 'MC_SAY_ENABLED':'true',
           'MC_COMMANDS':'players,status,say',
           'MC_CALLBACK_URL':f'http://127.0.0.1:{peer.server_port}/callback',
           'MC_CALLBACK_TOKEN':'smoke-callback'}
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'mcqq_bridge.main:app',
                                '--host', '127.0.0.1', '--port', str(port),
                                '--no-access-log', '--ws-max-size', '65536'], env=env)
    base = f'http://127.0.0.1:{port}'
    try:
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('uvicorn exited before ready')
                try:
                    if client.get('/healthz').status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('health check timed out')
            assert client.get('/readyz').status_code == 200
            assert client.get('/docs').status_code == 200
            assert client.get('/openapi.json').status_code == 200
            assert client.post('/api/v1/minecraft/events/chat', json={}).status_code == 401
            for kind, body in [
                ('chat', {'player':'Steve','message':'[CQ:at,qq=all] hello'}),
                ('death', {'player':'Steve','message':'Steve died'}),
                ('join', {'player':'Alex'}), ('leave', {'player':'Alex'}),
                ('players', {'players':['Steve'], 'max_players':20}),
                ('status', {'online':True,'version':'test-version'}),
            ]:
                response = client.post('/api/v1/minecraft/events/' + kind, json=body,
                                       headers={'Authorization':'Bearer smoke-mc'})
                assert response.status_code == 202, response.text
            assert len(Peer.messages) == 4
            assert client.get('/api/v1/state/players').json()['players'] == ['Steve']
            for command in ['players', 'status', 'say hello from QQ']:
                r = client.post('/api/v1/onebot/events', json=dict(
                    post_type='message', message_type='group', self_id=100,
                    user_id=42, group_id=123456, message='/mc ' + command),
                    headers={'Authorization':'Bearer smoke-events'})
                assert r.status_code == 202 and r.json()['accepted'] and r.json()['delivered'], r.text
            assert len(Peer.messages) == 6
            assert len(Peer.callbacks) == 1
            assert Peer.callbacks[0]['event'] == 'chat'
            assert Peer.callbacks[0]['message'] == 'hello from QQ'
            print('SMOKE PASS: TCP health/docs, six MC events, 6 OneBot HTTP sends, 1 MC callback; simulated peers only')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        peer.shutdown()
        peer.server_close()


if __name__ == '__main__':
    main()
