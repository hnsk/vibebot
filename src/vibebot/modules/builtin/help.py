"""Built-in help module. Responds to `!help` on channels and in private messages."""

from __future__ import annotations

from vibebot.core.events import Event
from vibebot.modules.base import Module, on_message


class HelpModule(Module):
    name = "help"
    description = "Reply to !help with the list of loaded modules."

    @on_message(startswith="!help")
    async def handle_help(self, event: Event) -> None:
        target: str = event.get("target", "")
        source: str = event.get("source", "")
        if not target:
            return
        reply_to = target if target.startswith("#") else source
        loaded = self.bot.modules.list_loaded()
        names = ", ".join(sorted({m.name for m in loaded if m.enabled})) or "none"
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        await conn.send_message(reply_to, f"vibebot modules: {names}")
