"""In-memory message ring buffers for refresh-safe backlog (channels + PM queries)."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Callable

from vibebot.core.events import Event, EventBus

_CHANNEL_PREFIXES = ("#", "&", "!", "+")
_ACTION_RE = re.compile(r"^\x01ACTION (.*)\x01$")

OwnNickResolver = Callable[[str], str | None]


def _is_channel(target: str | None) -> bool:
    return isinstance(target, str) and bool(target) and target[0] in _CHANNEL_PREFIXES


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


class ChannelHistory:
    """Ring buffer of pre-shaped line dicts, keyed by (network, target).

    `target` is a channel name (starts with #/&/!/+) for channel buffers, or a
    peer nickname for PM/query buffers. Own-nick resolution is needed to map
    PM events (where `target` is the bot's nick on inbound, and the peer on
    pydle-synthesized outbound echo) to a single peer-keyed buffer.
    """

    def __init__(
        self,
        capacity: int = 500,
        own_nick_of: OwnNickResolver | None = None,
    ) -> None:
        self._capacity = capacity
        self._store: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=capacity)
        )
        self._own_nick_of: OwnNickResolver = own_nick_of or (lambda _net: None)

    def set_own_nick_resolver(self, fn: OwnNickResolver) -> None:
        self._own_nick_of = fn

    def _pm_peer(self, network: str, target: str | None, source: str | None) -> str | None:
        """Resolve the peer nick for a PM event; returns None for channel targets."""
        if _is_channel(target) or not target:
            return None
        own = self._own_nick_of(network)
        if own and source and source == own:
            return target
        return source or target

    def append(self, network: str, channel: str, line: dict[str, Any]) -> None:
        self._store[(network, channel)].append(line)

    def snapshot(self, network: str, channel: str) -> list[dict[str, Any]]:
        return list(self._store.get((network, channel), ()))

    def peers(self, network: str) -> list[str]:
        """List PM peers (non-channel targets) with any recorded history."""
        seen = {ch for (n, ch) in self._store if n == network and not _is_channel(ch)}
        return sorted(seen, key=str.lower)

    def clear(self, network: str | None = None, channel: str | None = None) -> None:
        if network is None and channel is None:
            self._store.clear()
            return
        keys = [k for k in self._store if (network is None or k[0] == network) and (channel is None or k[1] == channel)]
        for k in keys:
            del self._store[k]

    def attach(self, bus: EventBus) -> None:
        bus.subscribe("*", self._on_event)

    async def _on_event(self, event: Event) -> None:
        net = event.network
        p = event.payload
        kind = event.kind
        if kind == "message":
            target = p.get("target")
            src = p.get("source") or ""
            msg = p.get("message") or ""
            m = _ACTION_RE.match(msg)
            line_kind = "action" if m else "msg"
            body = m.group(1) if m else msg
            own = self._own_nick_of(net)
            if _is_channel(target):
                self.append(net, target, {
                    "ts": _now(), "kind": line_kind, "nick": src, "body": body,
                })
            else:
                peer = self._pm_peer(net, target, src)
                if not peer:
                    return
                # Tag own-nick outbound echoes so the UI can render them on the
                # right side of the buffer (matches the live-event `self` flag).
                self_flag = bool(own and src == own)
                entry = {
                    "ts": _now(), "kind": line_kind, "nick": src, "body": body,
                }
                if self_flag:
                    entry["self"] = True
                self.append(net, peer, entry)
        elif kind == "notice":
            target = p.get("target")
            src = p.get("source") or ""
            msg = p.get("message") or ""
            if _is_channel(target):
                self.append(net, target, {
                    "ts": _now(), "kind": "notice", "nick": src, "body": msg,
                })
            else:
                peer = self._pm_peer(net, target, src)
                if not peer:
                    return
                own = self._own_nick_of(net)
                entry = {"ts": _now(), "kind": "notice", "nick": src, "body": msg}
                if own and src == own:
                    entry["self"] = True
                self.append(net, peer, entry)
        elif kind == "join":
            ch = p.get("channel")
            if not _is_channel(ch):
                return
            user = p.get("user") or ""
            ident = p.get("ident") or "*"
            host = p.get("host") or "*"
            self.append(net, ch, {
                "ts": _now(), "kind": "event", "event": "join", "glyph": "→",
                "user": user, "ident": ident, "host": host, "channel": ch,
                "body": f"{user} ({ident}@{host}) joined {ch}",
            })
        elif kind == "part":
            ch = p.get("channel")
            if not _is_channel(ch):
                return
            user = p.get("user") or ""
            ident = p.get("ident") or "*"
            host = p.get("host") or "*"
            reason = p.get("message")
            body = f"{user} ({ident}@{host}) left {ch}" + (f" ({reason})" if reason else "")
            self.append(net, ch, {
                "ts": _now(), "kind": "event", "event": "part", "glyph": "←",
                "user": user, "ident": ident, "host": host, "channel": ch, "reason": reason,
                "body": body,
            })
        elif kind == "quit":
            user = p.get("user") or ""
            ident = p.get("ident") or "*"
            host = p.get("host") or "*"
            reason = p.get("message")
            body = f"{user} ({ident}@{host}) quit" + (f" ({reason})" if reason else "")
            # Append to every channel buffer known for this network (membership
            # is not tracked here — acceptable to log in all channel buffers).
            # For PM buffers, only log when the quitting user IS the peer we're
            # talking with; otherwise the quit is irrelevant to that query.
            for (n, ch) in list(self._store.keys()):
                if n != net:
                    continue
                is_chan = _is_channel(ch)
                if not is_chan and ch != user:
                    continue
                self.append(net, ch, {
                    "ts": _now(), "kind": "event", "event": "quit", "glyph": "⤫",
                    "user": user, "ident": ident, "host": host, "reason": reason,
                    "body": body,
                })
        elif kind == "kick":
            ch = p.get("channel")
            if not _is_channel(ch):
                return
            t = p.get("target") or ""
            by = p.get("by")
            reason = p.get("reason")
            body = f"{t} kicked from {ch}" + (f" by {by}" if by else "") + (f" ({reason})" if reason else "")
            self.append(net, ch, {
                "ts": _now(), "kind": "event", "event": "kick", "glyph": "✕",
                "target": t, "target_ident": p.get("target_ident") or "*", "target_host": p.get("target_host") or "*",
                "by": by, "by_ident": p.get("by_ident") or "*", "by_host": p.get("by_host") or "*",
                "channel": ch, "reason": reason,
                "body": body,
            })
        elif kind == "mode":
            ch = p.get("channel")
            if not _is_channel(ch):
                return
            modes = list(p.get("modes") or [])
            flags = " ".join(str(x) for x in modes)
            by = p.get("by")
            body = f"mode {ch} {flags}" + (f" by {by}" if by else "")
            self.append(net, ch, {
                "ts": _now(), "kind": "event", "event": "mode", "glyph": "±",
                "channel": ch, "modes": modes,
                "by": by, "by_ident": p.get("by_ident") or "*", "by_host": p.get("by_host") or "*",
                "body": body,
            })
        elif kind == "topic":
            ch = p.get("channel")
            if not _is_channel(ch):
                return
            # Skip the synthetic "initial topic" event emitted on join (RPL_TOPIC).
            # It's already rendered in the topic bar; logging would clutter backlog
            # with a line on every reload.
            if p.get("initial"):
                return
            by = p.get("by")
            topic = p.get("topic")
            body = f"topic" + (f" by {by}" if by else "") + f": {topic or '(cleared)'}"
            self.append(net, ch, {
                "ts": _now(), "kind": "event", "event": "topic", "glyph": "≡",
                "channel": ch, "topic": topic, "by": by,
                "body": body,
            })
        elif kind == "nick":
            old = p.get("old") or ""
            new = p.get("new") or ""
            ident = p.get("ident") or "*"
            host = p.get("host") or "*"
            body = f"{old} is now known as {new}"
            entry = {
                "ts": _now(), "kind": "event", "event": "nick", "glyph": "↺",
                "old": old, "new": new, "ident": ident, "host": host,
                "body": body,
            }
            # Append to channel buffers (cheap — user may not be in all of them,
            # matches prior behavior). For PM buffers, log on the old nick and
            # carry the buffer forward under the new nick so the query persists
            # across the rename.
            for (n, ch) in list(self._store.keys()):
                if n != net:
                    continue
                if _is_channel(ch):
                    self.append(net, ch, dict(entry))
                elif ch == old:
                    self.append(net, ch, dict(entry))
                    if new and new != old:
                        buf = self._store.pop((net, old))
                        self._store[(net, new)] = buf
