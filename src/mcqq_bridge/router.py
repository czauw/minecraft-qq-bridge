"""唯一业务路由：QQ → MC、MC → QQ、/list、去重与回环抑制。

职责边界：本模块不实现 socket / token / 协议编解码，只消费 adapter 给出的
``QQMessage`` / ``MCEvent``，并把文本交给 ``QQBotClient`` 或 ``ServerManager``。
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, cast

from .config import AppConfig, RelayConfig
from .events import MCEvent, QQMessage
from .leaderboard_models import LeaderboardUnavailable, UnknownServerError
from .leaderboard_text import render_leaderboard
from .manager import ServerQueryResult

__all__ = ["BridgeRouter", "format_list", "format_mc_event", "split_message"]


class _TTLCache:
    """有界 + TTL 的去重集合（防止无限占内存）。"""

    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._items: OrderedDict[str, float] = OrderedDict()

    def add(self, key: str) -> bool:
        """返回 True 表示第一次见到（未被去重）。"""
        now = time.monotonic()
        self._prune(now)
        if key in self._items:
            return False
        self._items[key] = now
        while len(self._items) > self._max:
            self._items.popitem(last=False)
        return True

    def contains(self, key: str) -> bool:
        self._prune(time.monotonic())
        return key in self._items

    def discard(self, key: str) -> None:
        self._items.pop(key, None)

    def _prune(self, now: float) -> None:
        while self._items:
            key, created = next(iter(self._items.items()))
            if now - created <= self._ttl:
                break
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)


def _clean(text: str, limit: int) -> str:
    value = " ".join(str(text).split())
    return value[:limit]


# 全量模式（GROUP_MESSAGE_CREATE）下，QQ 会把「@某人」替换成 ``<@member_openid>``
# （@全体为 ``<@all>``）并写在 content 开头；命令匹配前必须剥掉，
# 否则 ``<@abc> /list`` 会以 ``<`` 开头，匹配不上 ``^/list``，被当成普通聊天转发。
_AT_PREFIX = re.compile(r"^\s*<@!?[^<>\s]*>\s*")


def _strip_at_prefix(text: str) -> str:
    """剥掉文本开头连续的 ``<@...>`` 艾特标签（可能有多个 @）。"""
    previous = None
    while previous != text:
        previous = text
        text = _AT_PREFIX.sub("", text, count=1)
    return text


def format_mc_event(event: MCEvent, relay: RelayConfig) -> str | None:
    """MC 事件 → QQ 文本，统一 ``[服务器] [玩家] ： 内容``。"""
    if event.type == "chat":
        content = event.text or ""
    elif event.type == "join":
        content = "进入了服务器"
    elif event.type == "leave":
        content = "离开了服务器"
    elif event.type == "death":
        content = event.text or "死亡"
    else:  # pragma: no cover - 类型已由 DTO 限制
        return None
    player = _clean(event.player, relay.max_nickname_chars) or "未知玩家"
    content = _clean(content, relay.max_mc_message_chars)
    server_name = _clean(event.server, relay.max_nickname_chars)
    return f"[{server_name}] [{player}] ： {content}"


def format_list(results: Iterable[ServerQueryResult]) -> str:
    """按配置顺序格式化 /list 文本；禁用服务器不出现（不查询也不展示）。"""
    blocks: list[str] = []
    for result in results:
        if not result.enabled:
            continue
        if result.status == "online":
            if result.players:
                blocks.append(
                    f"{result.name} {len(result.players)}人在线：\n" + "\n".join(result.players)
                )
            else:
                blocks.append(f"{result.name} 0人在线")
        else:
            blocks.append(f"{result.name} 离线")
    return "\n\n".join(blocks)


def split_message(text: str, max_chars: int, max_parts: int) -> tuple[list[str], bool]:
    """按行切分文本；超过 ``max_parts`` 时截断并标注（标注也计入单片上限）。"""
    max_chars = max(1, int(max_chars))
    max_parts = max(1, int(max_parts))
    chunks: list[str] = []
    for line in text.split("\n"):
        while len(line) > max_chars:
            if chunks and len(chunks[-1]) < max_chars:
                room = max_chars - len(chunks[-1]) - 1
                if room > 0:
                    chunks[-1] = f"{chunks[-1]}\n{line[:room]}"
                    line = line[room:]
                    continue
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if chunks and len(chunks[-1]) + 1 + len(line) <= max_chars:
            chunks[-1] = f"{chunks[-1]}\n{line}" if chunks[-1] else line
        else:
            chunks.append(line)

    chunks = [chunk for chunk in chunks if chunk.strip()]
    truncated = len(chunks) > max_parts
    if truncated:
        note = "\n（列表过长，已截断）"
        chunks = chunks[:max_parts]
        if len(note) >= max_chars:
            # 上限比说明本身还短：只保留能放下的说明，绝不超长
            chunks[-1] = note[:max_chars]
        else:
            budget = max_chars - len(note)
            last = chunks[-1]
            chunks[-1] = (last[:budget] if len(last) > budget else last) + note
    return chunks or [""], truncated


class BridgeRouter:
    def __init__(
        self,
        config: AppConfig,
        manager: Any,
        qq: Any,
        *,
        logger: logging.Logger | None = None,
        leaderboards: Any = None,
    ) -> None:
        self.config = config
        self.manager = manager
        self.qq = qq
        self.leaderboards = leaderboards
        self._log = logger or logging.getLogger("mcqq_bridge.router")
        self._qq_seen = _TTLCache(config.relay.dedupe_ttl_seconds, config.relay.dedupe_max_entries)
        self._mc_seen = _TTLCache(config.relay.dedupe_ttl_seconds, config.relay.dedupe_max_entries)
        self._loop_guard = _TTLCache(config.relay.loop_guard_seconds, max(64, config.relay.dedupe_max_entries // 4))
        # 同一文本可能被并发广播：用引用计数保证「失败清理」不会误删仍在途的登记
        self._guard_refs: dict[str, int] = {}
        self._list_pattern = re.compile(
            r"^" + re.escape(config.relay.list_command) + r"(?:\s+(.*))?$",
            re.IGNORECASE | re.DOTALL,
        )
        # 排行榜命令：只匹配服务器名，参数不做模糊匹配；/deathx、/playtimes 不匹配
        self._leaderboard_pattern = re.compile(
            r"^/(death|playtime)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL
        )

    # -- 回环防护登记 -------------------------------------------------------

    def _guard_add(self, key: str) -> None:
        self._guard_refs[key] = self._guard_refs.get(key, 0) + 1
        self._loop_guard.add(key)

    def _guard_release(self, key: str) -> None:
        """广播失败：撤销本次登记（最后一个所有者撤销时才真正移除指纹）。"""
        count = self._guard_refs.get(key, 0) - 1
        if count > 0:
            self._guard_refs[key] = count
            return
        self._guard_refs.pop(key, None)
        self._loop_guard.discard(key)

    def _guard_seal(self, key: str) -> None:
        """广播成功：所有权转为 TTL 缓存条目，释放引用计数（避免计数表无限增长）。"""
        count = self._guard_refs.get(key, 0) - 1
        if count > 0:
            self._guard_refs[key] = count
        else:
            self._guard_refs.pop(key, None)

    # -- QQ → MC -----------------------------------------------------------

    async def on_qq_message(self, message: QQMessage) -> None:
        if message.group_openid != self.config.bot.group_openid:
            self._log.debug("忽略非目标群消息")
            return
        if not self._qq_seen.add(f"qq:{message.group_openid}:{message.message_id}"):
            self._log.debug("忽略重复 QQ 事件：%s", message.message_id)
            return

        text = message.text.strip()
        if not text:
            return

        # 全量模式会带 ``<@member_openid>`` 前缀：命令匹配与「/」判定都基于剥离后的文本
        command_text = _strip_at_prefix(text).strip()
        command = self._list_pattern.match(command_text)
        if command is not None:
            await self._handle_list(message, (command.group(1) or "").strip())
            return

        board_command = self._leaderboard_pattern.match(command_text)
        if board_command is not None:
            word = board_command.group(1).lower()
            kind = "deaths" if word == "death" else "playtime"
            # 只取首个空白 token 作服务器名；多余参数忽略（需求 §6.5）
            tokens = (board_command.group(2) or "").split()
            argument = tokens[0] if tokens else ""
            await self._handle_leaderboard(message, kind, argument)
            return

        # 所有以 / 开头的内容一律不转发，不管它是不是已实现的指令
        if command_text.startswith("/"):
            self._log.debug("忽略以 / 开头的聊天（不转发到 MC）")
            return

        if not self.config.relay.qq_to_mc_enabled:
            return

        if message.truncated:
            self._log.info("QQ 消息超长已截断后转发")

        payload = f"{_clean(message.nickname, self.config.relay.max_nickname_chars)}：{_clean(command_text, self.config.relay.max_mc_message_chars)}"
        normalized = _normalize(payload)
        # 发送前按「目标服务器」登记回环指纹：服务端可能在返回 ACK 之前就把
        # broadcast 回显成玩家聊天事件，等 ACK 再登记已经太晚。
        targets = self._online_targets()
        guard_keys = {name: f"mc:{name}:{normalized}" for name in targets}
        for key in guard_keys.values():
            self._guard_add(key)
        try:
            results = await self.manager.broadcast(payload)
        except Exception:
            for key in guard_keys.values():
                self._guard_release(key)
            raise
        # 广播失败的服务器：撤销登记；成功的转为 TTL 条目并释放引用计数
        for name, key in guard_keys.items():
            if results.get(name, False):
                self._guard_seal(key)
            else:
                self._guard_release(key)
        if results:
            delivered = sum(1 for ok in results.values() if ok)
            self._log.info("QQ 消息广播到 %d/%d 个在线服务器", delivered, len(results))

    def _online_targets(self) -> list[str]:
        """本次广播会覆盖的服务器名（用于发送前登记回环指纹）。

        优先用 manager 的真实在线目标；没有该能力时退回「所有启用服务器」
        （宁可多登记一点，也不能漏掉回环导致消息自激）。
        """
        getter = cast(
            "Callable[[], list[str]] | None", getattr(self.manager, "online_targets", None)
        )
        if getter is not None:
            try:
                return [str(name) for name in getter()]
            except Exception:  # pragma: no cover - 防御性
                pass
        return [server.name for server in self.config.enabled_servers()]

    async def _handle_list(self, message: QQMessage, argument: str) -> None:
        if argument:
            server = self.config.server(argument)
            # 禁用服务器对 /list 不可见：既不当成有效名称，也不查询
            if server is None or not server.enabled:
                await self.qq.send_text(
                    f"未找到服务器：{_clean(argument, 64)}", reply_to=message.message_id
                )
                return
            results = await self.manager.query([argument])
        else:
            results = await self.manager.query()

        text = format_list(results) or "没有启用的服务器"
        chunks, truncated = split_message(
            text, self.config.bot.max_message_chars, self.config.bot.max_reply_parts
        )
        if truncated:
            self._log.info("/list 响应过长，已按官方被动回复上限截断")
        for chunk in chunks:
            await self.qq.send_text(chunk, reply_to=message.message_id)

    async def _handle_leaderboard(self, message: QQMessage, kind: str, argument: str) -> None:
        """``/death``、``/playtime``：只匹配服务器名，无参输出所有启用服全量。

        全部回复都走被动回复（``reply_to=message_id``、``proactive=False``），
        不消耗主动消息配额；任何失败都只影响本条回复，不影响转发链路。
        """
        if self.leaderboards is None:
            await self.qq.send_text(
                "排行榜数据暂不可用", reply_to=message.message_id, proactive=False
            )
            return
        try:
            board = await self.leaderboards.get_board(kind, argument or None)
        except UnknownServerError:
            await self.qq.send_text(
                f"未找到服务器：{_clean(argument, 64)}",
                reply_to=message.message_id,
                proactive=False,
            )
            return
        except LeaderboardUnavailable:
            await self.qq.send_text(
                "排行榜数据暂不可用", reply_to=message.message_id, proactive=False
            )
            return
        except Exception as exc:  # 统计层异常不得影响 QQ 会话
            self._log.warning("排行榜查询失败：%s", type(exc).__name__)
            await self.qq.send_text(
                "排行榜数据暂不可用", reply_to=message.message_id, proactive=False
            )
            return

        chunks = render_leaderboard(
            board,
            self.config.bot.max_message_chars,
            self.config.bot.max_reply_parts,
            single_server=bool(argument),
        )
        for chunk in chunks:
            await self.qq.send_text(chunk, reply_to=message.message_id, proactive=False)

    # -- MC → QQ -----------------------------------------------------------

    async def on_mc_event(self, event: MCEvent) -> None:
        if not self.config.relay.mc_to_qq.enabled(event.type):
            return
        if not self._mc_seen.add(f"mc:{event.server}:{event.event_id}"):
            self._log.debug("忽略重复 MC 事件：%s/%s", event.server, event.event_id)
            return
        if event.type == "chat" and self._loop_guard.contains(
            f"mc:{event.server}:{_normalize(event.text)}"
        ):
            self._log.info("检测到 QQ→MC→QQ 回环，已抑制（%s）", event.server)
            return

        text = format_mc_event(event, self.config.relay)
        if text is None:
            return
        result = await self.qq.send_text(text, proactive=True)
        if not result.delivered:
            self._log.warning("MC 事件转发 QQ 失败：%s（%s）", event.type, result.reason)


def _normalize(text: str) -> str:
    return " ".join(str(text).split()).lower()
