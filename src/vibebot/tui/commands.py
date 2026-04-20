"""Slash-command parsing + dispatch.

Translates a composer line like `/op alice` into an ApiClient call. Mirrors
the command set the web UI exposes (see `src/vibebot/web/static/app.js`) and
the endpoints in `src/vibebot/api/routes/send.py` + `networks.py`.
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from vibebot.tui.api import ApiClient
from vibebot.tui.state import UiState, is_channel


class CommandError(Exception):
    pass


@dataclass
class ParsedCommand:
    name: str
    args: list[str]
    raw: str


def parse_slash(line: str) -> ParsedCommand | None:
    """Return a ParsedCommand if `line` is a slash command, else None.

    A leading `//` is treated as a literal message with one slash stripped
    (matching the convention used by most IRC clients).
    """
    if not line.startswith("/") or line.startswith("//"):
        return None
    body = line[1:].strip()
    if not body:
        return None
    # shlex keeps quoted tokens intact (useful for /topic "multi word").
    try:
        tokens = shlex.split(body, posix=True)
    except ValueError:
        tokens = body.split()
    if not tokens:
        return None
    name = tokens[0].lower()
    return ParsedCommand(name=name, args=tokens[1:], raw=body)


def literal_message(line: str) -> str:
    """Strip a leading `//` escape so `//cmd` sends the literal text `/cmd`."""
    if line.startswith("//"):
        return line[1:]
    return line


Handler = Callable[["CommandContext"], Awaitable[None]]


@dataclass
class CommandContext:
    api: ApiClient
    state: UiState
    cmd: ParsedCommand
    on_open_query: Callable[[str, str], None]
    on_close_query: Callable[[str, str], None]

    @property
    def net(self) -> str:
        if not self.state.active_net:
            raise CommandError("no active network")
        return self.state.active_net

    @property
    def target(self) -> str:
        if not self.state.active_target:
            raise CommandError("no active channel/query")
        return self.state.active_target

    def _channel_and_rest(self, require_nick: bool = False) -> tuple[str, list[str]]:
        """Return (channel, remaining_args).

        If the first arg is a channel name, use it; otherwise fall back to the
        active channel (matches how the web UI composer infers context).
        """
        args = list(self.cmd.args)
        if args and is_channel(args[0]):
            channel = args.pop(0)
        else:
            if not is_channel(self.state.active_target):
                raise CommandError(f"/{self.cmd.name} needs a channel")
            channel = self.state.active_target or ""
        if require_nick and not args:
            raise CommandError(f"/{self.cmd.name} needs a nick")
        return channel, args


async def _cmd_join(ctx: CommandContext) -> None:
    if not ctx.cmd.args:
        raise CommandError("/join needs a channel")
    await ctx.api.join(ctx.net, ctx.cmd.args[0])


async def _cmd_part(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest()
    reason = " ".join(rest) if rest else None
    await ctx.api.part(ctx.net, channel, reason)


async def _cmd_op(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    await ctx.api.op(ctx.net, channel, rest[0])


async def _cmd_deop(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    await ctx.api.deop(ctx.net, channel, rest[0])


async def _cmd_voice(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    await ctx.api.voice(ctx.net, channel, rest[0])


async def _cmd_devoice(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    await ctx.api.devoice(ctx.net, channel, rest[0])


async def _cmd_kick(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    nick = rest[0]
    reason = " ".join(rest[1:]) if len(rest) > 1 else None
    await ctx.api.kick(ctx.net, channel, nick, reason)


async def _cmd_ban(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    await ctx.api.ban(ctx.net, channel, rest[0])


async def _cmd_kickban(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest(require_nick=True)
    nick = rest[0]
    reason = " ".join(rest[1:]) if len(rest) > 1 else None
    await ctx.api.kickban(ctx.net, channel, nick, reason)


async def _cmd_mode(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest()
    if not rest:
        raise CommandError("/mode needs flags (e.g. +m)")
    flags = rest[0]
    args = rest[1:]
    await ctx.api.mode(ctx.net, channel, flags, args)


async def _cmd_topic(ctx: CommandContext) -> None:
    channel, rest = ctx._channel_and_rest()
    topic = " ".join(rest) if rest else None
    await ctx.api.set_topic(ctx.net, channel, topic)


async def _cmd_nick(ctx: CommandContext) -> None:
    if not ctx.cmd.args:
        raise CommandError("/nick needs a new nick")
    await ctx.api.set_nick(ctx.net, ctx.cmd.args[0])


async def _cmd_whois(ctx: CommandContext) -> None:
    if not ctx.cmd.args:
        raise CommandError("/whois needs a nick")
    await ctx.api.whois(ctx.net, ctx.cmd.args[0])


async def _cmd_raw(ctx: CommandContext) -> None:
    if not ctx.cmd.raw.startswith("raw"):
        # Shouldn't happen — defensive.
        raise CommandError("/raw needs a line")
    # shlex-quoted raw lines will lose meaning (IRC messages don't use shell
    # quoting). Reach past the parser to grab the original text.
    line = ctx.cmd.raw[len("raw"):].strip()
    if not line:
        raise CommandError("/raw needs a line")
    await ctx.api.raw(ctx.net, line)


async def _cmd_me(ctx: CommandContext) -> None:
    target = ctx.state.active_target
    if not target or target == "*":
        raise CommandError("/me needs an active channel or query")
    body = ctx.cmd.raw[len("me"):].strip()
    if not body:
        raise CommandError("/me needs a message")
    # CTCP ACTION encoding — matches the web UI.
    await ctx.api.send(ctx.net, target, f"\x01ACTION {body}\x01")


async def _cmd_query(ctx: CommandContext) -> None:
    if not ctx.cmd.args:
        raise CommandError("/query needs a nick")
    peer = ctx.cmd.args[0]
    ctx.on_open_query(ctx.net, peer)


async def _cmd_close(ctx: CommandContext) -> None:
    target = ctx.state.active_target
    if not target or is_channel(target) or target == "*":
        raise CommandError("/close needs an active query")
    await ctx.api.close_query(ctx.net, target)
    ctx.on_close_query(ctx.net, target)


HANDLERS: dict[str, Handler] = {
    "join": _cmd_join,
    "part": _cmd_part,
    "leave": _cmd_part,
    "op": _cmd_op,
    "deop": _cmd_deop,
    "voice": _cmd_voice,
    "devoice": _cmd_devoice,
    "kick": _cmd_kick,
    "ban": _cmd_ban,
    "kickban": _cmd_kickban,
    "mode": _cmd_mode,
    "topic": _cmd_topic,
    "nick": _cmd_nick,
    "whois": _cmd_whois,
    "raw": _cmd_raw,
    "me": _cmd_me,
    "query": _cmd_query,
    "msg": _cmd_query,  # alias for /query
    "close": _cmd_close,
}


async def dispatch(
    ctx: CommandContext,
) -> None:
    """Run the handler matching ctx.cmd.name; raise CommandError for user errors."""
    handler = HANDLERS.get(ctx.cmd.name)
    if handler is None:
        raise CommandError(f"unknown command: /{ctx.cmd.name}")
    await handler(ctx)


def command_summary() -> list[tuple[str, str]]:
    """Short help list for the TUI's `/help` handler."""
    return [
        ("/join <chan>", "join a channel"),
        ("/part [chan] [reason]", "leave a channel"),
        ("/op <nick> | /deop <nick>", "toggle op on the active channel"),
        ("/voice <nick> | /devoice <nick>", "toggle voice on the active channel"),
        ("/kick <nick> [reason]", "kick a user from the active channel"),
        ("/ban <nick> | /kickban <nick> [reason]", "ban / kickban"),
        ("/mode <flags> [args…]", "set channel mode"),
        ("/topic [text]", "read or set the channel topic"),
        ("/nick <newnick>", "change your nickname on the current network"),
        ("/whois <nick>", "show whois"),
        ("/me <message>", "send an action"),
        ("/query <nick>", "open a PM buffer"),
        ("/close", "close the active query"),
        ("/raw <line>", "send raw IRC"),
    ]


__all__ = [
    "CommandContext",
    "CommandError",
    "ParsedCommand",
    "command_summary",
    "dispatch",
    "literal_message",
    "parse_slash",
]


# Silence unused-symbol warnings if mypy runs: explicit re-export list above.
_ = (ParsedCommand, Any)
