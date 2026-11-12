from __future__ import annotations

from typing import Any, Protocol


class OutboundOneBot(Protocol):
    async def send_group_message(self, group_id: int, message: str) -> bool: ...


class McCallback(Protocol):
    async def send(self, payload: dict[str, Any]) -> bool: ...


class HermesAdminExecutor(Protocol):
    async def execute(self, prompt: str, *, actor_id: int) -> dict[str, Any]: ...


class DisabledHermesAdminExecutor:
    """安全占位：默认拒绝，不执行进程、shell 或 Hermes 调用。"""

    async def execute(self, prompt: str, *, actor_id: int) -> dict[str, Any]:
        return {"accepted": False, "delivered": False, "reason": "disabled"}

    async def close(self) -> None:
        return None
