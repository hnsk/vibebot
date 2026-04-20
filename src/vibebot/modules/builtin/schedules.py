"""Built-in admin-only `!schedule[s]` IRC commands.

Lets operators audit and cancel schedules across every module. Per-user
`cancel my own` UX is *not* provided here — each module that creates user
schedules owns that surface and calls `ScheduleService.cancel(...)` with the
requester identity so ownership is enforced there.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from vibebot.core.acl import Identity
from vibebot.core.events import Event
from vibebot.modules.base import Module
from vibebot.scheduler.service import ScheduleDTO, ScheduleError

if TYPE_CHECKING:
    from vibebot.core.network import NetworkConnection

PERMISSION = "admin"


class SchedulesModule(Module):
    name = "schedules"
    description = "Admin commands for auditing and cancelling schedules."

    async def on_message(self, event: Event) -> None:
        message: str = event.get("message", "")
        if not (message.startswith("!schedules") or message.startswith("!schedule ")):
            return
        source: str = event.get("source", "")
        target: str = event.get("target", "")
        conn = self.bot.networks.get(event.network)
        if conn is None or not target:
            return
        reply_to = target if target.startswith("#") else source
        userhost = _userhost(conn, source)
        identity = Identity.parse(userhost or f"{source}!unknown@unknown")
        if not await self.bot.acl.check(identity, PERMISSION):
            return

        args = message.split()
        cmd = args[0]
        if cmd == "!schedules":
            await self._cmd_list(conn, reply_to)
            return
        if cmd != "!schedule" or len(args) < 2:
            await conn.send_message(reply_to, "usage: !schedules | !schedule show|cancel|pause|resume|run <id>")
            return
        sub = args[1]
        if sub == "show" and len(args) == 3:
            await self._cmd_show(conn, reply_to, args[2])
        elif sub == "cancel" and len(args) == 3:
            await self._run_op(conn, reply_to, args[2], self.bot.schedules.cancel, "cancelled")
        elif sub == "pause" and len(args) == 3:
            await self._run_op(conn, reply_to, args[2], self.bot.schedules.pause, "paused")
        elif sub == "resume" and len(args) == 3:
            await self._run_op(conn, reply_to, args[2], self.bot.schedules.resume, "resumed")
        elif sub == "run" and len(args) == 3:
            await self._run_op(conn, reply_to, args[2], self.bot.schedules.run_now, "triggered")
        else:
            await conn.send_message(reply_to, "usage: !schedules | !schedule show|cancel|pause|resume|run <id>")

    async def _cmd_list(self, conn: NetworkConnection, reply_to: str) -> None:
        items = await self.bot.schedules.list()
        if not items:
            await conn.send_message(reply_to, "no schedules")
            return
        for dto in items[:20]:
            next_run = dto.next_run_at.isoformat() if dto.next_run_at else "-"
            await conn.send_message(
                reply_to,
                f"{dto.id[:8]} {dto.status} next={next_run} "
                f"{dto.repo_name}/{dto.module_name}#{dto.handler_name} owner={dto.owner_nick}",
            )
        if len(items) > 20:
            await conn.send_message(reply_to, f"... {len(items) - 20} more")

    async def _cmd_show(self, conn: NetworkConnection, reply_to: str, schedule_id: str) -> None:
        dto = await self._resolve(conn, reply_to, schedule_id)
        if dto is None:
            return
        await conn.send_message(
            reply_to,
            f"id={dto.id} status={dto.status} owner={dto.owner_nick} ({dto.owner_mask}) "
            f"{dto.repo_name}/{dto.module_name}#{dto.handler_name} trigger={dto.trigger} "
            f"next={dto.next_run_at} last={dto.last_run_at} err={dto.last_error}",
        )

    async def _run_op(
        self,
        conn: NetworkConnection,
        reply_to: str,
        schedule_id: str,
        op: Callable[[str], Awaitable[ScheduleDTO | None]],
        verb: str,
    ) -> None:
        dto = await self._resolve(conn, reply_to, schedule_id)
        if dto is None:
            return
        try:
            await op(dto.id)
        except ScheduleError as exc:
            await conn.send_message(reply_to, f"error: {exc}")
            return
        await conn.send_message(reply_to, f"{verb} {dto.id[:8]}")

    async def _resolve(
        self,
        conn: NetworkConnection,
        reply_to: str,
        schedule_id_or_prefix: str,
    ) -> ScheduleDTO | None:
        """Accept either a full id or an 8-char prefix."""
        if len(schedule_id_or_prefix) >= 32:
            try:
                return await self.bot.schedules.get(schedule_id_or_prefix)
            except ScheduleError:
                await conn.send_message(reply_to, f"no such schedule: {schedule_id_or_prefix}")
                return None
        items = await self.bot.schedules.list()
        matches = [dto for dto in items if dto.id.startswith(schedule_id_or_prefix)]
        if len(matches) == 0:
            await conn.send_message(reply_to, f"no such schedule: {schedule_id_or_prefix}")
            return None
        if len(matches) > 1:
            await conn.send_message(reply_to, f"ambiguous prefix: {schedule_id_or_prefix}")
            return None
        return matches[0]


def _userhost(conn: NetworkConnection, nick: str) -> str | None:
    users = getattr(conn.client, "users", {}) or {}
    info = users.get(nick)
    if not isinstance(info, dict):
        return None
    ident = info.get("username") or "*"
    host = info.get("hostname") or "*"
    return f"{nick}!{ident}@{host}"
