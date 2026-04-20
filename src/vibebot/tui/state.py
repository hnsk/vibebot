"""Shared UI state for the TUI.

`UiState` holds every piece of data the chat view renders from: networks,
per-(network, target) buffers, channel rosters, topics, active pointers, unread
counters. `apply_event` translates a WS `Event` into buffer appends + derived
state changes, mirroring the rules in `src/vibebot/web/static/app.js` and
`src/vibebot/core/history.py` so the TUI renders the same way the web UI does.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from vibebot.core.events import Event

_CHANNEL_PREFIXES = ("#", "&", "!", "+")
_ACTION_RE = re.compile(r"^\x01ACTION (.*)\x01$")
BUFFER_CAPACITY = 500
ECHO_TTL_SECONDS = 10.0


def is_channel(target: str | None) -> bool:
    return isinstance(target, str) and bool(target) and target[0] in _CHANNEL_PREFIXES


def buf_key(network: str, target: str) -> tuple[str, str]:
    return (network, target)


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class Line:
    """One rendered row in a buffer.

    `kind` is msg | action | notice | event | system | whois. `event` is the
    event subtype (join/part/quit/kick/mode/nick/topic) when kind == "event".
    """

    kind: str
    ts: datetime = field(default_factory=_now)
    nick: str = ""
    body: str = ""
    event: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    self_sent: bool = False


@dataclass
class _PendingEcho:
    kind: str
    body: str
    expires: float


@dataclass
class UiState:
    """In-memory model of everything the chat view renders."""

    # {network_name: dict from routes/networks.py list_networks}
    networks: dict[str, dict] = field(default_factory=dict)
    # Own nick per network.
    own_nicks: dict[str, str] = field(default_factory=dict)
    # {network_name: [channel_name, …]} — declared + joined channels.
    declared: dict[str, set[str]] = field(default_factory=dict)
    # {(net, target): deque[Line]} — ring buffer of chat lines.
    buffers: dict[tuple[str, str], deque[Line]] = field(default_factory=dict)
    # {(net, channel): [{nick, prefix, ident, host, modes}, …]}
    rosters: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    # {(net, channel): {topic, by, set_at}}
    topics: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # {(net, target): int}
    unread: dict[tuple[str, str], int] = field(default_factory=dict)
    # Buffers that have had server history merged in.
    hydrated: set[tuple[str, str]] = field(default_factory=set)
    # FIFO of own-sent lines awaiting the pydle-synthesized echo so we can dedup.
    pending_echoes: dict[tuple[str, str], list[_PendingEcho]] = field(default_factory=dict)
    # Currently-active selection.
    active_net: str | None = None
    active_target: str | None = None

    # ------------------------------------------------------------------
    # buffer helpers
    # ------------------------------------------------------------------
    def buffer(self, net: str, target: str) -> deque[Line]:
        key = buf_key(net, target)
        buf = self.buffers.get(key)
        if buf is None:
            buf = deque(maxlen=BUFFER_CAPACITY)
            self.buffers[key] = buf
        return buf

    def push_line(self, net: str, target: str, line: Line) -> None:
        self.buffer(net, target).append(line)
        if self.active_net == net and self.active_target == target:
            return
        if line.kind in ("msg", "action"):
            key = buf_key(net, target)
            self.unread[key] = self.unread.get(key, 0) + 1

    def clear_unread(self, net: str, target: str) -> None:
        self.unread.pop(buf_key(net, target), None)

    def record_pending_echo(self, net: str, target: str, kind: str, body: str) -> None:
        import time

        q = self.pending_echoes.setdefault(buf_key(net, target), [])
        q.append(_PendingEcho(kind=kind, body=body, expires=time.monotonic() + ECHO_TTL_SECONDS))

    def consume_pending_echo(self, net: str, target: str, kind: str, body: str) -> bool:
        import time

        q = self.pending_echoes.get(buf_key(net, target))
        if not q:
            return False
        now = time.monotonic()
        while q and q[0].expires < now:
            q.pop(0)
        if q and q[0].kind == kind and q[0].body == body:
            q.pop(0)
            return True
        return False

    def targets_for(self, net: str) -> tuple[list[str], list[str]]:
        """Return (channels, queries) for a network — sorted for stable display."""
        seen: set[str] = set(self.declared.get(net, set()))
        for (bn, bt) in self.buffers:
            if bn == net and bt != "*":
                seen.add(bt)
        chans = sorted((t for t in seen if is_channel(t)), key=str.lower)
        queries = sorted((t for t in seen if not is_channel(t)), key=str.lower)
        return chans, queries

    # ------------------------------------------------------------------
    # event → buffer routing (mirrors app.js:routeEvent and history.py)
    # ------------------------------------------------------------------
    def apply_event(self, event: Event) -> set[tuple[str, str]]:
        """Mutate state from a live event. Returns buffer keys that changed."""
        net = event.network
        p = event.payload
        k = event.kind
        changed: set[tuple[str, str]] = set()

        if k == "message":
            target = p.get("target") or ""
            src = p.get("source") or ""
            body = p.get("message") or ""
            action_body = _action_body(body)
            line_kind = "action" if action_body is not None else "msg"
            line_body = action_body if action_body is not None else body
            own = self.own_nicks.get(net)
            buffer = target if is_channel(target) else (target if own and src == own else src)
            if not buffer:
                return changed
            if own and src == own and self.consume_pending_echo(net, buffer, line_kind, line_body):
                return changed
            self.push_line(
                net,
                buffer,
                Line(kind=line_kind, nick=src, body=line_body, self_sent=bool(own and src == own)),
            )
            changed.add(buf_key(net, buffer))

        elif k == "notice":
            target = p.get("target") or ""
            src = p.get("source") or ""
            body = p.get("message") or ""
            own = self.own_nicks.get(net)
            buffer = target if is_channel(target) else (target if own and src == own else src)
            if not buffer:
                return changed
            self.push_line(net, buffer, Line(kind="notice", nick=src, body=body))
            changed.add(buf_key(net, buffer))

        elif k == "join":
            ch = p.get("channel") or ""
            user = p.get("user") or ""
            if is_channel(ch):
                self.declared.setdefault(net, set()).add(ch)
                self.push_line(
                    net,
                    ch,
                    Line(
                        kind="event",
                        event="join",
                        nick=user,
                        body=f"{user} ({p.get('ident') or '*'}@{p.get('host') or '*'}) joined {ch}",
                        extras={"channel": ch, "ident": p.get("ident"), "host": p.get("host")},
                    ),
                )
                changed.add(buf_key(net, ch))

        elif k == "part":
            ch = p.get("channel") or ""
            user = p.get("user") or ""
            if is_channel(ch):
                reason = p.get("message")
                body = f"{user} ({p.get('ident') or '*'}@{p.get('host') or '*'}) left {ch}"
                if reason:
                    body += f" ({reason})"
                self.push_line(
                    net,
                    ch,
                    Line(kind="event", event="part", nick=user, body=body, extras={"channel": ch, "reason": reason}),
                )
                changed.add(buf_key(net, ch))

        elif k == "quit":
            user = p.get("user") or ""
            reason = p.get("message")
            body = f"{user} quit" + (f" ({reason})" if reason else "")
            # Broadcast to every channel buffer known for this network.
            for (bn, bt) in list(self.buffers):
                if bn != net:
                    continue
                if is_channel(bt) or bt == user:
                    self.push_line(net, bt, Line(kind="event", event="quit", nick=user, body=body, extras={"reason": reason}))
                    changed.add((bn, bt))

        elif k == "kick":
            ch = p.get("channel") or ""
            t = p.get("target") or ""
            by = p.get("by")
            reason = p.get("reason")
            if is_channel(ch):
                body = f"{t} kicked from {ch}"
                if by:
                    body += f" by {by}"
                if reason:
                    body += f" ({reason})"
                self.push_line(
                    net,
                    ch,
                    Line(kind="event", event="kick", nick=t, body=body, extras={"channel": ch, "by": by, "reason": reason}),
                )
                changed.add(buf_key(net, ch))

        elif k == "mode":
            ch = p.get("channel") or ""
            modes = list(p.get("modes") or [])
            by = p.get("by")
            if is_channel(ch):
                flags = " ".join(str(x) for x in modes)
                body = f"mode {ch} {flags}" + (f" by {by}" if by else "")
                self.push_line(
                    net,
                    ch,
                    Line(kind="event", event="mode", body=body, extras={"channel": ch, "modes": modes, "by": by}),
                )
                changed.add(buf_key(net, ch))

        elif k == "nick":
            old = p.get("old") or ""
            new = p.get("new") or ""
            if self.own_nicks.get(net) == old:
                self.own_nicks[net] = new
            body = f"{old} is now known as {new}"
            for (bn, bt) in list(self.buffers):
                if bn != net:
                    continue
                if is_channel(bt) or bt == old:
                    self.push_line(
                        net,
                        bt,
                        Line(kind="event", event="nick", body=body, extras={"old": old, "new": new}),
                    )
                    changed.add((bn, bt))

        elif k == "topic":
            ch = p.get("channel") or ""
            if is_channel(ch):
                self.topics[buf_key(net, ch)] = {
                    "topic": p.get("topic"),
                    "by": p.get("by"),
                    "set_at": p.get("set_at"),
                }
                if not p.get("initial"):
                    by = p.get("by")
                    topic = p.get("topic")
                    body = "topic" + (f" by {by}" if by else "") + f": {topic or '(cleared)'}"
                    self.push_line(
                        net,
                        ch,
                        Line(kind="event", event="topic", body=body, extras={"channel": ch, "topic": topic, "by": by}),
                    )
                changed.add(buf_key(net, ch))

        elif k == "connect":
            self.push_line(net, "*", Line(kind="system", body=f"connected to {net}"))
            changed.add(buf_key(net, "*"))

        elif k == "host_hidden":
            self.push_line(net, "*", Line(kind="system", body=f"host hidden on {net}"))
            changed.add(buf_key(net, "*"))

        elif k == "server_reply":
            cmd = p.get("command") or ""
            params = list(p.get("params") or [])
            own = self.own_nicks.get(net)
            rest = params[1:] if (params and own and params[0] == own) else params
            body = f"[server] {cmd} {' '.join(str(x) for x in rest)}".strip()
            self.push_line(net, "*", Line(kind="system", body=body))
            changed.add(buf_key(net, "*"))

        elif k == "whois":
            # Render into active buffer if available, else a status-ish fallback.
            target = self.active_target if (self.active_net == net and self.active_target) else (p.get("nick") or "*")
            if p.get("error"):
                body = f"whois {p.get('nick') or ''}: {p.get('error')}"
            else:
                parts: list[str] = []
                if p.get("username") or p.get("hostname"):
                    parts.append(f"{p.get('username') or '?'}@{p.get('hostname') or '?'}")
                if p.get("realname"):
                    parts.append(f"realname={p.get('realname')}")
                if p.get("account"):
                    parts.append(f"account={p.get('account')}")
                if p.get("server"):
                    parts.append(f"server={p.get('server')}")
                chans = p.get("channels")
                if isinstance(chans, list) and chans:
                    parts.append("chans=" + " ".join(str(c) for c in chans))
                body = f"whois {p.get('nick') or ''}: " + " · ".join(parts) if parts else f"whois {p.get('nick') or ''}: (no info)"
            self.push_line(net, target, Line(kind="whois", body=body, extras=dict(p)))
            changed.add(buf_key(net, target))

        return changed


def _action_body(message: str) -> str | None:
    m = _ACTION_RE.match(message or "")
    return m.group(1) if m else None
