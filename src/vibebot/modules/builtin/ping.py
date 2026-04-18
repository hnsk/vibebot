"""Built-in ping module — simple sanity check."""

from __future__ import annotations

from vibebot.core.events import Event
from vibebot.modules.base import Module


class PingModule(Module):
    name = "ping"
    description = "Reply 'pong' to !ping."

    async def on_message(self, event: Event) -> None:
        message: str = event.get("message", "")
        if message.strip() != "!ping":
            return
        target: str = event.get("target", "")
        source: str = event.get("source", "")
        reply_to = target if target.startswith("#") else source
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        await conn.send_message(reply_to, "pong")
