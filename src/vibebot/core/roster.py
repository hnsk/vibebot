"""Authoritative per-channel user state.

Single source of truth for membership, nick!ident@host, and per-channel user
modes, independent of the pydle client's internal caches. Populated via /WHO
on bot join and kept in sync from JOIN/PART/QUIT/KICK/NICK/MODE events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _key(nick: str | None) -> str:
    return (nick or "").lower()


@dataclass
class RosterUser:
    nick: str
    ident: str = "*"
    host: str = "*"
    realname: str = ""
    account: str = "*"
    modes: set[str] = field(default_factory=set)

    def mask(self) -> str:
        return f"{self.nick}!{self.ident}@{self.host}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "nick": self.nick,
            "ident": self.ident,
            "host": self.host,
            "realname": self.realname,
            "account": self.account,
            "modes": sorted(self.modes),
        }


class ChannelRoster:
    """Per-network, per-channel roster of RosterUser entries."""

    def __init__(self) -> None:
        self._channels: dict[str, dict[str, dict[str, RosterUser]]] = {}
        self._own_nick: dict[str, str] = {}

    # --- queries --------------------------------------------------------
    def channels(self, network: str) -> list[str]:
        return list(self._channels.get(network, {}).keys())

    def users(self, network: str, channel: str) -> list[RosterUser]:
        return list(self._channels.get(network, {}).get(channel.lower(), {}).values())

    def get_user(self, network: str, channel: str, nick: str) -> RosterUser | None:
        return self._channels.get(network, {}).get(channel.lower(), {}).get(_key(nick))

    def find_user(self, network: str, nick: str) -> RosterUser | None:
        for chan_users in self._channels.get(network, {}).values():
            u = chan_users.get(_key(nick))
            if u is not None:
                return u
        return None

    def channels_for(self, network: str, nick: str) -> list[str]:
        key = _key(nick)
        return [ch for ch, users in self._channels.get(network, {}).items() if key in users]

    def own_nick(self, network: str) -> str | None:
        return self._own_nick.get(network)

    # --- mutations ------------------------------------------------------
    def set_own_nick(self, network: str, nick: str) -> None:
        self._own_nick[network] = nick

    def ensure_channel(self, network: str, channel: str) -> dict[str, RosterUser]:
        return self._channels.setdefault(network, {}).setdefault(channel.lower(), {})

    def reset_channel(self, network: str, channel: str) -> None:
        self._channels.setdefault(network, {})[channel.lower()] = {}

    def drop_channel(self, network: str, channel: str) -> None:
        self._channels.get(network, {}).pop(channel.lower(), None)

    def upsert_user(
        self,
        network: str,
        channel: str,
        nick: str,
        *,
        ident: str | None = None,
        host: str | None = None,
        realname: str | None = None,
        account: str | None = None,
        modes: set[str] | None = None,
    ) -> RosterUser:
        users = self.ensure_channel(network, channel)
        u = users.get(_key(nick))
        if u is None:
            u = RosterUser(nick=nick)
            users[_key(nick)] = u
        u.nick = nick
        if ident and ident != "*":
            u.ident = ident
        if host and host != "*":
            u.host = host
        if realname is not None:
            u.realname = realname
        if account is not None:
            u.account = account
        if modes is not None:
            u.modes = set(modes)
        return u

    def remove_user(self, network: str, channel: str, nick: str) -> None:
        users = self._channels.get(network, {}).get(channel.lower())
        if users is not None:
            users.pop(_key(nick), None)

    def remove_user_all(self, network: str, nick: str) -> list[str]:
        key = _key(nick)
        out: list[str] = []
        for chan, users in self._channels.get(network, {}).items():
            if users.pop(key, None) is not None:
                out.append(chan)
        return out

    def rename_user(self, network: str, old: str, new: str) -> list[str]:
        old_k, new_k = _key(old), _key(new)
        out: list[str] = []
        for chan, users in self._channels.get(network, {}).items():
            u = users.pop(old_k, None)
            if u is None:
                continue
            u.nick = new
            users[new_k] = u
            out.append(chan)
        if self._own_nick.get(network, "").lower() == old_k:
            self._own_nick[network] = new
        return out

    def sync_modes_from_client(self, network: str, channel: str, client: Any) -> None:
        """Copy per-user mode letters from pydle's already-parsed channel state.

        pydle's `channels[chan]["modes"]` maps mode letter → set of nicks for
        privilege modes (o/v/h/q/a). After pydle's on_mode_change fires the
        dict is authoritative; mirror it into the roster so consumers never
        read stale state."""
        ch_info = getattr(client, "channels", {}).get(channel) or {}
        mode_map = ch_info.get("modes", {}) or {}
        users = self._channels.get(network, {}).get(channel.lower())
        if not users:
            return
        per_user: dict[str, set[str]] = {k: set() for k in users}
        for letter, holders in mode_map.items():
            if not isinstance(letter, str) or len(letter) != 1:
                continue
            for nick in holders or ():
                k = _key(nick)
                if k in per_user:
                    per_user[k].add(letter)
        for k, u in users.items():
            u.modes = per_user[k]

    def clear_network(self, network: str) -> None:
        self._channels.pop(network, None)
        self._own_nick.pop(network, None)
