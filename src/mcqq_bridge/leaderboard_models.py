"""排行榜 DTO：文本渲染器与（将来的）图片渲染器共用的唯一输入结构。

字段与 ``docs/LEADERBOARD_REQUIREMENTS.md`` §8.1 一一对应；``schema_version`` 从 1
开始，只增不改，供 ``GET /api/v1/leaderboards/*`` 与后续图片渲染器消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

__all__ = [
    "Leaderboard",
    "LeaderboardUnavailable",
    "RankRow",
    "SCHEMA_VERSION",
    "ServerBoard",
    "UnknownServerError",
    "utc_now_text",
]

SCHEMA_VERSION = 1

LeaderboardKind = Literal["deaths", "playtime"]


class UnknownServerError(Exception):
    """参数不是任何已启用服务器的名字（禁用项对外同样不可见）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"未找到服务器：{name}")


class LeaderboardUnavailable(Exception):
    """数据源/数据库整体不可用（不得伪造 0 值）。"""


def utc_now_text(moment: float | None = None) -> str:
    """UTC RFC3339（秒精度），如 ``2026-11-12T08:00:00Z``。"""
    stamp = datetime.fromtimestamp(moment, tz=timezone.utc) if moment is not None else datetime.now(timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RankRow:
    rank: int
    player: str  # 展示名（已按「同服同名」合并）
    value: int  # deaths: 次数；playtime: 秒
    display: str  # 已格式化，如 "82 次" / "20h35m07s"
    online: bool = False  # 仅 playtime 有意义

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "player": self.player,
            "value": self.value,
            "display": self.display,
            "online": self.online,
        }


@dataclass(frozen=True)
class ServerBoard:
    server: str
    rows: list[RankRow] = field(default_factory=list)  # 全量，不截断
    total_players: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "total_players": self.total_players,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass(frozen=True)
class Leaderboard:
    kind: LeaderboardKind
    schema_version: int = SCHEMA_VERSION
    generated_at: str = ""
    servers: list[ServerBoard] = field(default_factory=list)  # 顺序 = config.yaml 顺序

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "servers": [board.to_dict() for board in self.servers],
        }