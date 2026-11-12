"""测试公共构造工具（不参与生产代码）。"""

from __future__ import annotations

from typing import Any

from mcqq_bridge.config import AppConfig


def server_entry(name: str, port: int, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "host": "127.0.0.1",
        "port": port,
        "enabled": True,
        "players": {"mode": "auto", "timeout_seconds": 2},
    }
    entry.update(overrides)
    return entry


def bot_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "app_id": "102000000",
        "client_secret": "test-secret",
        "group_openid": "GROUP_OPENID_1",
        "api_base": "http://api.test",
        "receive_mode": "all",
        "send_rate_per_minute": 600,
        "max_message_chars": 400,
        "request_timeout_seconds": 2,
    }
    entry.update(overrides)
    return entry


def make_config(
    servers: list[dict[str, Any]] | None = None,
    bot: dict[str, Any] | None = None,
    relay: dict[str, Any] | None = None,
    logging: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
) -> AppConfig:
    data = {
        "bot": bot_entry(**(bot or {})),
        "servers": servers if servers is not None else [server_entry("主服", 18080)],
        "relay": relay or {},
        "runtime": {"status_http": {"enabled": False}},
        "logging": logging or {"level": "WARNING"},
    }
    if stats is not None:
        data["stats"] = stats
    return AppConfig.model_validate(data)
