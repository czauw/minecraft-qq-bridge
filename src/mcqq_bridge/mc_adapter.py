from __future__ import annotations

import logging
from typing import Any

import httpx

from .ports import McCallback
from .settings import Settings

log = logging.getLogger(__name__)


class HttpMcCallback(McCallback):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, payload: dict[str, Any]) -> bool:
        if not self.settings.qq_to_mc_enabled:
            return False
        headers = {}
        if self.settings.mc_callback_token:
            headers["Authorization"] = f"Bearer {self.settings.mc_callback_token}"
        try:
            response = await self.client.post(self.settings.mc_callback_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.status_code in {200, 201, 202, 204}
        except httpx.HTTPError:
            log.warning("Minecraft callback failed; message was not delivered")
            return False
