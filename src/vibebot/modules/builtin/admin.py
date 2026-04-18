"""Built-in admin module. `!modules`, `!repos`, `!reload <repo> <name>` — gated by ACL."""

from __future__ import annotations

from vibebot.core.acl import Identity
from vibebot.core.events import Event
from vibebot.modules.base import Module

PERMISSION = "admin"


class AdminModule(Module):
    name = "admin"
    description = "In-channel admin commands for users with the 'admin' permission."

    async def on_message(self, event: Event) -> None:
        message: str = event.get("message", "")
        if not message.startswith("!"):
            return
        source: str = event.get("source", "")
        target: str = event.get("target", "")
        conn = self.bot.networks.get(event.network)
        if conn is None or not target:
            return
        reply_to = target if target.startswith("#") else source
        # pydle provides userhost via client.users; fall back to nick-only identity.
        userhost = _userhost(conn, source)
        identity = Identity.parse(userhost or f"{source}!unknown@unknown")
        if not await self.bot.acl.check(identity, PERMISSION):
            return

        args = message.split()
        cmd = args[0]
        if cmd == "!modules":
            mods = ", ".join(f"{m.repo}/{m.name}({'on' if m.enabled else 'off'})" for m in self.bot.modules.list_loaded()) or "none"
            await conn.send_message(reply_to, mods)
        elif cmd == "!repos":
            repos = await self.bot.repos.list_repos()
            listing = ", ".join(f"{r.name}({r.branch})" for r in repos) or "none"
            await conn.send_message(reply_to, listing)
        elif cmd == "!reload" and len(args) == 3:
            try:
                await self.bot.modules.reload(args[1], args[2])
                await conn.send_message(reply_to, f"reloaded {args[1]}/{args[2]}")
            except Exception as exc:
                await conn.send_message(reply_to, f"error: {exc}")


def _userhost(conn, nick: str) -> str | None:
    users = getattr(conn.client, "users", {}) or {}
    info = users.get(nick)
    if not isinstance(info, dict):
        return None
    ident = info.get("username") or "*"
    host = info.get("hostname") or "*"
    return f"{nick}!{ident}@{host}"
