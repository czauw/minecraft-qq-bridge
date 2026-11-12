"""QueQiao WebSocket 客户端（Protocol V2，单服务器一连接）。

协议事实来源（只读核实，见 docs/VERIFICATION.md）：
* 握手：``x-self-name`` 必填（对应 QueQiao ``server_name``），可选
  ``Authorization: Bearer <access_token>``（对应 QueQiao ``access_token``）。
* 请求：``{"api": "...", "data": {...}, "echo": "唯一值"}``；
  响应：``{"code": 200, "api": "...", "post_type": "response",
  "status": "SUCCESS", "message": "...", "data": ..., "echo": "..."}``。
* 事件：``post_type`` 为 ``message``/``notice``，``sub_type`` 为
  ``player_chat``/``player_join``/``player_quit``/``player_death``。

**玩家名单**：QueQiao 已发布版本（QueQiao v0.5.0 内置 queqiao-core 0.6.8）
没有返回完整在线名单的 API。官方文档给出的权威做法是 ``send_rcon_command``
执行 ``list``，由 QueQiao 自己执行 RCON 并把命令输出原样返回；本模块据此
实现主查询。``get_status`` 走 Server List Ping 采样，只有当样例条数与
online 人数一致（可证明完整）时才接受，否则报查询失败而不是编造名单。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from websockets.asyncio.client import connect as ws_connect

from .config import ServerConfig
from .events import MCEvent, PlayerSnapshot
from .redact import error_kind, scrub

__all__ = [
    "MCEventCallback",
    "PlayerQueryError",
    "QueQiaoClient",
    "parse_event",
    "parse_player_list_output",
]

MAX_FRAME_BYTES = 1 << 20  # 1 MiB：单帧上限，防坏包/超大包
MAX_PLAYER_NAME = 64
MAX_PLAYERS = 2000
MAX_TEXT = 2000
MAX_EVENT_TASKS = 64
MAX_PLAYER_LOG = 512

MCEventCallback = Callable[[MCEvent], Awaitable[None]]
# 连接状态回调：(服务器名, 是否已连接)。用于排行榜在断线/重连后立刻收敛在线会话。
ConnectionCallback = Callable[[str, bool], None]

logger = logging.getLogger("mcqq_bridge.queqiao")


@dataclass
class _PlayerEntry:
    """名单条目：名字 + 可选的 UUID（UUID 优先用于合并 join/leave）。"""

    name: str
    uuid: str = ""



class PlayerQueryError(Exception):
    """一次玩家名单查询失败（未连接、响应错误、名单不完整等）。"""


# --- RCON list 输出解析 -------------------------------------------------------

_SECTION = re.compile(r"§.")
_EN_LIST = re.compile(
    r"There are\s+(\d+)\s+of\s+a\s+max(?:\s+of)?\s+(\d+)\s+players?\s+online\s*[:：]?(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_ZH_SLASH = re.compile(r"当前有\s*(\d+)\s*/\s*(\d+)\s*名玩家在线\s*[:：]?(.*)$", re.DOTALL)
_ZH_COUNT = re.compile(
    r"当前有\s*(\d+)\s*名玩家在线(?:[，,]\s*最多\s*(\d+)\s*名)?\s*[:：]?(.*)$", re.DOTALL
)
_GENERIC = re.compile(r"(\d+)\s*(?:/|of a max of)\s*(\d+)[^\d:：]*(?:online|在线)?\s*[:：]?(.*)$", re.DOTALL)


def parse_player_list_output(raw: str) -> PlayerSnapshot:
    """解析 QueQiao ``send_rcon_command`` 的 ``list`` 输出。

    支持英文（vanilla/Paper）与常见中文服务端格式；解析不出人数、或人数与
    名单条数不一致时抛 :class:`PlayerQueryError`，绝不返回一个“看起来完整”的
    空名单。
    """
    if not isinstance(raw, str):
        raise PlayerQueryError("list 输出不是字符串")
    text = _SECTION.sub("", raw).strip()
    if not text:
        raise PlayerQueryError("list 输出为空")
    if len(text) > 8192:
        raise PlayerQueryError("list 输出过长")

    match = (
        _EN_LIST.search(text)
        or _ZH_SLASH.search(text)
        or _ZH_COUNT.search(text)
        or _GENERIC.search(text)
    )
    if not match:
        raise PlayerQueryError("无法从 list 输出解析在线人数")

    online = int(match.group(1))
    max_players = int(match.group(2)) if match.group(2) is not None else None
    remainder = (match.group(3) or "").strip().lstrip(":：").strip()
    names: list[str] = []
    if remainder:
        for item in re.split(r"[,，、]", remainder):
            name = item.strip()
            if name:
                names.append(name[:MAX_PLAYER_NAME])

    if online != len(names):
        raise PlayerQueryError(
            f"list 输出显示 {online} 人在线，但只解析到 {len(names)} 个名字，名单不可信"
        )
    if len(names) > MAX_PLAYERS:
        raise PlayerQueryError("list 输出玩家过多")
    return PlayerSnapshot(players=names, max_players=max_players, authoritative=True, source="rcon")


# --- 事件解析 -----------------------------------------------------------------


def _player_of(payload: dict[str, Any]) -> tuple[str, str]:
    player = payload.get("player")
    if not isinstance(player, dict):
        return "", ""
    name = ""
    for key in ("nickname", "player_list_name", "display_name", "name"):
        value = player.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    raw_uuid = player.get("uuid")
    return name[:MAX_PLAYER_NAME], (raw_uuid if isinstance(raw_uuid, str) else "")


def _death_text(payload: dict[str, Any]) -> str:
    death = payload.get("death")
    if isinstance(death, dict):
        text = death.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()[:MAX_TEXT]
        key = death.get("key")
        args = death.get("args")
        if isinstance(key, str) and key:
            readable = key
            if isinstance(args, list) and args:
                readable = f"{key}（参数：{', '.join(str(a) for a in args[:8])}）"
            return readable[:MAX_TEXT]
        return ""
    if isinstance(death, str):
        return death.strip()[:MAX_TEXT]
    return ""


def parse_event(payload: dict[str, Any], server_name: str) -> MCEvent | None:
    """把 QueQiao 事件转成 :class:`MCEvent`；未知/无关事件返回 None。"""
    if not isinstance(payload, dict):
        return None
    if payload.get("post_type") not in ("message", "notice"):
        return None
    sub_type = payload.get("sub_type")
    player, player_uuid = _player_of(payload)
    if sub_type == "player_chat":
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return None
        event_type = "chat"
        text = message.strip()[:MAX_TEXT]
    elif sub_type == "player_join":
        event_type = "join"
        text = ""
    elif sub_type == "player_quit":
        event_type = "leave"
        text = ""
    elif sub_type == "player_death":
        event_type = "death"
        text = _death_text(payload)
    else:
        return None

    if not player:
        # 没有玩家名的 notice 无法归属，直接忽略而不是猜一个名字。
        return None

    message_id = payload.get("message_id")
    timestamp = payload.get("timestamp")
    event_id = ""
    if isinstance(message_id, str) and message_id:
        event_id = message_id
    elif isinstance(timestamp, int):
        event_id = f"{sub_type}:{player_uuid or player}:{timestamp}"
    else:
        event_id = f"{sub_type}:{player_uuid or player}:{text[:64]}"

    return MCEvent(
        server=server_name,  # 归属永远来自连接，不采用负载里的 server_name
        type=event_type,  # type: ignore[arg-type]
        player=player,
        text=text,
        uuid=player_uuid,
        event_id=event_id,
        timestamp=timestamp if isinstance(timestamp, int) else None,
    )


# --- 客户端 -------------------------------------------------------------------


class QueQiaoClient:
    """单个 QueQiao 服务器的独立 WebSocket 客户端。

    每个实例拥有自己的连接、任务、echo future 与玩家快照；任何异常都只影响
    本实例（``manager`` 负责并行与隔离）。
    """

    def __init__(
        self,
        server: ServerConfig,
        on_event: MCEventCallback,
        *,
        connect_factory: Callable[..., Any] | None = None,
        on_connection_change: ConnectionCallback | None = None,
    ) -> None:
        self.server = server
        self._on_event = on_event
        self._connect_factory = connect_factory or ws_connect
        self._on_connection_change = on_connection_change
        self._last_notified_connected = False
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._generation = 0
        self._stopping = False
        self._event_tasks: set[asyncio.Task[None]] = set()
        self.connected = False
        # 玩家名单：以 UUID 优先合并查询快照与 join/leave 增量
        self._entries: list[_PlayerEntry] = []
        self._player_epoch = 0
        self._player_log: deque[tuple[int, str, str, str]] = deque(maxlen=MAX_PLAYER_LOG)
        self._query_lock = asyncio.Lock()
        self.max_players: int | None = None
        self.player_source = ""
        self.last_error = ""
        self.last_sync_ok = False
        self.reconnect_count = 0
        self._log = logging.getLogger(f"mcqq_bridge.queqiao.{server.name}")

    @property
    def players(self) -> list[str]:
        return [entry.name for entry in self._entries]

    @players.setter
    def players(self, value: list[str]) -> None:
        self._entries = [_PlayerEntry(name) for name in value]

    # -- 生命周期 ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"queqiao-{self.server.name}")

    async def close(self) -> None:
        self._stopping = True
        ws = self._ws
        self._ws = None
        self.connected = False
        self._notify_connection(False)
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - 关闭时的容错
                pass
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._fail_pending("客户端已关闭")
        await self._drain_event_tasks()
        self._clear_players("closed")

    def _notify_connection(self, connected: bool) -> None:
        """连接状态变化通知（幂等 + 异常隔离）：只在状态真正翻转时回调一次。"""
        if connected == self._last_notified_connected:
            return
        self._last_notified_connected = connected
        callback = self._on_connection_change
        if callback is None:
            return
        try:
            callback(self.server.name, connected)
        except Exception:  # 回调失败绝不能影响连接本身
            self._log.warning("连接状态回调失败（%s）", "connected" if connected else "disconnected")

    async def _drain_event_tasks(self) -> None:
        tasks = list(self._event_tasks)
        self._event_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- 运行循环 ----------------------------------------------------------

    async def _run(self) -> None:
        backoff = self.server.reconnect_initial_seconds
        while not self._stopping:
            try:
                await self._connect_once()
                # 正常返回说明连接被对端关闭；连续失败会累积退避
                backoff = self.server.reconnect_initial_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 网络/握手错误：只记录受控种类，不写上游原文
                self.last_error = _connect_error(exc)
                self._log.warning("连接/读取失败：%s", self.last_error)
            finally:
                await self._teardown_connection()
            if self._stopping:
                break
            self.reconnect_count += 1
            delay = backoff * random.uniform(0.8, 1.2)
            self._log.info("将在 %.1f 秒后重连（第 %d 次）", delay, self.reconnect_count)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self.server.reconnect_max_seconds)

    async def _connect_once(self) -> None:
        # QueQiao 会对 x-self-name 做 URLDecoder(UTF-8)，因此非 ASCII 服务器名必须
        # 先 percent-encode，否则 HTTP 头按 latin-1 传输会变成乱码并被拒绝。
        headers = {"x-self-name": quote(self.server.handshake_self_name, safe="")}
        if self.server.access_token:
            headers["Authorization"] = f"Bearer {self.server.access_token}"
        uri = self.server.ws_uri
        self._log.debug("连接 %s", uri)
        async with self._connect_factory(
            uri,
            additional_headers=headers,
            open_timeout=self.server.connect_timeout_seconds,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_FRAME_BYTES,
            close_timeout=5,
        ) as websocket:
            self._generation += 1
            self._ws = websocket
            self.connected = True
            self.last_error = ""
            self._log.info("已连接 QueQiao（%s）", uri)
            self._notify_connection(True)
            sync_task = asyncio.create_task(self._sync_players(), name=f"sync-{self.server.name}")
            try:
                async for raw in websocket:
                    await self._handle_frame(raw)
            finally:
                sync_task.cancel()
                await asyncio.gather(sync_task, return_exceptions=True)
                self._log.info("QueQiao 连接已断开")

    async def _teardown_connection(self) -> None:
        self._ws = None
        was_connected = self.connected
        self.connected = False
        self._notify_connection(False)
        # 连接代次 +1：查询在途结果、旧 echo 响应一律作废，防止旧快照/旧响应污染
        self._generation += 1
        self._fail_pending("连接已断开")
        await self._drain_event_tasks()
        self._clear_players("disconnected")
        if was_connected:
            self._log.info("服务器已标记为离线，玩家名单已失效")

    def _clear_players(self, reason: str) -> None:
        self._entries = []
        self.max_players = None
        self.player_source = ""
        self.last_sync_ok = False
        # 断线后旧的增量日志不再可与新快照合并
        self._player_log.clear()
        self._player_epoch += 1
        if reason != "closed":
            self._log.debug("清空玩家名单：%s", reason)

    async def _handle_frame(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            if len(raw) > MAX_FRAME_BYTES:
                self._log.warning("收到超长二进制帧，已忽略")
                return
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._log.warning("收到非 UTF-8 帧，已忽略")
                return
        if not isinstance(raw, str) or len(raw) > MAX_FRAME_BYTES:
            self._log.warning("收到超长帧，已忽略")
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            self._log.warning("收到无法解析的 JSON 帧（%d 字节），已忽略", len(raw))
            return
        if not isinstance(payload, dict):
            self._log.warning("收到非对象 JSON 帧，已忽略")
            return

        post_type = payload.get("post_type")
        if post_type == "response" or ("echo" in payload and "code" in payload):
            echo = payload.get("echo")
            future = self._pending.pop(str(echo), None) if echo is not None else None
            if future is not None and not future.done():
                future.set_result(payload)
            else:
                self._log.debug("收到无主响应（echo=%s）", echo)
            return

        event = parse_event(payload, self.server.name)
        if event is None:
            self._log.debug("忽略未知/无关事件：%s/%s", post_type, payload.get("sub_type"))
            return
        if len(self._event_tasks) >= MAX_EVENT_TASKS:
            self._log.warning("事件处理队列已满，丢弃事件：%s", event.type)
            return
        task = asyncio.create_task(self._dispatch(event), name=f"event-{self.server.name}")
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def _dispatch(self, event: MCEvent) -> None:
        self._apply_incremental(event)
        try:
            await self._on_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单条事件失败不能拖垮连接；只记受控种类
            self._log.warning("事件处理失败（%s）：%s", event.type, error_kind(exc))

    # -- 玩家名单合并 --------------------------------------------------------

    @staticmethod
    def _find_entry(entries: list[_PlayerEntry], uuid_value: str, name: str) -> int | None:
        """UUID 优先：同 UUID 即同一玩家；没有 UUID 时按名字（MC 名字不区分大小写）。"""
        if uuid_value:
            for index, entry in enumerate(entries):
                if entry.uuid == uuid_value:
                    return index
        lowered = name.lower()
        for index, entry in enumerate(entries):
            if entry.name.lower() == lowered:
                return index
        return None

    @classmethod
    def _apply_event_to(cls, entries: list[_PlayerEntry], event: MCEvent) -> None:
        index = cls._find_entry(entries, event.uuid, event.player)
        if event.type == "join":
            if index is None:
                if len(entries) < MAX_PLAYERS:
                    entries.append(_PlayerEntry(event.player, event.uuid))
            elif event.uuid and not entries[index].uuid:
                # 快照只给了名字，事件带来 UUID：补齐，后续按 UUID 合并
                entries[index].uuid = event.uuid
        elif event.type == "leave" and index is not None:
            entries.pop(index)

    def _apply_incremental(self, event: MCEvent) -> None:
        """join/leave 记入增量日志并在已有权威快照上做增量修正。

        * 只有存在权威快照（``player_source``）时才改动内存名单，不凭空虚造；
        * 每条事件都写入有界增量日志，查询返回后按「查询开始时的 epoch」重放，
          因此查询期间发生的 join/leave 不会被旧快照覆盖；
        * UUID 优先合并，同名不同 UUID 不会被误判为同一人。
        """
        if event.type not in ("join", "leave"):
            return
        self._player_epoch += 1
        self._player_log.append((self._player_epoch, event.type, event.player, event.uuid))
        if not self.player_source:
            return
        self._apply_event_to(self._entries, event)

    # -- 请求/响应 ---------------------------------------------------------

    def _fail_pending(self, reason: str) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(PlayerQueryError(reason))

    async def _request(self, api: str, data: dict[str, Any], timeout: float) -> dict[str, Any]:
        websocket = self._ws
        if websocket is None or not self.connected:
            raise PlayerQueryError("尚未连接")
        echo = uuid.uuid4().hex
        generation = self._generation
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            frame = json.dumps({"api": api, "data": data, "echo": echo}, ensure_ascii=False)
            async with self._send_lock:
                await websocket.send(frame)
            payload = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise PlayerQueryError(f"{api} 请求超时（{timeout:.1f}s）") from exc
        except PlayerQueryError:
            raise
        except Exception as exc:
            # 只保留异常种类：底层异常文本可能带 URL/凭据
            raise PlayerQueryError(f"{api} 发送失败：{error_kind(exc)}") from exc
        finally:
            self._pending.pop(echo, None)
            if generation != self._generation and not future.done():  # pragma: no cover
                future.cancel()
        return payload

    @staticmethod
    def _check_response(payload: dict[str, Any], api: str) -> Any:
        code = payload.get("code")
        status = payload.get("status")
        if code != 200 or status != "SUCCESS":
            # 不把上游 message 原文写进异常/日志（可能回显凭据或正文）
            if code == 404:
                raise PlayerQueryError(f"{api} 不被该 QueQiao 版本支持（code=404）")
            raise PlayerQueryError(f"{api} 返回错误（code={code}, status={status}）")
        return payload.get("data")

    # -- 业务操作 ----------------------------------------------------------

    async def broadcast(self, text: str) -> bool:
        """向该服务器广播一条聊天消息（QueQiao 会在前面加自身前缀）。"""
        try:
            payload = await self._request(
                "broadcast",
                {"message": {"text": text}},
                timeout=self.server.players.timeout_seconds,
            )
            self._check_response(payload, "broadcast")
            return True
        except PlayerQueryError as exc:
            self._log.warning("广播失败：%s", scrub(str(exc), 120))
            return False

    async def _sync_players(self) -> None:
        """连接（含重连）后同步已有玩家；失败只记录，不阻塞事件接收。"""
        try:
            snapshot = await self.query_players()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_sync_ok = False
            self._log.warning("连接后玩家名单同步失败：%s", error_kind(exc))
            return
        self.last_sync_ok = True
        self._log.info("连接后玩家名单同步完成：%d 人（来源 %s）", len(snapshot.players), snapshot.source)

    async def query_players(self, *, rcon_only: bool = False) -> PlayerSnapshot:
        """查询完整在线名单；任何不确定都抛 :class:`PlayerQueryError`。

        ``rcon_only=True`` 供排行榜对账使用：强制走 ``send_rcon_command`` 的字面量
        ``list``，**不**回退到 ``get_status``，也不改本地 ``players.mode``；其余
        行为（并发锁、代次校验、增量重放）与默认模式完全一致。

        并发保护：同一连接上的查询串行化（``_query_lock``），连接代次变化
        （断线/重连）后旧响应直接作废，避免旧快照覆盖新状态。
        """
        mode = "rcon" if rcon_only else self.server.players.mode
        timeout = self.server.players.timeout_seconds
        errors: list[str] = []

        async with self._query_lock:
            generation = self._generation
            epoch = self._player_epoch
            snapshot: PlayerSnapshot | None = None

            if mode in ("auto", "rcon"):
                try:
                    snapshot = await self._query_via_rcon(timeout, command="list" if rcon_only else None)
                except PlayerQueryError as exc:
                    errors.append(f"rcon: {exc}")
                    if mode == "rcon":
                        raise

            if snapshot is None:
                try:
                    snapshot = await self._query_via_status(timeout)
                except PlayerQueryError as exc:
                    errors.append(f"status: {exc}")
                    raise PlayerQueryError("；".join(errors)) from None

            if generation != self._generation:
                # 查询期间连接已重建：结果可能来自旧连接，直接丢弃
                raise PlayerQueryError("查询期间连接已重建，本次结果作废")

            return self._apply_snapshot(snapshot, epoch)

    async def _query_via_rcon(self, timeout: float, command: str | None = None) -> PlayerSnapshot:
        payload = await self._request(
            "send_rcon_command", {"command": command or self.server.players.rcon_command}, timeout
        )
        data = self._check_response(payload, "send_rcon_command")
        if not isinstance(data, str):
            raise PlayerQueryError("send_rcon_command 的 data 不是命令输出字符串")
        return parse_player_list_output(data)

    async def _query_via_status(self, timeout: float) -> PlayerSnapshot:
        payload = await self._request("get_status", {}, timeout)
        data = self._check_response(payload, "get_status")
        if not isinstance(data, dict):
            raise PlayerQueryError("get_status 的 data 不是对象")
        ping = data.get("server_list_ping")
        if not isinstance(ping, dict):
            raise PlayerQueryError("get_status 响应缺少 server_list_ping")
        if not ping.get("available"):
            raise PlayerQueryError(f"Server List Ping 不可用（reason={ping.get('reason')}）")
        players_info = ping.get("players")
        if not isinstance(players_info, dict):
            raise PlayerQueryError("Server List Ping 响应缺少 players")
        online = players_info.get("online")
        sample = players_info.get("sample")
        if not isinstance(online, int) or online < 0:
            raise PlayerQueryError("Server List Ping 缺少 online 人数")
        if not isinstance(sample, list):
            raise PlayerQueryError("Server List Ping 缺少 sample 列表")
        names = [
            item["name"][:MAX_PLAYER_NAME]
            for item in sample
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
        ]
        if online != len(names):
            raise PlayerQueryError(
                f"Server List Ping 只返回 {len(names)}/{online} 名玩家采样，无法确认完整名单"
                "（QueQiao 无完整名单 API，请在 QueQiao 开启 rcon 或使用 mode=rcon）"
            )
        max_players = players_info.get("max")
        return PlayerSnapshot(
            players=names,
            max_players=max_players if isinstance(max_players, int) else None,
            authoritative=True,
            source="status",
        )

    def _apply_snapshot(self, snapshot: PlayerSnapshot, epoch: int) -> PlayerSnapshot:
        """把查询快照与「查询开始后发生的 join/leave」合并后落盘。

        快照是在网络上「某个时刻」采到的；查询返回时可能已经有 join/leave 到达。
        这里用查询开始时的 ``epoch`` 重放增量日志，保证旧快照不会把新事件抹掉。
        """
        entries = [_PlayerEntry(name) for name in snapshot.players]
        for log_epoch, kind, name, player_uuid in list(self._player_log):
            if log_epoch <= epoch:
                continue
            self._apply_event_to(
                entries,
                MCEvent(
                    server=self.server.name,
                    type=kind,  # type: ignore[arg-type]
                    player=name,
                    uuid=player_uuid,
                    event_id=f"merge:{log_epoch}",
                ),
            )
        self._entries = entries
        self.max_players = snapshot.max_players
        self.player_source = snapshot.source
        self.last_error = ""
        return PlayerSnapshot(
            players=[entry.name for entry in entries],
            max_players=snapshot.max_players,
            authoritative=snapshot.authoritative,
            source=snapshot.source,
        )

    # -- 状态 --------------------------------------------------------------

    def status_payload(self) -> dict[str, Any]:
        return {
            "name": self.server.name,
            "enabled": self.server.enabled,
            "host": self.server.host,
            "port": self.server.port,
            "connected": self.connected,
            "players": len(self.players),
            "player_source": self.player_source,
            "last_sync_ok": self.last_sync_ok,
            "reconnects": self.reconnect_count,
            "error": self.last_error,
        }


def _connect_error(exc: BaseException) -> str:
    """受控的连接失败描述：只含异常种类与 HTTP 状态码（不写上游原文/URL 凭据）。"""
    kind = error_kind(exc)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"connect_failed:{kind}:http_{status}"
    return f"connect_failed:{kind}"

