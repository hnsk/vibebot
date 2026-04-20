"""Built-in ping module — simple sanity check."""

from __future__ import annotations

from pydantic import BaseModel, Field

from vibebot.core.events import Event
from vibebot.modules.base import Module, on_message


class PingSettings(BaseModel):
    response: str = Field(default="pong", description="Reply sent when someone says !ping.")


class PingModule(Module):
    name = "ping"
    description = "Reply to !ping with a configurable response."
    Settings = PingSettings

    @on_message(exact="!ping")
    async def handle_ping(self, event: Event) -> None:
        target: str = event.get("target", "")
        source: str = event.get("source", "")
        reply_to = target if target.startswith("#") else source
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        await conn.send_message(reply_to, self.settings.response)
