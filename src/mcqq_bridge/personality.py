from __future__ import annotations

from typing import Any


class PersonalityProvider:
    """Future extension point. No LLM provider is connected in this MVP."""

    enabled = False

    async def reply(self, *, text: str, context: dict[str, Any]) -> str | None:
        return None
