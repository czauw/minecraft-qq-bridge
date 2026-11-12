from __future__ import annotations

from typing import Any

from .models import ChatEvent, DeathEvent, PlayerEvent
from .ports import DisabledHermesAdminExecutor, HermesAdminExecutor, OutboundOneBot
from .settings import Settings
from .state import BridgeState


class BridgeService:
    def __init__(self, settings: Settings, onebot: OutboundOneBot, mc_callback,
                 hermes_admin: HermesAdminExecutor | None = None) -> None:
        self.settings = settings
        self.onebot = onebot
        self.mc_callback = mc_callback
        self.state = BridgeState(ttl_seconds=settings.state_ttl_seconds)
        self.hermes_admin = hermes_admin or DisabledHermesAdminExecutor()

    async def handle_mc_event(self, event_type: str, payload: Any) -> None:
        if event_type == "players":
            self.state.update_players(payload.players, payload.max_players)
        elif event_type == "status":
            self.state.update_status(payload.model_dump())
        elif event_type == "join":
            self.state.apply_join(payload.player)
        elif event_type == "leave":
            self.state.apply_leave(payload.player)
        if not self.settings.mc_notifications.get(event_type, False):
            return
        message = self._format_mc(event_type, payload)
        if message:
            for group_id in self.settings.allowed_group_ids:
                await self.onebot.send_group_message(group_id, message)

    def _format_mc(self, event_type: str, payload: Any) -> str | None:
        if event_type == "chat" and isinstance(payload, ChatEvent):
            return f"[MC] {payload.player}: {payload.message}"
        if event_type == "death" and isinstance(payload, DeathEvent):
            return f"[MC] {payload.message or payload.player + ' 死亡'}"
        if event_type == "join" and isinstance(payload, PlayerEvent):
            return f"[MC] {payload.player} 加入了服务器"
        if event_type == "leave" and isinstance(payload, PlayerEvent):
            return f"[MC] {payload.player} 离开了服务器"
        return None

    async def handle_onebot_event(self, event: dict[str, Any]) -> dict[str, bool]:
        rejected = {"accepted": False, "delivered": False}
        if not isinstance(event, dict):
            return rejected
        if event.get("post_type") != "message" or event.get("message_type") != "group":
            return rejected
        group_id = self._int(event.get("group_id"))
        user_id = self._int(event.get("user_id"))
        if group_id is None or user_id is None or group_id not in self.settings.allowed_group_ids:
            return rejected
        bot_id = self.settings.onebot_self_id or self._int(event.get("self_id"))
        if bot_id is not None and user_id == bot_id:
            return rejected
        text = event.get("message") or event.get("raw_message") or ""
        if isinstance(text, list):
            if any(not isinstance(s, dict) or s.get("type") != "text"
                   or not isinstance(s.get("data"), dict)
                   or not isinstance(s["data"].get("text"), str) for s in text):
                return rejected
            text = "".join(s["data"]["text"] for s in text)
        if not isinstance(text, str) or len(text) > 2100:
            return rejected
        parts = text.strip().split(maxsplit=2)
        if not parts or parts[0] != "/mc":
            return rejected
        command = parts[1].lower() if len(parts) > 1 else ""
        argument = parts[2] if len(parts) > 2 else ""
        if command == "hermes":
            return await self._handle_hermes(group_id, user_id, argument)
        if command not in self.settings.mc_commands:
            return rejected
        if command == "players":
            freshness = self.state.freshness("players")
            prefix = {"unknown": "未知快照", "stale": "过期快照", "fresh": "缓存快照"}[freshness]
            reply = "{}：玩家（{}人）：{}".format(prefix, len(self.state.players), ", ".join(self.state.players) or "无")
        elif command == "status":
            reply = self._status_text()
        elif command == "say" and self.settings.mc_say_enabled:
            if not argument or len(argument) > 2000 or not self.settings.qq_to_mc_enabled:
                return rejected
            delivered = await self.mc_callback.send({
                "event": "chat", "message": argument, "group_id": group_id,
                "user_id": user_id, "nickname": self._nickname(event),
            })
            return {"accepted": delivered, "delivered": delivered}
        else:
            return rejected
        delivered = await self.onebot.send_group_message(group_id, reply)
        return {"accepted": True, "delivered": delivered}

    async def _handle_hermes(self, group_id: int, user_id: int, prompt: str) -> dict[str, bool]:
        """Run only through the explicitly configured Hermes HTTP adapter."""
        rejected = {"accepted": False, "delivered": False}
        if not self.settings.hermes_admin_enabled:
            return rejected
        if user_id not in self.settings.hermes_admin_ids:
            return rejected
        if not prompt or len(prompt) > self.settings.hermes_max_prompt_chars:
            return rejected
        result = await self.hermes_admin.execute(prompt, actor_id=user_id)
        if not result.get("accepted") or not result.get("delivered"):
            return {"accepted": bool(result.get("accepted")), "delivered": False}
        reply = result.get("reply")
        if not isinstance(reply, str) or not reply:
            return {"accepted": True, "delivered": False}
        delivered = await self.onebot.send_group_message(group_id, reply)
        return {"accepted": True, "delivered": delivered}

    def _status_text(self) -> str:
        status = self.state.status
        online = {True: "在线", False: "离线", None: "未知"}[status.get("online")]
        freshness = {"fresh": "缓存快照", "stale": "过期快照", "unknown": "未知快照"}[self.state.freshness("status")]
        player_freshness = self.state.freshness("players")
        return f"服务器（{freshness}）：{online}；版本：{status.get('version') or '未知版本'}；玩家：{len(self.state.players)}（{player_freshness}）"

    @staticmethod
    def _int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed >= 0 and str(parsed) == str(value).strip() else None

    @staticmethod
    def _nickname(event: dict[str, Any]) -> str | None:
        sender = event.get("sender") or {}
        if not isinstance(sender, dict):
            return None
        value = sender.get("card") or sender.get("nickname")
        return str(value)[:256] if value else None
