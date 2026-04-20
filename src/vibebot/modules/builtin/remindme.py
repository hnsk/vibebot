"""Built-in `!remindme` module — schedule personal reminders.

Syntax: ``!remindme <when> <message>`` where ``<when>`` is either a short
form like ``5m``, ``2h``, ``3d`` (optionally concatenated, e.g. ``1h30m``)
or a long form like ``5 minutes``, ``1 day``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from vibebot.core.acl import Identity
from vibebot.core.events import Event
from vibebot.modules.base import Module
from vibebot.scheduler.service import ScheduleError

if TYPE_CHECKING:
    from vibebot.core.network import NetworkConnection

_UNIT_SECONDS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31_536_000,
}

_LONG_UNIT: dict[str, str] = {
    "second": "s",
    "minute": "m",
    "hour": "h",
    "day": "d",
    "week": "w",
    "year": "y",
}

_SHORT_RE = re.compile(r"^(?:(\d+)([smhdwy]))+$")
_SHORT_TOKEN_RE = re.compile(r"(\d+)([smhdwy])")
_LONG_RE = re.compile(
    r"^\s*(\d+)\s+(second|minute|hour|day|week|year)s?\s*$",
    re.IGNORECASE,
)

MAX_SECONDS = 5 * _UNIT_SECONDS["y"]


def parse_duration(text: str) -> int:
    """Parse a human duration string into seconds.

    Accepts ``5m``, ``1h30m``, ``1 day``, ``2 hours``. Raises
    ``ValueError`` on empty, unknown, or out-of-range input.
    """
    if not text:
        raise ValueError("empty duration")
    stripped = text.strip()
    long_match = _LONG_RE.match(stripped)
    if long_match:
        amount = int(long_match.group(1))
        unit = _UNIT_SECONDS[_LONG_UNIT[long_match.group(2).lower()]]
        total = amount * unit
    elif _SHORT_RE.match(stripped):
        total = 0
        for num, unit_char in _SHORT_TOKEN_RE.findall(stripped):
            total += int(num) * _UNIT_SECONDS[unit_char]
    else:
        raise ValueError(f"unrecognized duration: {text!r}")
    if total <= 0:
        raise ValueError("duration must be positive")
    if total > MAX_SECONDS:
        raise ValueError(f"duration exceeds max ({MAX_SECONDS}s)")
    return total


class RemindMeSettings(BaseModel):
    command: str = Field(
        default="!remindme",
        description="Command prefix that triggers a reminder.",
    )
    reply_format: str = Field(
        default="{nick}: reminder — {message}",
        description="Template for the delivered reminder. Placeholders: {nick}, {message}.",
    )


class RemindMeModule(Module):
    name = "remindme"
    description = "Let users schedule personal reminders via chat."
    Settings = RemindMeSettings

    async def on_load(self) -> None:
        self.register_handler("remind", self._fire)

    async def on_message(self, event: Event) -> None:
        message: str = event.get("message", "")
        if not message:
            return
        stripped = message.strip()
        command: str = self.settings.command
        if not (stripped == command or stripped.startswith(command + " ")):
            return

        source: str = event.get("source", "")
        target: str = event.get("target", "")
        conn = self.bot.networks.get(event.network)
        if conn is None or not target or not source:
            return
        reply_to = target if target.startswith("#") else source

        parts = stripped.split(maxsplit=2)
        if len(parts) < 3 or not parts[2].strip():
            await conn.send_message(
                reply_to,
                f"usage: {command} <when> <message>  "
                "(e.g. 5m, 2h, 1d, '1 day')",
            )
            return

        when_token = parts[1]
        body = parts[2].strip()

        # Support two-word long form: "1 day rest". split(maxsplit=2) produced
        # parts=[cmd, "1", "day rest"] — detect that and re-split.
        if when_token.isdigit():
            extra = body.split(maxsplit=1)
            if len(extra) == 2:
                candidate = f"{when_token} {extra[0]}"
                try:
                    seconds = parse_duration(candidate)
                except ValueError:
                    pass
                else:
                    body = extra[1].strip()
                    if not body:
                        await conn.send_message(
                            reply_to,
                            f"usage: {command} <when> <message>",
                        )
                        return
                    await self._schedule(
                        conn, reply_to, event, source, body, seconds
                    )
                    return
            await conn.send_message(
                reply_to,
                f"usage: {command} <when> <message>  "
                "(e.g. 5m, 2h, 1d, '1 day')",
            )
            return

        try:
            seconds = parse_duration(when_token)
        except ValueError as exc:
            await conn.send_message(reply_to, f"bad duration: {exc}")
            return

        await self._schedule(conn, reply_to, event, source, body, seconds)

    async def _schedule(
        self,
        conn: NetworkConnection,
        reply_to: str,
        event: Event,
        source: str,
        body: str,
        seconds: int,
    ) -> None:
        run_date = datetime.now(UTC) + timedelta(seconds=seconds)
        userhost = _userhost(conn, source) or f"{source}!unknown@unknown"
        identity = Identity.parse(userhost)
        try:
            dto = await self.bot.schedules.create(
                owner_nick=source,
                owner_mask=identity.mask(),
                owner_network=event.network,
                repo="__builtin__",
                module="remindme",
                handler="remind",
                trigger={"type": "date", "run_date": run_date.isoformat()},
                payload={
                    "network": event.network,
                    "reply_to": reply_to,
                    "nick": source,
                    "message": body,
                },
                title=f"remindme:{source}:{body[:40]}",
                misfire_grace_seconds=300,
                requester=identity,
            )
        except ScheduleError as exc:
            await conn.send_message(reply_to, f"error: {exc}")
            return
        await conn.send_message(
            reply_to,
            f"{source}: reminder set for {_humanize(seconds)} (id {dto.id[:8]})",
        )

    async def _fire(self, payload: dict[str, Any]) -> None:
        network = payload.get("network")
        reply_to = payload.get("reply_to")
        nick = payload.get("nick", "")
        message = payload.get("message", "")
        if not network or not reply_to:
            return
        conn = self.bot.networks.get(network)
        if conn is None:
            return
        try:
            text = self.settings.reply_format.format(nick=nick, message=message)
        except (KeyError, IndexError):
            text = f"{nick}: reminder — {message}"
        await conn.send_message(reply_to, text)


def _userhost(conn: NetworkConnection, nick: str) -> str | None:
    users = getattr(conn.client, "users", {}) or {}
    info = users.get(nick)
    if not isinstance(info, dict):
        return None
    ident = info.get("username") or "*"
    host = info.get("hostname") or "*"
    return f"{nick}!{ident}@{host}"


def _humanize(seconds: int) -> str:
    for unit, size in (("y", 31_536_000), ("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
