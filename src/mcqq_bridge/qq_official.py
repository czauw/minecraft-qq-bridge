"""QQ 官方机器人 adapter（仅官方接口，不使用 OneBot 等非官方登录协议）。

协议事实来源（官方文档，见 docs/VERIFICATION.md）：
* 取 token：``POST {api_base}/app/getAppAccessToken`` body
  ``{"appId": ..., "clientSecret": ...}`` → ``{"access_token", "expires_in"}``；
  失败时 HTTP 仍为 200，业务错误在 ``code``。
* 调用鉴权头：``Authorization: QQBot ***
* 网关：``GET {api_base}/gateway`` → ``{"url": "wss://..."}``；
  连接后先收 ``op=10 Hello``（``heartbeat_interval`` 毫秒），再发
  ``op=2 Identify``（``token="QQBot xxx"``、``intents``、``shard``、
  ``properties``）；心跳 ``op=1``（``d`` 为「已收到的最新 ``s``」，首次为 null），
  服务端回 ``op=11``；断开可 ``op=6 Resume``（``session_id`` + 已处理 ``seq``）；
  ``op=7`` 重连、``op=9`` 会话失效。

  官方关闭码表（<https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html>）::

    关闭码      含义                                     可 Resume  可 Identify
    4001/4002   无效 opcode / payload                    否         否
    4006/4007   会话无效 / seq 错误                      否         是
    4008        发送过快                                 是         是
    4009        连接过期                                 是         是
    4010~4014   无效 shard / 分片过多 / version / intent   否         否
    4900~4913   内部错误                                 否         是
    4914/4915   机器人已下架 / 已封禁                     否         否

  本模块按上表把关闭码映射为「Resume / 重新 Identify / 终止」；传输层断链
  （无关闭帧、1006 等）保留会话尝试 Resume，若网关回 4006/4007 再降级为
  Identify，不再对所有错误一律无休止重连。
* 群事件：``GROUP_AT_MESSAGE_CREATE`` / ``GROUP_MESSAGE_CREATE``，intent
  ``1<<25``；``author.username`` 为昵称，``d.id`` 为消息 ID，``d.id`` 可用于
  被动回复；``msg_elements`` 给出元素顺序与附件。
* 发群消息：``POST /v2/groups/{group_openid}/messages``，``msg_type=0`` 纯文本，
  被动回复带 ``msg_id`` + 递增 ``msg_seq``；被动回复 5 分钟内每条消息最多 5 次，
  主动消息有频控。成功响应必须带消息 id，否则不记为已投递。
* 群内状态：``GET /v2/groups/{group_openid}/bot_state`` → ``member_openid`` /
  ``joined_at`` / ``allow_proactive_msg`` / ``recv_msg_setting`` /
  ``member_role``；``11253``= 仅白名单机器人可用，按 unknown 降级。

**恢复水位**：收到事件只更新「心跳 seq」，只有事件真正处理完成才推进
「Resume 水位」，且必须连续完成；队列满/处理未完成时不推进，由主动 Resume
让网关重投，绝不把未处理事件标成已完成。

安全约定：日志与状态里只出现「受控错误种类 / code / status / 本地上下文」，
不写入上游原始 ``message``、关闭原因、消息正文或任何凭据。
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .config import BotConfig, RelayConfig
from .events import QQMessage
from .redact import error_kind, scrub

__all__ = [
    "BotState",
    "QQApiError",
    "QQBotClient",
    "SendResult",
    "TokenManager",
    "classify_close_code",
    "parse_group_message",
]

MAX_FRAME_BYTES = 1 << 20
MAX_ELEMENTS = 32
MAX_ELEMENT_DEPTH = 3
MAX_SEEN_IDS = 512

# 官方关闭码 → 处理动作（见模块 docstring 的表格）
RESUME_CLOSE_CODES = frozenset({4008, 4009})
IDENTIFY_CLOSE_CODES = frozenset({4006, 4007, *range(4900, 4914)})
FATAL_CLOSE_CODES = frozenset({4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915})
# 传输层断链：没有关闭帧 / 1006 等，官方文档建议短时间内重连可补发事件
TRANSPORT_CLOSE_CODES = frozenset({1000, 1001, 1005, 1006})

# 业务错误码：主动消息权限/配额类。命中后进入有界冷却，避免每条事件都打拒绝请求。
# 该集合只影响「是否暂停主动发送」，不影响被动回复；可按官方新增码扩展。
PERMISSION_OR_QUOTA_CODES = frozenset({304003, 40034100, 40034105})

logger = logging.getLogger("mcqq_bridge.qq")

QQEventCallback = Callable[[QQMessage], Awaitable[None]]


def classify_close_code(code: int | None) -> str:
    """把官方 WebSocket 关闭码映射为 ``resume`` / ``identify`` / ``terminate``。

    * ``4008``/``4009``：会话仍有效，可 Resume；
    * ``4006``/``4007``/``4900~4913``：会话不可用，必须重新 Identify；
    * ``4001/4002/4010~4014/4914/4915``：官方表里 Resume 与 Identify 都不允许，
      继续重连只会无休止打接口，因此终止；
    * 传输层断链（无关闭帧、1000/1001/1005/1006）：保留会话尝试 Resume，
      若网关随后回 4006/4007 会自动降级为 Identify；
    * 其他未知关闭码：按官方「其他错误，请重新发起 identify」处理。
    """
    if code is None or code in TRANSPORT_CLOSE_CODES:
        return "resume"
    if code in FATAL_CLOSE_CODES:
        return "terminate"
    if code in RESUME_CLOSE_CODES:
        return "resume"
    return "identify"


class QQApiError(Exception):
    """QQ 官方接口调用失败（不包含任何凭证内容）。"""


@dataclass
class SendResult:
    accepted: bool
    delivered: bool
    reason: str = ""
    message_id: str | None = None


@dataclass
class BotState:
    """``GET /v2/groups/{group_openid}/bot_state`` 的本地受控快照。

    只保存布尔/枚举值与受控原因，不保存上游原文。
    """

    checked: bool = False
    allow_proactive_msg: bool | None = None
    recv_msg_setting: str = ""
    member_role: str = ""
    reason: str = ""
    checked_at: float = 0.0
    unknown: bool = True

    def as_payload(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "unknown": self.unknown,
            "allow_proactive_msg": self.allow_proactive_msg,
            "recv_msg_setting": self.recv_msg_setting,
            "member_role": self.member_role,
            "reason": self.reason,
            "checked_at": round(self.checked_at, 3),
        }


def _attachment_placeholder(attachment: Any, relay: RelayConfig) -> str:
    if not isinstance(attachment, dict):
        return ""
    content_type = attachment.get("content_type")
    if isinstance(content_type, str) and content_type.lower().startswith("image/"):
        return relay.image_placeholder
    return relay.attachment_placeholder


def _elements_text(elements: list[Any], relay: RelayConfig, depth: int = 0) -> str:
    parts: list[str] = []
    for element in elements[:MAX_ELEMENTS]:
        if not isinstance(element, dict):
            continue
        content = element.get("content")
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
        attachments = element.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments[:MAX_ELEMENTS]:
                placeholder = _attachment_placeholder(attachment, relay)
                if placeholder:
                    parts.append(placeholder)
        nested = element.get("msg_elements")
        if depth + 1 < MAX_ELEMENT_DEPTH and isinstance(nested, list) and nested:
            nested_text = _elements_text(nested, relay, depth + 1)
            if nested_text:
                parts.append(nested_text)
    return " ".join(part for part in parts if part)


def parse_group_message(
    payload: dict[str, Any], relay: RelayConfig, *, max_chars: int = 2000
) -> QQMessage | None:
    """解析群消息事件体（``d``）；非群消息/机器人自身消息返回 None。"""
    if not isinstance(payload, dict):
        return None
    group_openid = payload.get("group_openid")
    message_id = payload.get("id")
    if not isinstance(group_openid, str) or not group_openid:
        return None
    if not isinstance(message_id, str) or not message_id:
        return None

    author = payload.get("author")
    if not isinstance(author, dict):
        author = {}
    if author.get("bot") is True:
        return None
    nickname = author.get("username")
    nickname = nickname.strip() if isinstance(nickname, str) and nickname.strip() else relay.nickname_fallback
    nickname = nickname[: relay.max_nickname_chars]
    member_openid = author.get("member_openid") or author.get("id") or ""
    if not isinstance(member_openid, str):
        member_openid = ""

    elements = payload.get("msg_elements")
    image_count = 0
    attachment_count = 0
    if isinstance(elements, list) and elements:
        text = _elements_text(elements, relay)
        for element in elements[:MAX_ELEMENTS]:
            if not isinstance(element, dict):
                continue
            attachments = element.get("attachments")
            if isinstance(attachments, list):
                for attachment in attachments:
                    if _attachment_placeholder(attachment, relay) == relay.image_placeholder:
                        image_count += 1
                    elif isinstance(attachment, dict):
                        attachment_count += 1
    else:
        content = payload.get("content")
        text = content.strip() if isinstance(content, str) else ""
        attachments = payload.get("attachments")
        if isinstance(attachments, list):
            extras: list[str] = []
            for attachment in attachments[:MAX_ELEMENTS]:
                placeholder = _attachment_placeholder(attachment, relay)
                if placeholder:
                    extras.append(placeholder)
                    if placeholder == relay.image_placeholder:
                        image_count += 1
                    elif isinstance(attachment, dict):
                        attachment_count += 1
            if extras:
                text = f"{text} {' '.join(extras)}".strip() if text else " ".join(extras)

    if not text:
        return None

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return QQMessage(
        group_openid=group_openid,
        message_id=message_id,
        member_openid=member_openid,
        nickname=nickname,
        text=text,
        is_at_bot=payload.get("_at_bot") is True,
        image_count=image_count,
        attachment_count=attachment_count,
        truncated=truncated,
    )


class TokenManager:
    """带并发锁与过期窗口的 access_token 管理器。"""

    def __init__(self, bot: BotConfig, client: httpx.AsyncClient) -> None:
        self._bot = bot
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._log = logging.getLogger("mcqq_bridge.qq.token")

    async def get(self, *, force: bool = False) -> str:
        async with self._lock:
            now = time.monotonic()
            if not force and self._token and now < self._expires_at:
                return self._token
            url = f"{self._bot.api_base}/app/getAppAccessToken"
            try:
                response = await self._client.post(
                    url,
                    json={"appId": self._bot.app_id, "clientSecret": self._bot.client_secret},
                    timeout=self._bot.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise QQApiError(f"获取 access_token 失败：{error_kind(exc)}") from exc

            payload: dict[str, Any] = {}
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
            except ValueError:
                payload = {}

            token = payload.get("access_token")
            if response.status_code != 200 or not isinstance(token, str) or not token:
                code = payload.get("code")
                self._log.warning("获取 access_token 失败：HTTP %s, code=%s", response.status_code, code)
                raise QQApiError(f"获取 access_token 失败（HTTP {response.status_code}, code={code}）")

            try:
                expires_in = float(payload.get("expires_in", 7200))
            except (TypeError, ValueError):
                expires_in = 7200.0
            ttl = max(expires_in - self._bot.token_refresh_skew_seconds, 30.0)
            self._token = token
            self._expires_at = now + ttl
            self._log.info("access_token 已更新，有效期 %.0f 秒（含提前刷新窗口）", ttl)
            return token

    async def invalidate(self, token: str) -> None:
        async with self._lock:
            if self._token == token:
                self._token = None
                self._expires_at = 0.0


class QQBotClient:
    """官方 QQ Bot：网关接收（WS）+ HTTP 主动/被动发送 + 群内状态探测。"""

    def __init__(
        self,
        bot: BotConfig,
        relay: RelayConfig,
        on_event: QQEventCallback,
        *,
        http_client: httpx.AsyncClient | None = None,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.bot = bot
        self.relay = relay
        self._on_event = on_event
        self._connect_factory = connect_factory or ws_connect
        self._http = http_client or httpx.AsyncClient()
        self._owns_http = http_client is None
        self.tokens = TokenManager(bot, self._http)
        self._task: asyncio.Task[None] | None = None
        self._state_task: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._session_id: str | None = None
        # 心跳用「已收到的最新 s」；Resume 用「连续处理完成的水位」
        self._seq_received: int | None = None
        self._resume_seq: int | None = None
        self._seq_heap: list[int] = []
        self._inflight: set[int] = set()
        # 已入队但尚未处理完成的 seq（去重命中时据此判断「副本还在队列里」）
        self._queued_seqs: set[int] = set()
        self._event_queue: asyncio.Queue[tuple[int | None, QQMessage]] = asyncio.Queue(
            maxsize=bot.event_queue_size
        )
        self._event_slots = asyncio.Semaphore(bot.event_workers)
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._ready = False
        self._stopping = False
        self._fatal = False
        self._last_close_code: int | None = None
        self._close_intent: str | None = None
        self._ack_event = asyncio.Event()
        self._msg_seq: OrderedDict[str, int] = OrderedDict()
        self._send_window: deque[float] = deque()
        self._gateway_url: str | None = None
        self._bot_state = BotState()
        self._proactive_failures = 0
        self._cooldown_until = 0.0
        self.overflow_events = 0
        self.last_error = ""
        self.reconnect_count = 0
        self._log = logging.getLogger("mcqq_bridge.qq")

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._fatal = False
        self._ensure_worker()
        self._task = asyncio.create_task(self._run(), name="qq-official")
        if self._state_task is None or self._state_task.done():
            self._state_task = asyncio.create_task(self._bot_state_loop(), name="qq-bot-state")

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain_events(), name="qq-events")

    async def close(self) -> None:
        self._stopping = True
        ws = self._ws
        self._ws = None
        self._ready = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - 关闭容错
                pass
        for task in (self._task, self._state_task, self._worker):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._state_task = None
        self._worker = None
        for task in list(self._close_tasks) + list(self._event_tasks):
            task.cancel()
        pending = list(self._close_tasks) + list(self._event_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._close_tasks.clear()
        self._event_tasks.clear()
        if self._owns_http:
            await self._http.aclose()

    @property
    def connected(self) -> bool:
        return self._ready and self._ws is not None

    @property
    def fatal(self) -> bool:
        """是否因不可重试的官方关闭码而停止（需人工处理 intent/权限/机器人状态）。"""
        return self._fatal

    # -- 网关 --------------------------------------------------------------

    async def _run(self) -> None:
        backoff = self.bot.reconnect_initial_seconds
        while not self._stopping:
            action = "resume"
            try:
                gateway = await self._resolve_gateway()
                await self._connect_once(gateway)
                action = self._close_intent or classify_close_code(self._last_close_code)
                backoff = self.bot.reconnect_initial_seconds
            except asyncio.CancelledError:
                raise
            except QQApiError as exc:
                self.last_error = scrub(str(exc), 120)
                self._log.warning("QQ 网关错误：%s", self.last_error)
                action = "resume"
            except Exception as exc:
                self.last_error = f"gateway_error:{error_kind(exc)}"
                self._log.warning("QQ 网关异常：%s", self.last_error)
                action = "resume"
            finally:
                self._ready = False
                self._ws = None
                self._close_intent = None

            if action == "terminate":
                self._fatal = True
                self.last_error = f"gateway_close_{self._last_close_code}"
                self._log.error(
                    "QQ 网关返回不可重试的关闭码 %s，已停止重连（请检查 intent/权限/机器人状态）",
                    self._last_close_code,
                )
                break
            if self._stopping:
                break
            if action == "identify":
                # 会话已不可 Resume：清空 session/seq，下一轮重新 Identify
                self._reset_session()
            self.reconnect_count += 1
            delay = min(backoff * random.uniform(0.8, 1.2), self.bot.reconnect_max_seconds)
            self._log.info("%.1f 秒后重连 QQ 网关（第 %d 次）", delay, self.reconnect_count)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self.bot.reconnect_max_seconds)

    def _reset_seq_state(self) -> None:
        self._seq_received = None
        self._resume_seq = None
        self._seq_heap.clear()
        self._inflight.clear()

    def _reset_session(self) -> None:
        self._session_id = None
        self._reset_seq_state()

    async def _resolve_gateway(self) -> str:
        if self.bot.gateway_url:
            return self.bot.gateway_url
        if self._gateway_url:
            return self._gateway_url
        url = f"{self.bot.api_base}/gateway"
        refreshed = False
        while True:
            token = await self.tokens.get()
            try:
                response = await self._http.get(
                    url,
                    headers={"Authorization": f"QQBot {token}"},
                    timeout=self.bot.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise QQApiError(f"获取网关地址失败：{error_kind(exc)}") from exc
            if response.status_code == 401 and not refreshed:
                # 鉴权失效：按官方规范作废当前 token 并刷新一次
                refreshed = True
                await self.tokens.invalidate(token)
                self._log.warning("获取网关地址返回 401，已作废本地 token 并重新获取")
                continue
            payload: dict[str, Any] = {}
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    payload = parsed
            except ValueError:
                payload = {}
            gateway = payload.get("url")
            if response.status_code != 200 or not isinstance(gateway, str) or not gateway:
                raise QQApiError(
                    f"获取网关地址失败（HTTP {response.status_code}, code={payload.get('code')}）"
                )
            self._gateway_url = gateway
            return gateway

    async def _connect_once(self, gateway: str) -> None:
        self._last_close_code = None
        self._close_intent = None
        async with self._connect_factory(
            gateway,
            open_timeout=self.bot.request_timeout_seconds,
            ping_interval=None,  # 使用官方 op=1 心跳
            max_size=MAX_FRAME_BYTES,
            close_timeout=5,
        ) as websocket:
            self._ws = websocket
            hello = await asyncio.wait_for(websocket.recv(), timeout=self.bot.request_timeout_seconds)
            try:
                hello_payload = json.loads(hello)
            except (TypeError, ValueError):
                raise QQApiError("网关 Hello 不是合法 JSON")
            if not isinstance(hello_payload, dict) or hello_payload.get("op") != 10:
                raise QQApiError("网关未发送 op=10 Hello")
            data = hello_payload.get("d")
            interval_ms = data.get("heartbeat_interval") if isinstance(data, dict) else None
            if not isinstance(interval_ms, (int, float)) or interval_ms <= 0:
                raise QQApiError("Hello 缺少合法的 heartbeat_interval")
            await self._send_identify(websocket)
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(websocket, float(interval_ms) / 1000.0), name="qq-heartbeat"
            )
            try:
                await self._read_loop(websocket)
            except ConnectionClosed as exc:
                # 不读取 ConnectionClosed.code（13.1 起废弃且会告警），改用 rcvd/sent
                closing = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
                self._last_close_code = getattr(closing, "code", None) if closing is not None else None
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _send_identify(self, websocket: Any) -> None:
        token = await self.tokens.get()
        if self._session_id and self._resume_seq is not None:
            payload = {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._resume_seq,
                },
            }
            self._log.info("尝试恢复 QQ 会话（水位数=%s）", self._resume_seq)
        elif self._session_id:
            # 会话存在但尚无已处理事件：seq=0 表示「未确认任何事件」
            payload = {
                "op": 6,
                "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": 0},
            }
            self._log.info("尝试恢复 QQ 会话（尚无可确认事件）")
        else:
            payload = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": self.bot.intents,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "mcqq_bridge", "$device": "mcqq_bridge"},
                },
            }
            self._log.info("发送 Identify 订阅事件（intents=%s）", self.bot.intents)
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def _heartbeat_loop(self, websocket: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._ack_event.clear()
            try:
                async with self._send_lock:
                    await websocket.send(json.dumps({"op": 1, "d": self._seq_received}))
            except Exception:
                return
            try:
                await asyncio.wait_for(
                    self._ack_event.wait(), timeout=self.bot.heartbeat_ack_timeout_seconds
                )
            except asyncio.TimeoutError:
                self._log.warning("心跳未收到 ACK，主动重连（保留会话以便 Resume）")
                self._request_close("resume")
                return

    def _request_close(self, intent: str) -> None:
        """由本端发起关闭：记录意图，避免被当成「未知关闭码」而清空会话。"""
        self._close_intent = intent
        ws = self._ws
        if ws is None:
            return
        task = asyncio.create_task(self._safe_close(ws), name="qq-close")
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    @staticmethod
    async def _safe_close(websocket: Any) -> None:
        try:
            await websocket.close(code=1000, reason="client reconnect")
        except Exception:  # pragma: no cover - 关闭容错
            pass

    async def _read_loop(self, websocket: Any) -> None:
        async for raw in websocket:
            if not isinstance(raw, str) or len(raw) > MAX_FRAME_BYTES:
                self._log.warning("忽略超长/非文本网关帧")
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                self._log.warning("忽略无法解析的网关帧")
                continue
            if not isinstance(payload, dict):
                continue
            op = payload.get("op")
            if op == 0:
                await self._dispatch_dispatch(payload)
            elif op == 11:
                self._ack_event.set()
            elif op == 1:
                try:
                    async with self._send_lock:
                        await websocket.send(json.dumps({"op": 1, "d": self._seq_received}))
                except Exception:
                    return
            elif op == 9:
                self._log.warning("QQ 网关要求重新鉴权（Invalid Session），将重新 Identify")
                self._reset_session()
                self._request_close("identify")
                return
            elif op == 7:
                self._log.info("QQ 网关要求重连（保留会话以便 Resume）")
                self._request_close("resume")
                return
            else:
                self._log.debug("忽略未知 op=%s", op)

    # -- 事件队列与恢复水位 --------------------------------------------------

    def _track_received(self, seq: int | None) -> None:
        if seq is None:
            return
        self._seq_received = seq
        if seq not in self._inflight:
            self._inflight.add(seq)
            heapq.heappush(self._seq_heap, seq)

    def _track_completed(self, seq: int | None) -> None:
        """事件真正处理完成后才推进 Resume 水位；未完成的事件会一直挡住水位。"""
        if seq is None:
            return
        self._inflight.discard(seq)
        while self._seq_heap and self._seq_heap[0] not in self._inflight:
            value = heapq.heappop(self._seq_heap)
            if self._resume_seq is None or value > self._resume_seq:
                self._resume_seq = value

    async def _drain_events(self) -> None:
        while True:
            await self._event_slots.acquire()
            try:
                seq, message = await self._event_queue.get()
            except asyncio.CancelledError:
                self._event_slots.release()
                raise
            task = asyncio.create_task(self._dispatch_message(seq, message), name="qq-event")
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
            task.add_done_callback(lambda _task: self._event_slots.release())

    async def _dispatch_message(self, seq: int | None, message: QQMessage) -> None:
        try:
            await self._on_event(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning("QQ 消息处理失败：%s", error_kind(exc))
        finally:
            if seq is not None:
                self._queued_seqs.discard(seq)
            self._track_completed(seq)

    def _remember_id(self, message_id: str) -> None:
        """标记「已进入可重投处理路径」。只允许在事件成功入队后调用。"""
        self._seen_ids[message_id] = None
        while len(self._seen_ids) > MAX_SEEN_IDS:
            self._seen_ids.popitem(last=False)

    async def _dispatch_dispatch(self, payload: dict[str, Any]) -> None:
        self._ensure_worker()
        event_type = payload.get("t")
        data = payload.get("d")

        if event_type == "READY":
            # 新会话：旧的 seq 水位不再可 Resume，但 session_id 要记下来供 Resume 用
            self._reset_seq_state()
            if isinstance(data, dict):
                session_id = data.get("session_id")
                if isinstance(session_id, str):
                    self._session_id = session_id
            self._ready = True
            self.last_error = ""
            self._log.info("QQ 机器人已就绪（会话已建立）")
            return
        if event_type == "RESUMED":
            self._ready = True
            self._log.info("QQ 会话已恢复")
            return
        if event_type not in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            return

        seq = payload.get("s")
        seq = seq if isinstance(seq, int) else None
        self._track_received(seq)

        if self.bot.receive_mode == "mention" and event_type != "GROUP_AT_MESSAGE_CREATE":
            # mention 模式只消费 @机器人 事件，全量群消息一律不处理
            self._log.debug("mention 模式忽略非 @ 群消息")
            self._track_completed(seq)
            return
        if not isinstance(data, dict):
            self._track_completed(seq)
            return
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            data = {**data, "_at_bot": True}
        message = parse_group_message(data, self.relay, max_chars=self.bot.max_message_chars)
        if message is None:
            self._track_completed(seq)
            return
        key = f"{message.group_openid}:{message.message_id}"
        if key in self._seen_ids:
            # 全量 + @ 双投、Resume 重投都会走到这里：统一按 message_id 去重。
            # 若该 seq 的副本仍在队列中等待处理，水位由那个副本负责推进，不能在这里提前完成。
            self._log.debug("忽略重复 QQ 事件（已去重）")
            if seq is None or seq not in self._queued_seqs:
                self._track_completed(seq)
            return
        try:
            self._event_queue.put_nowait((seq, message))
        except asyncio.QueueFull:
            # 队列满：事件保持「未完成」且**不记入去重表**，水位不推进，
            # 主动 Resume 让网关重投同一事件时仍能重新入队（否则重投会被去重永久丢弃）。
            self.overflow_events += 1
            if self._close_intent is None:
                self._log.warning(
                    "QQ 事件队列已满（上限 %d），事件保持可重投且不推进恢复水位，主动 Resume 由网关重投",
                    self.bot.event_queue_size,
                )
                self._request_close("resume")
            return
        # 只有真正进入可重投处理路径（已入队）后才记已见
        self._remember_id(key)
        if seq is not None:
            self._queued_seqs.add(seq)

    # -- 发送 --------------------------------------------------------------

    def _next_seq(self, message_id: str) -> int:
        seq = self._msg_seq.get(message_id, 0) + 1
        self._msg_seq[message_id] = seq
        self._msg_seq.move_to_end(message_id)
        while len(self._msg_seq) > 64:
            self._msg_seq.popitem(last=False)
        return seq

    def _rate_allows(self) -> bool:
        now = time.monotonic()
        while self._send_window and now - self._send_window[0] > 60.0:
            self._send_window.popleft()
        if len(self._send_window) >= self.bot.send_rate_per_minute:
            return False
        self._send_window.append(now)
        return True

    def _proactive_block_reason(self) -> str:
        """主动消息的本地闸门：配置关闭 / 冷却中 / 官方明确不允许。"""
        if not self.bot.proactive_enabled:
            return "proactive_disabled"
        if time.monotonic() < self._cooldown_until:
            return "proactive_cooldown"
        if self._bot_state.allow_proactive_msg is False:
            return "proactive_not_allowed"
        return ""

    def _note_send_failure(self, status_code: int, code: Any) -> None:
        if status_code == 429 or code in PERMISSION_OR_QUOTA_CODES:
            self._proactive_failures += 1
            delay = min(
                self.bot.proactive_cooldown_initial_seconds * (2 ** (self._proactive_failures - 1)),
                self.bot.proactive_cooldown_max_seconds,
            )
            self._cooldown_until = time.monotonic() + delay
            self._log.warning(
                "主动消息被平台拒绝（http=%s code=%s），进入 %.0f 秒冷却（第 %d 次）",
                status_code,
                code,
                delay,
                self._proactive_failures,
            )

    def _note_send_success(self) -> None:
        self._proactive_failures = 0
        self._cooldown_until = 0.0

    async def send_text(
        self, text: str, *, reply_to: str | None = None, proactive: bool = False
    ) -> SendResult:
        """发送群文本消息。``delivered`` 仅在官方 API 业务成功且响应结构合法时为 True。"""
        if not text or not text.strip():
            return SendResult(accepted=False, delivered=False, reason="empty_message")
        if proactive:
            blocked = self._proactive_block_reason()
            if blocked:
                return SendResult(accepted=False, delivered=False, reason=blocked)
        if not self._rate_allows():
            self._log.warning("已触发本地发送频率上限（%d 条/分钟），本次跳过", self.bot.send_rate_per_minute)
            return SendResult(accepted=False, delivered=False, reason="rate_limited")

        payload: dict[str, Any] = {"content": text, "msg_type": 0}
        if reply_to:
            payload["msg_id"] = reply_to
            payload["msg_seq"] = self._next_seq(reply_to)

        url = f"{self.bot.api_base}/v2/groups/{quote(self.bot.group_openid, safe='')}/messages"
        refreshed = False
        retried_429 = False
        while True:
            try:
                token = await self.tokens.get()
            except QQApiError as exc:
                return SendResult(accepted=False, delivered=False, reason=scrub(str(exc), 120))
            try:
                response = await self._http.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"QQBot {token}",
                        "X-Union-Appid": self.bot.app_id,
                        "Content-Type": "application/json",
                    },
                    timeout=self.bot.request_timeout_seconds,
                )
            except httpx.HTTPError as exc:
                return SendResult(
                    accepted=False, delivered=False, reason=f"network:{error_kind(exc)}"
                )

            body: dict[str, Any] = {}
            parsed_ok = True
            try:
                parsed = response.json()
            except ValueError:
                parsed_ok = False
                parsed = None
            if isinstance(parsed, dict):
                body = parsed
            else:
                parsed_ok = False

            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self.tokens.invalidate(token)
                self._log.warning("群消息发送返回 401，已作废本地 token 并重试一次")
                continue
            if response.status_code == 429 and not retried_429:
                retried_429 = True
                retry_after = _retry_after(response, body)
                self._log.warning("QQ 接口限流（429），%.1f 秒后重试一次", retry_after)
                await asyncio.sleep(retry_after)
                continue

            code = body.get("code")
            if response.status_code == 200 and (code in (None, 0)):
                message_id = _extract_message_id(body) if parsed_ok else None
                if message_id is None:
                    # HTTP 200 但没有合法成功结构（坏 JSON / 数组 / 空 body / 缺 id）
                    self._log.warning("QQ 群消息响应不是合法成功结构（缺少消息 id），不记为已投递")
                    self._note_send_failure(response.status_code, code)
                    return SendResult(accepted=False, delivered=False, reason="invalid_response")
                self._note_send_success()
                return SendResult(accepted=True, delivered=True, message_id=message_id)

            reason = f"http_{response.status_code}"
            if code is not None:
                reason = f"{reason}_code_{code}"
            # 绝不打印上游原文 message，只保留 code/status
            self._log.warning("QQ 群消息发送失败：%s（上游 message 已省略）", reason)
            self._note_send_failure(response.status_code, code)
            return SendResult(accepted=False, delivered=False, reason=reason)

    # -- 群内状态（bot_state） ----------------------------------------------

    async def refresh_bot_state(self) -> BotState:
        """探测 ``GET /v2/groups/{group_openid}/bot_state``。

        失败/无权限一律按 ``unknown`` 降级（不谎称已确认），只记录受控原因。
        """
        if not self.bot.bot_state_probe_enabled:
            self._bot_state = BotState(checked=False, unknown=True, reason="probe_disabled")
            return self._bot_state
        url = f"{self.bot.api_base}/v2/groups/{quote(self.bot.group_openid, safe='')}/bot_state"
        refreshed = False
        while True:
            try:
                token = await self.tokens.get()
                response = await self._http.get(
                    url,
                    headers={"Authorization": f"QQBot {token}", "X-Union-Appid": self.bot.app_id},
                    timeout=self.bot.request_timeout_seconds,
                )
            except QQApiError as exc:
                self._bot_state = BotState(checked=False, reason=scrub(str(exc), 80))
                return self._bot_state
            except httpx.HTTPError as exc:
                self._bot_state = BotState(checked=False, reason=f"network:{error_kind(exc)}")
                return self._bot_state

            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self.tokens.invalidate(token)
                continue

            try:
                parsed = response.json()
            except ValueError:
                parsed = None
            body = parsed if isinstance(parsed, dict) else {}
            code = body.get("code")
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            if response.status_code == 200 and code in (None, 0) and isinstance(data, dict):
                allow = data.get("allow_proactive_msg")
                setting = data.get("recv_msg_setting")
                role = data.get("member_role")
                known = isinstance(allow, bool)
                self._bot_state = BotState(
                    checked=True,
                    unknown=not known,
                    allow_proactive_msg=allow if known else None,
                    recv_msg_setting=setting if isinstance(setting, str) else "",
                    member_role=role if isinstance(role, str) else "",
                    reason="" if known else "missing_allow_proactive_msg",
                    checked_at=time.monotonic(),
                )
                if self._bot_state.allow_proactive_msg is True:
                    # 官方确认可主动推送：解除本地冷却
                    self._proactive_failures = 0
                    self._cooldown_until = 0.0
                if known:
                    self._log.info(
                        "群内状态已确认：allow_proactive_msg=%s recv_msg_setting=%s",
                        self._bot_state.allow_proactive_msg,
                        self._bot_state.recv_msg_setting or "-",
                    )
                else:
                    self._log.warning("群内状态缺少 allow_proactive_msg，按 unknown 降级")
                return self._bot_state

            reason = "whitelist_only" if code == 11253 else f"http_{response.status_code}_code_{code}"
            self._bot_state = BotState(checked=False, reason=reason, checked_at=time.monotonic())
            self._log.warning("群内状态探测不可用（%s），主动消息按 unknown 处理", reason)
            return self._bot_state

    async def _bot_state_loop(self) -> None:
        while True:
            try:
                await self.refresh_bot_state()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 探测失败绝不能拖垮事件接收
                self._log.warning("群内状态探测异常：%s", error_kind(exc))
            interval = self.bot.bot_state_interval_seconds
            if interval <= 0:
                return
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    # -- 状态 --------------------------------------------------------------

    def degraded_reasons(self) -> list[str]:
        """当前无法确认/不满足的运行时前提（供 readyz 与 status 暴露）。"""
        reasons: list[str] = []
        allow = self._bot_state.allow_proactive_msg
        if self.bot.proactive_enabled:
            if allow is False:
                reasons.append("proactive_not_allowed")
            elif allow is None:
                reasons.append("proactive_state_unknown")
        if time.monotonic() < self._cooldown_until:
            reasons.append("proactive_cooldown")
        setting = self._bot_state.recv_msg_setting
        if setting and setting != "all":
            reasons.append(f"recv_msg_setting:{setting}")
        elif not setting and self.bot.receive_mode == "all":
            # bot_state 不可用时无法证明「全量监听」已生效
            reasons.append("recv_msg_setting_unknown")
        return reasons

    def status_payload(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "receive_mode": self.bot.receive_mode,
            "proactive_enabled": self.bot.proactive_enabled,
            "reconnects": self.reconnect_count,
            "fatal": self._fatal,
            "error": self.last_error,
            "queue_depth": self._event_queue.qsize(),
            "queue_capacity": self.bot.event_queue_size,
            "queue_overflow": self.overflow_events,
            "event_workers": self.bot.event_workers,
            "resume_seq": self._resume_seq,
            "seen_seq": self._seq_received,
            "bot_state": self._bot_state.as_payload(),
            "degraded": self.degraded_reasons(),
        }


def _extract_message_id(body: dict[str, Any]) -> str | None:
    data = body.get("data")
    if isinstance(data, dict):
        value = data.get("id")
        if isinstance(value, str) and value:
            return value
    value = body.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def _retry_after(response: httpx.Response, body: dict[str, Any]) -> float:
    value: Any = None
    header = response.headers.get("Retry-After")
    if header:
        try:
            value = float(header)
        except ValueError:
            value = None
    if value is None and isinstance(body.get("retry_after"), (int, float)):
        value = float(body["retry_after"])
    if value is None:
        value = 1.0
    return max(0.0, min(float(value), 5.0))
