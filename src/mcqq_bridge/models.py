from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    player: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=2000)
    server: str | None = Field(default=None, max_length=128)


class DeathEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    player: str = Field(min_length=1, max_length=256)
    message: str | None = Field(default=None, max_length=1000)
    server: str | None = Field(default=None, max_length=128)


class PlayerEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    player: str = Field(min_length=1, max_length=256)
    server: str | None = Field(default=None, max_length=128)


class PlayersEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    players: list[str] = Field(default_factory=list, max_length=1000)
    max_players: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("players")
    @classmethod
    def validate_players(cls, value: list[str]) -> list[str]:
        if any(not p or len(p) > 256 for p in value):
            raise ValueError("player names must be 1-256 characters")
        return value


class StatusEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    online: bool
    version: str | None = Field(default=None, max_length=256)
    motd: str | None = Field(default=None, max_length=1000)
    latency_ms: int | None = Field(default=None, ge=0, le=600000)


class CallbackEvent(BaseModel):
    """Payload posted from QQ to the MC-side webhook consumer."""

    event: str = Field(pattern="^chat$")
    message: str = Field(min_length=1, max_length=2000)
    group_id: int
    user_id: int
    nickname: str | None = Field(default=None, max_length=256)
