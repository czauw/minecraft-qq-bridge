from __future__ import annotations

from typing import Annotated, Any, Literal
import json

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "minecraft-qq-bridge"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    mc_webhook_token: str = ""
    mc_webhook_require_token: bool = True
    state_ttl_seconds: float = Field(default=120, ge=1, le=86400)
    mc_notifications: dict[str, bool] = Field(
        default_factory=lambda: {"chat": True, "death": True, "join": True, "leave": True}
    )

    allowed_group_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    onebot_enabled: bool = False
    onebot_transport: Literal["http", "websocket"] = "http"
    onebot_api_url: str = "http://127.0.0.1:3000"
    onebot_access_token: str = ""
    onebot_webhook_token: str = ""
    onebot_webhook_secret: str = ""
    onebot_self_id: int | None = None
    onebot_ws_path: str = "/api/v1/onebot/ws"
    onebot_api_timeout_seconds: float = Field(default=5.0, ge=0.1, le=60)

    qq_to_mc_enabled: bool = True
    mc_callback_url: str = "http://127.0.0.1:25580/bridge/qq"
    mc_callback_token: str = ""
    mc_commands: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["players", "status"])
    mc_say_enabled: bool = False

    # Hermes integration is deliberately opt-in and never inherits profile credentials.
    hermes_admin_enabled: bool = False
    hermes_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_token: str = ""
    hermes_model: str = "hermes-agent"
    hermes_timeout_seconds: float = Field(default=30.0, ge=0.1, le=300)
    hermes_max_concurrency: int = Field(default=1, ge=1, le=16)
    hermes_max_prompt_chars: int = Field(default=2000, ge=1, le=10000)
    hermes_max_response_chars: int = Field(default=6000, ge=1, le=20000)
    llm_enabled: bool = False
    llm_persona: str = ""
    llm_random_reply_probability: float = Field(default=0.0, ge=0, le=1)

    @field_validator("onebot_self_id", mode="before")
    @classmethod
    def empty_self_id(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("mc_commands", mode="before")
    @classmethod
    def normalize_commands(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else value.split(",")
        return [str(item).strip().lower() for item in value]

    @field_validator("allowed_group_ids", "hermes_admin_ids", mode="before")
    @classmethod
    def normalize_groups(cls, value: Any) -> list[int]:
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else [item for item in value.split(",") if item.strip()]
        return [int(item) for item in value]


settings = Settings()
