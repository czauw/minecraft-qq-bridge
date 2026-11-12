from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class BridgeState:
    players: list[str] = field(default_factory=list)
    max_players: int | None = None
    status: dict[str, Any] = field(default_factory=lambda: {"online": None})
    ttl_seconds: float = 120
    players_monotonic: float | None = None
    status_monotonic: float | None = None
    players_updated_at: str | None = None
    status_updated_at: str | None = None

    def update_players(self, players: list[str], max_players: int | None = None) -> None:
        self.players = list(dict.fromkeys(players))[:1000]
        self.max_players = max_players
        self.players_monotonic = time.monotonic()
        self.players_updated_at = datetime.now(timezone.utc).isoformat()

    def update_status(self, payload: dict[str, Any]) -> None:
        self.status = dict(payload)
        self.status_monotonic = time.monotonic()
        self.status_updated_at = datetime.now(timezone.utc).isoformat()
        if payload.get("online") is False:
            self.update_players([], self.max_players)

    def apply_join(self, player: str) -> None:
        if player not in self.players and len(self.players) < 1000:
            self.players.append(player)
        # Incremental events cannot prove a complete snapshot is fresh.

    def apply_leave(self, player: str) -> None:
        if player in self.players:
            self.players.remove(player)

    def freshness(self, kind: str) -> str:
        updated = getattr(self, f"{kind}_monotonic")
        if updated is None:
            return "unknown"
        return "stale" if time.monotonic() - updated > self.ttl_seconds else "fresh"

    def metadata_payload(self) -> dict[str, Any]:
        return {
            "ttl_seconds": self.ttl_seconds,
            **{kind: {"freshness": self.freshness(kind),
                      "updated_at": getattr(self, f"{kind}_updated_at")}
               for kind in ("players", "status")},
        }

    def players_payload(self) -> dict[str, Any]:
        return {"players": self.players, "count": len(self.players), "max_players": self.max_players}

    def status_payload(self) -> dict[str, Any]:
        return {**self.status, "players": self.players, "player_count": len(self.players)}
