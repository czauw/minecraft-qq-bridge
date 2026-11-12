from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

from .ports import HermesAdminExecutor
from .settings import Settings

log = logging.getLogger(__name__)


class DisabledHermesExecutor:
    """Fail-closed implementation used unless the explicit feature flag is on."""

    async def execute(self, prompt: str, *, actor_id: int) -> dict[str, Any]:
        return {"accepted": False, "delivered": False, "reason": "disabled"}

    async def close(self) -> None:
        return None


class HermesApiExecutor:
    """Call an already-running Hermes API server over its documented HTTP API.

    This adapter never launches a process, reads a Hermes profile, or receives a
    Hermes credential from the container. The token and URL are explicit bridge
    settings supplied by the operator through the environment.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=settings.hermes_timeout_seconds)
        self._in_flight = 0
        self._admission_lock = asyncio.Lock()
        parsed = urlparse(settings.hermes_api_url)
        self._valid_url = parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def close(self) -> None:
        await self.client.aclose()

    async def execute(self, prompt: str, *, actor_id: int) -> dict[str, Any]:
        if not self.settings.hermes_admin_enabled:
            return {"accepted": False, "delivered": False, "reason": "disabled"}
        if actor_id not in self.settings.hermes_admin_ids:
            return {"accepted": False, "delivered": False, "reason": "unauthorized"}
        if not self.settings.hermes_api_token or not self._valid_url:
            return {"accepted": False, "delivered": False, "reason": "not_configured"}
        if len(prompt) > self.settings.hermes_max_prompt_chars:
            return {"accepted": False, "delivered": False, "reason": "prompt_too_long"}

        payload = {
            "model": self.settings.hermes_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.settings.hermes_api_token}"}
        async with self._admission_lock:
            if self._in_flight >= self.settings.hermes_max_concurrency:
                return {"accepted": False, "delivered": False, "reason": "concurrency_limit"}
            self._in_flight += 1
        try:
            response = await self.client.post(
                f"{self.settings.hermes_api_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("Hermes API request failed; no response delivered")
            return {"accepted": True, "delivered": False, "reason": "upstream_failure"}
        finally:
            async with self._admission_lock:
                self._in_flight -= 1

        reply = self._extract_reply(body)
        if reply is None:
            log.warning("Hermes API response had no assistant text")
            return {"accepted": True, "delivered": False, "reason": "invalid_response"}
        return {"accepted": True, "delivered": True, "reply": reply}

    def _extract_reply(self, body: Any) -> str | None:
        if not isinstance(body, dict):
            return None
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return None
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            return None
        return message["content"][: self.settings.hermes_max_response_chars]
