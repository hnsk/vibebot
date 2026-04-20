"""Built-in admin module. `!modules`, `!repos`, `!reload <repo> <name>` — gated by ACL."""

from __future__ import annotations

from vibebot.core.acl import Identity
from vibebot.core.events import Event
from vibebot.modules.base import Module, on_message

PERMISSION = "admin"


class AdminModule(Module):
    name = "admin"
    description = "In-channel admin commands for users with the 'admin' permission."

    @on_message(exact="!modules")
    async def cmd_modules(self, event: Event) -> None:
        ctx = await self._context(event)
        if ctx is None:
            return
        conn, reply_to = ctx
        mods = ", ".join(
            f"{m.repo}/{m.name}({'on' if m.enabled else 'off'})"
            for m in self.bot.modules.list_loaded()
        ) or "none"
        await conn.send_message(reply_to, mods)

    @on_message(exact="!repos")
    async def cmd_repos(self, event: Event) -> None:
        ctx = await self._context(event)
        if ctx is None:
            return
        conn, reply_to = ctx
        repos = await self.bot.repos.list_repos()
        listing = ", ".join(f"{r.name}({r.branch})" for r in repos) or "none"
        await conn.send_message(reply_to, listing)

    @on_message(regex=r"^!reload\s+(\S+)\s+(\S+)\s*$")
    async def cmd_reload(self, event: Event) -> None:
        ctx = await self._context(event)
        if ctx is None:
            return
        conn, reply_to = ctx
        args = event.get("message", "").split()
        if len(args) != 3:
            return
        try:
            await self.bot.modules.reload(args[1], args[2])
            await conn.send_message(reply_to, f"reloaded {args[1]}/{args[2]}")
        except Exception as exc:
            await conn.send_message(reply_to, f"error: {exc}")

    async def _context(self, event: Event):
        """Resolve conn + reply_to, enforce ACL. Returns None to skip."""
        source: str = event.get("source", "")
        target: str = event.get("target", "")
        conn = self.bot.networks.get(event.network)
        if conn is None or not target:
            return None
        reply_to = target if target.startswith("#") else source
        userhost = _userhost(conn, source)
        identity = Identity.parse(userhost or f"{source}!unknown@unknown")
        if not await self.bot.acl.check(identity, PERMISSION):
            return None
        return conn, reply_to


def _userhost(conn, nick: str) -> str | None:
    users = getattr(conn.client, "users", {}) or {}
    info = users.get(nick)
    if not isinstance(info, dict):
        return None
    ident = info.get("username") or "*"
    host = info.get("hostname") or "*"
    return f"{nick}!{ident}@{host}"
