"""协议无关的事件 DTO。

QQ 官方与 QueQiao 的原始 JSON 只在本模块（以及各自的 adapter）内解析，
路由层只处理这些结构，避免把某一侧的整数字段/私有字段泄漏到另一侧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = ["MCEvent", "MCEventType", "PlayerSnapshot", "QQMessage"]

MCEventType = Literal["chat", "join", "leave", "death"]


@dataclass(frozen=True)
class QQMessage:
    """目标群里的一条普通 QQ 消息（已归一化为可广播文本）。"""

    group_openid: str
    message_id: str
    member_openid: str
    nickname: str
    text: str
    is_at_bot: bool = False
    image_count: int = 0
    attachment_count: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class MCEvent:
    """来自某个 QueQiao 连接的一条 MC 事件。``server`` 始终来自连接归属。"""

    server: str
    type: MCEventType
    player: str
    text: str = ""
    uuid: str = ""
    event_id: str = ""
    timestamp: int | None = None


@dataclass
class PlayerSnapshot:
    """一次玩家名单查询的结果。``authoritative`` 为 True 才代表完整名单。"""

    players: list[str] = field(default_factory=list)
    max_players: int | None = None
    authoritative: bool = False
    source: str = ""

    @property
    def count(self) -> int:
        return len(self.players)
