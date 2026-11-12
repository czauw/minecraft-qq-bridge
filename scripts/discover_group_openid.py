#!/usr/bin/env python3
"""一次性小工具：从 QQ 官方网关事件里读出群 OpenID（group_openid）。

官方没有「列出机器人所在群」的接口（`/users/@me/groups` 不存在），
group_openid 只能从事件体里拿。本脚本只做三件事：

1. 用 AppID + ClientSecret 换 access_token；
2. 连官方 WebSocket 网关并 Identify（默认 intents=1<<25 群与 C2C 事件）；
3. 打印收到的群事件里的 group_openid / 群名，拿到第一条后退出。

用法：
    .venv/bin/python scripts/discover_group_openid.py \
        --app-id 你的AppID --env-file .env

    # 或直接给 secret（注意 shell history 泄露风险）
    .venv/bin/python scripts/discover_group_openid.py --app-id 102xxxxx --secret xxxx

拿到后把它填进 config.yaml 的 bot.group_openid（不是 QQ 群号）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import websockets

DEFAULT_API_BASE = "https://api.bot.qq.com"
INTENTS_GROUP_AND_C2C = 1 << 25  # 33554432

GROUP_EVENT_TYPES = {
    "GROUP_ADD_ROBOT",
    "GROUP_DEL_ROBOT",
    "GROUP_MSG_REJECT",
    "GROUP_MSG_RECEIVE",
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
    "C2C_MESSAGE_CREATE",
}


def load_env_file(path: str) -> None:
    """极简 .env 加载：KEY=VALUE / export KEY=VALUE / 成对引号，不覆盖已有环境变量。"""
    file = Path(path)
    if not file.is_file():
        raise SystemExit(f"找不到 env 文件：{path}")
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


async def fetch_token(client: httpx.AsyncClient, api_base: str, app_id: str, secret: str) -> str:
    resp = await client.post(
        f"{api_base}/app/getAppAccessToken",
        json={"appId": app_id, "clientSecret": secret},
    )
    payload = {}
    try:
        payload = resp.json()
    except ValueError:
        pass
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if resp.status_code != 200 or not token:
        # 业务错误在 code 字段，HTTP 往往仍是 200
        code = payload.get("code") if isinstance(payload, dict) else None
        raise SystemExit(f"取 access_token 失败：HTTP {resp.status_code} code={code}（检查 AppID/Secret）")
    return str(token)


async def fetch_gateway(client: httpx.AsyncClient, api_base: str, token: str) -> str:
    resp = await client.get(f"{api_base}/gateway", headers={"Authorization": f"QQBot {token}"})
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    url = payload.get("url") if isinstance(payload, dict) else None
    if resp.status_code != 200 or not url:
        raise SystemExit(f"取网关地址失败：HTTP {resp.status_code}")
    return str(url)


def describe(event_type: str, d: dict) -> str:
    openid = d.get("group_openid") or ""
    bits = [f"事件={event_type}", f"group_openid={openid or '(无)'}"]
    if d.get("group_name"):
        bits.append(f"群名={d['group_name']}")
    author = d.get("author") or {}
    if isinstance(author, dict):
        if author.get("username"):
            bits.append(f"用户={author['username']}")
        if author.get("member_openid"):
            bits.append(f"member_openid={author['member_openid']}")
    if isinstance(d.get("content"), str) and d["content"]:
        bits.append(f"内容={d['content'][:40]}")
    if d.get("op_member_openid"):
        bits.append(f"操作人={d['op_member_openid']}")
    return " | ".join(bits)


async def run(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    app_id = args.app_id or os.environ.get("QQ_BOT_APP_ID", "")
    secret = args.secret or os.environ.get("QQ_BOT_CLIENT_SECRET", "")
    if not app_id or not secret:
        print("缺少 AppID 或 ClientSecret：用 --app-id/--secret 传入，或在 .env 里设 "
              "QQ_BOT_APP_ID / QQ_BOT_CLIENT_SECRET", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=10.0) as client:
        token = await fetch_token(client, api_base, app_id, secret)
        gateway = await fetch_gateway(client, api_base, token)
        print(f"网关：{gateway}")
        print("请在 QQ 群里 @机器人 发任意一条消息（或重新把机器人拉进群）。等待事件中…")

        async with websockets.connect(gateway, ping_interval=None, max_size=2**23) as ws:
            hello = json.loads(await ws.recv())
            interval = float((hello.get("d") or {}).get("heartbeat_interval", 30000)) / 1000.0
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": args.intents,
                    "shard": [0, 1],
                    "properties": {},
                },
            }))

            stop = asyncio.Event()

            async def heartbeat() -> None:
                seq = None
                while not stop.is_set():
                    await asyncio.sleep(interval)
                    try:
                        await ws.send(json.dumps({"op": 1, "d": seq}))
                    except Exception:
                        return

            hb = asyncio.create_task(heartbeat())
            try:
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                    except asyncio.TimeoutError:
                        print(f"\n{args.timeout:.0f} 秒内没收到群事件。确认：\n"
                              "  1) 机器人确实在这个群里（沙箱群需在开放平台配置过）；\n"
                              "  2) 你 @ 的是这个机器人；\n"
                              "  3) intents 含 1<<25（默认已含）。", file=sys.stderr)
                        return 3
                    frame = json.loads(raw)
                    op = frame.get("op")
                    if op == 11:
                        continue
                    if op == 7:  # 服务端要求重连
                        print("服务端要求重连（op=7），请重跑本脚本。", file=sys.stderr)
                        return 4
                    if op == 9:
                        print("会话失效（op=9），请重跑本脚本。", file=sys.stderr)
                        return 4
                    if op != 0:
                        continue
                    event_type = frame.get("t") or ""
                    d = frame.get("d") or {}
                    if event_type in GROUP_EVENT_TYPES or d.get("group_openid"):
                        print("\n" + describe(event_type, d))
                        openid = d.get("group_openid")
                        if openid:
                            print(f"\n✅ 把下面这行填进 config.yaml：\n    group_openid: \"{openid}\"")
                            if openid != args.expect:
                                print("（若这个群不是目标群，请在目标群里 @机器人 后重跑）")
                            return 0
                    else:
                        print(f"（忽略事件 {event_type or '未知'}）")
                return 0
            finally:
                stop.set()
                hb.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(description="从官方网关事件里读取群 OpenID")
    parser.add_argument("--app-id", default="", help="机器人 AppID（也可用 QQ_BOT_APP_ID）")
    parser.add_argument("--secret", default="", help="ClientSecret（建议用 --env-file 传入）")
    parser.add_argument("--env-file", default="", help="加载 KEY=VALUE 文件（如 .env）")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="默认官方 https://api.bot.qq.com")
    parser.add_argument("--intents", type=int, default=INTENTS_GROUP_AND_C2C, help="默认 33554432")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待事件秒数，默认 120")
    parser.add_argument("--expect", default="", help="已知的目标 group_openid（仅用于提示）")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()