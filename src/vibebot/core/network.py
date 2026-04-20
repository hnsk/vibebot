"""One IRC network connection. Wraps a pydle client and bridges events onto the EventBus."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from typing import Any

import pydle
import pydle.features
from pydle.features.ircv3.sasl import SASLSupport
from pydle.features.rfc1459 import RFC1459Support

from vibebot.config import (
    NetworkConfig,
    NickServAuthConfig,
    NoAuthConfig,
    QAuthConfig,
    RateLimitConfig,
    SaslAuthConfig,
    ServerConfig,
)
from vibebot.core.events import Event, EventBus
from vibebot.core.rate_limiter import BucketOverflow, TokenBucket
from vibebot.core.roster import ChannelRoster

log = logging.getLogger(__name__)


def _parse_mode_diff(
    modes: list[str], behaviour: dict[str, set[str]] | None
) -> list[tuple[str, str, str | None]]:
    """Parse a MODE param list into ``(direction, letter, arg|None)`` tuples.

    ``modes`` is the wire representation as pydle received it (e.g.
    ``["+o-v", "alice", "bob"]``). ``behaviour`` maps mode-type names
    (``"param"``, ``"param_set"``, ``"list"``, ``"noparam"``) to the sets of
    mode letters that fall into each category. Falls back to treating unknown
    letters as no-parameter.
    """
    from pydle.features.rfc1459 import protocol as rfc_protocol

    param_types = {
        rfc_protocol.BEHAVIOUR_PARAMETER,
        rfc_protocol.BEHAVIOUR_LIST,
    }
    param_on_set = rfc_protocol.BEHAVIOUR_PARAMETER_ON_SET
    items = list(modes)
    out: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(items):
        piece = items[i]
        add = True
        for ch in piece:
            if ch == "+":
                add = True
                continue
            if ch == "-":
                add = False
                continue
            mode_type = rfc_protocol.BEHAVIOUR_NO_PARAMETER
            if behaviour:
                for btype, letters in behaviour.items():
                    if ch in letters:
                        mode_type = btype
                        break
            needs_arg = mode_type in param_types or (
                mode_type == param_on_set and add
            )
            arg: str | None = None
            if needs_arg and i + 1 < len(items):
                arg = items.pop(i + 1)
            out.append(("+" if add else "-", ch, arg))
        i += 1
    return out


class _Client(pydle.Client, SASLSupport):  # type: ignore[misc, valid-type]
    """pydle client with protocol toggle, inline auth dispatch, and EventBus bridge."""

    # Disable pydle's internal auto-reconnect. NetworkConnection._run owns the
    # reconnect loop (fail-over chain + backoff); pydle's reconnect would race
    # it and, on DNS failure inside handle_forever's disconnect path, raise an
    # unhandled gaierror that kills the read task before our disconnect event
    # fires, wedging _run on _vb_disconnected.wait() forever.
    RECONNECT_ON_ERROR = False

    def __init__(
        self,
        *args: Any,
        network_name: str,
        bus: EventBus,
        autojoin: list[str],
        protocol: str,
        auth: Any,
        roster: ChannelRoster | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._network_name = network_name
        self._bus = bus
        self._autojoin = autojoin
        self._vb_protocol = protocol
        self._vb_auth = auth
        self._vb_roster = roster
        self._vb_sasl_failed = False
        self._vb_host_hidden = asyncio.Event()
        self._vb_disconnected = asyncio.Event()
        self._vb_expected_disconnect = False

    async def _publish(self, kind: str, **payload: Any) -> None:
        await self._bus.publish(Event(kind=kind, network=self._network_name, payload=payload))

    def _user_meta(self, nick: str | None) -> dict[str, str]:
        """Return {ident, host} for `nick` from pydle's user cache, falling
        back to '*' when the server has not advertised identity for this nick
        yet (pydle fills both from `ident@host` on any message/JOIN).

        Note: pydle's `self.users` is a `NormalizingDict` (MutableMapping), NOT
        a `dict` subclass — so we can't gate the lookup on `isinstance(_, dict)`.
        """
        if not nick:
            return {"ident": "*", "host": "*"}
        users = getattr(self, "users", None)
        if users is None:
            return {"ident": "*", "host": "*"}
        try:
            info = users.get(nick)
        except Exception:
            info = None
        if not info:
            return {"ident": "*", "host": "*"}
        ident = info.get("username") if hasattr(info, "get") else None
        host = info.get("hostname") if hasattr(info, "get") else None
        return {"ident": ident or "*", "host": host or "*"}

    # --- registration path ----------------------------------------------
    async def _register(self) -> None:  # type: ignore[override]
        if self._vb_protocol == "rfc1459":
            await RFC1459Support._register(self)
            return
        await super()._register()

    # --- SASL hooks -----------------------------------------------------
    async def _sasl_abort(self, timeout: bool = False) -> None:  # type: ignore[override]
        self._vb_sasl_failed = True
        await super()._sasl_abort(timeout=timeout)

    # --- connect flow ---------------------------------------------------
    async def on_connect(self) -> None:  # type: ignore[override]
        await super().on_connect()
        # Run auth + autojoin in a background task so we don't block pydle's read loop.
        asyncio.create_task(self._post_connect(), name=f"{self._network_name}:post_connect")

    async def _post_connect(self) -> None:
        auth = self._vb_auth
        auth_required = bool(getattr(auth, "required", False)) if not isinstance(auth, NoAuthConfig) else False

        if isinstance(auth, SaslAuthConfig) and auth_required and self._vb_sasl_failed:
            log.warning("%s: SASL required but failed → disconnecting", self._network_name)
            await self.disconnect(expected=True)
            return

        try:
            if isinstance(auth, QAuthConfig):
                await self._auth_q(auth)
            elif isinstance(auth, NickServAuthConfig):
                await self._auth_nickserv(auth)
        except Exception:
            log.exception("%s: auth dispatch failed", self._network_name)
            if auth_required:
                await self.disconnect(expected=True)
                return

        if self._vb_roster is not None and self.nickname:
            self._vb_roster.set_own_nick(self._network_name, self.nickname)
        for channel in self._autojoin:
            await self.join(channel)
        await self._publish("connect")

    async def _auth_q(self, auth: QAuthConfig) -> None:
        await self.message(auth.service, f"AUTH {auth.username} {auth.password}")
        if auth.hidehost:
            await self.rawmsg("MODE", self.nickname, "+x")
            if auth.wait_before_join:
                try:
                    await asyncio.wait_for(self._vb_host_hidden.wait(), timeout=auth.wait_timeout)
                except TimeoutError:
                    log.warning(
                        "%s: Q +x confirmation (396) timed out after %.1fs; joining anyway",
                        self._network_name,
                        auth.wait_timeout,
                    )

    async def _auth_nickserv(self, auth: NickServAuthConfig) -> None:
        cmd = auth.command_template.format(username=auth.username, password=auth.password)
        await self.message(auth.service_nick, cmd)

    # --- event bridge ---------------------------------------------------
    async def on_message(self, target: str, source: str, message: str) -> None:  # type: ignore[override]
        await self._publish("message", target=target, source=source, message=message)

    async def on_ctcp_action(self, by: str, target: str, contents: str) -> None:  # type: ignore[override]
        # pydle's CTCPSupport intercepts CTCP PRIVMSG before on_message fires.
        # Re-emit as a wrapped "message" event so the web UI's parseAction path
        # renders inbound /me just like outbound local echoes.
        await self._publish(
            "message",
            target=target,
            source=by,
            message=f"\x01ACTION {contents}\x01",
        )

    async def on_join(self, channel: str, user: str) -> None:  # type: ignore[override]
        meta = self._user_meta(user)
        if self._vb_roster is not None:
            if user == self.nickname:
                # Our own join → reset the channel and seed fresh state via /WHO.
                self._vb_roster.reset_channel(self._network_name, channel)
            else:
                self._vb_roster.upsert_user(
                    self._network_name, channel, user,
                    ident=meta["ident"], host=meta["host"],
                )
        await self._publish("join", channel=channel, user=user, ident=meta["ident"], host=meta["host"])
        # /WHO is our canonical source for channel membership + masks + user
        # modes. Runs once on bot join, per-channel; subsequent activity updates
        # the roster via JOIN/PART/QUIT/KICK/NICK/MODE events.
        if user == self.nickname:
            try:
                await self.rawmsg("WHO", channel)
            except Exception:
                log.exception("%s: failed to send WHO %s", self._network_name, channel)

    async def on_part(self, channel: str, user: str, message: str | None = None) -> None:  # type: ignore[override]
        meta = self._user_meta(user)
        if self._vb_roster is not None:
            if user == self.nickname:
                self._vb_roster.drop_channel(self._network_name, channel)
            else:
                self._vb_roster.remove_user(self._network_name, channel, user)
        await self._publish("part", channel=channel, user=user, ident=meta["ident"], host=meta["host"], message=message)

    async def on_quit(self, user: str, message: str | None = None) -> None:  # type: ignore[override]
        # pydle sync's user before firing on_quit, so ident/host are still
        # resolvable here; they disappear once _destroy_user runs afterwards.
        meta = self._user_meta(user)
        channels: list[str] = []
        if self._vb_roster is not None:
            channels = self._vb_roster.remove_user_all(self._network_name, user)
        await self._publish(
            "quit",
            user=user, ident=meta["ident"], host=meta["host"],
            message=message, channels=channels,
        )

    async def on_kick(
        self,
        channel: str,
        target: str,
        by: str | None,
        reason: str | None = None,
    ) -> None:  # type: ignore[override]
        tmeta = self._user_meta(target)
        bmeta = self._user_meta(by)
        if self._vb_roster is not None:
            if target == self.nickname:
                self._vb_roster.drop_channel(self._network_name, channel)
            else:
                self._vb_roster.remove_user(self._network_name, channel, target)
        await self._publish(
            "kick",
            channel=channel,
            target=target,
            target_ident=tmeta["ident"],
            target_host=tmeta["host"],
            by=by,
            by_ident=bmeta["ident"],
            by_host=bmeta["host"],
            reason=reason,
        )

    async def on_nick_change(self, old: str, new: str) -> None:  # type: ignore[override]
        # pydle synthesizes a NICK event on registration completion with
        # old=DEFAULT_NICKNAME ("<unregistered>") to transition from its
        # placeholder to the real nick. Don't surface that to the UI.
        if old == "<unregistered>":
            return
        meta = self._user_meta(new)
        channels: list[str] = []
        if self._vb_roster is not None:
            channels = self._vb_roster.rename_user(self._network_name, old, new)
        await self._publish(
            "nick",
            old=old, new=new, ident=meta["ident"], host=meta["host"],
            channels=channels,
        )

    async def on_notice(self, target: str, source: str, message: str) -> None:  # type: ignore[override]
        await self._publish("notice", target=target, source=source, message=message)

    async def on_mode_change(self, channel: str, modes: Any, by: str | None) -> None:  # type: ignore[override]
        bmeta = self._user_meta(by)
        # pydle has already applied the mode change to its channel state by the
        # time this fires. Mirror privilege-mode letters onto the roster so it
        # stays in sync without re-parsing raw MODE args here.
        if self._vb_roster is not None:
            self._vb_roster.sync_modes_from_client(self._network_name, channel, self)
        raw_modes = list(modes)
        parsed = _parse_mode_diff(raw_modes, getattr(self, "_channel_modes_behaviour", None))
        await self._publish(
            "mode",
            channel=channel,
            modes=raw_modes,
            modes_parsed=parsed,
            by=by,
            by_ident=bmeta["ident"],
            by_host=bmeta["host"],
        )

    async def on_ctcp(self, by: str, target: str, what: str, contents: str) -> None:  # type: ignore[override]
        # Fire for every CTCP request (including ACTION). The UI-facing
        # `message` event for ACTION is emitted separately by on_ctcp_action.
        await super().on_ctcp(by, target, what, contents)
        meta = self._user_meta(by)
        await self._publish(
            "ctcp",
            source=by,
            target=target,
            ctcp_type=(what or "").upper(),
            contents=contents or "",
            source_ident=meta["ident"],
            source_host=meta["host"],
        )

    async def on_topic_change(self, channel: str, message: str, by: str | None) -> None:  # type: ignore[override]
        await self._publish("topic", channel=channel, topic=message, by=by)

    async def on_raw_332(self, message: Any) -> None:  # type: ignore[override]
        # RPL_TOPIC on channel join — pydle's default writes channels[chan]["topic"]
        # but never surfaces an event. Publish one so the UI can show the topic
        # without a separate fetch.
        await super().on_raw_332(message)
        params = list(message.params)
        if len(params) >= 3:
            _, channel, topic = params[0], params[1], params[2]
            await self._publish("topic", channel=channel, topic=topic, by=None, initial=True)

    async def on_raw_396(self, message: Any) -> None:  # type: ignore[override]
        self._vb_host_hidden.set()
        await self._publish("host_hidden", params=list(message.params))

    async def on_raw_366(self, message: Any) -> None:  # type: ignore[override]
        # End of /NAMES. pydle's on_raw_353 has already populated the channel's
        # users + modes dict; emit an event so the UI refreshes its user list
        # now that initial @/+ status is known.
        await super().on_raw_366(message)
        params = list(message.params)
        channel = params[1] if len(params) >= 2 else None
        if channel:
            await self._publish("names", channel=channel)

    async def on_raw_352(self, message: Any) -> None:  # type: ignore[override]
        # RPL_WHOREPLY: <client> <channel> <ident> <host> <server> <nick> <flags> :<hopcount> <realname>
        # Canonical source for nick!ident@host on channel join — populates the roster.
        params = list(message.params)
        if len(params) < 8:
            return
        channel, ident, host = params[1], params[2], params[3]
        nick, flags, trailing = params[5], params[6] or "", params[7] or ""
        rest = trailing.split(" ", 1)
        realname = rest[1] if len(rest) > 1 else ""
        prefix_map = {sym: letter for sym, letter in (getattr(self, "_nickname_prefixes", {}) or {}).items()}
        modes: set[str] = set()
        for ch in flags:
            letter = prefix_map.get(ch)
            if letter:
                modes.add(letter)
        if self._vb_roster is not None:
            self._vb_roster.upsert_user(
                self._network_name, channel, nick,
                ident=ident, host=host, realname=realname, modes=modes,
            )
        await self._publish(
            "who_reply",
            channel=channel, nick=nick, ident=ident, host=host,
            realname=realname, modes=sorted(modes),
        )

    async def on_raw_315(self, message: Any) -> None:  # type: ignore[override]
        # RPL_ENDOFWHO: <client> <mask> :End of WHO list
        await super().on_raw_315(message)
        params = list(message.params)
        mask = params[1] if len(params) >= 2 else None
        await self._publish("who_end", mask=mask)
        if mask:
            await self._publish("roster", channel=mask, reason="who")

    async def _publish_numeric(self, message: Any) -> None:
        try:
            params = [p for p in (message.params or ()) if p is not None]
        except Exception:
            params = []
        await self._publish(
            "server_reply",
            source=getattr(message, "source", None),
            command=str(getattr(message, "command", "") or ""),
            params=params,
        )

    async def on_raw_221(self, message: Any) -> None:  # type: ignore[override]
        # RPL_UMODEIS — user mode echo (e.g. after MODE nick +i on connect).
        await self._publish_numeric(message)

    async def on_raw_338(self, message: Any) -> None:  # type: ignore[override]
        # RPL_WHOISACTUALLY — WHOIS actual host/IP line.
        await self._publish_numeric(message)

    async def on_raw_379(self, message: Any) -> None:  # type: ignore[override]
        # RPL_WHOISMODES — WHOIS user-modes line.
        await self._publish_numeric(message)

    async def on_unknown(self, message: Any) -> None:  # type: ignore[override]
        # pydle logs unhandled server replies (numerics like 461/421/401, plus
        # non-numeric server commands) and drops them. Bridge them onto the bus
        # so /raw output and server error replies surface in the UI.
        await super().on_unknown(message)
        await self._publish_numeric(message)

    async def on_isupport_modes(self, value: Any) -> None:  # type: ignore[override]
        # Ergo (and some other servers) advertise MODES with no value,
        # meaning "no limit". pydle's default int(value) raises on None.
        if value is None:
            self._mode_limit = None
            return
        try:
            self._mode_limit = int(value)
        except (TypeError, ValueError):
            self._mode_limit = None

    async def on_disconnect(self, expected: bool) -> None:  # type: ignore[override]
        # Signal + publish FIRST so _run's wait always wakes, even if super
        # raises. With RECONNECT_ON_ERROR=False, super just logs and returns,
        # but we guard anyway — losing the disconnect signal wedges _run.
        self._vb_expected_disconnect = bool(expected)
        self._vb_disconnected.set()
        try:
            await self._publish("disconnect", expected=expected)
        except Exception:
            log.exception("%s: disconnect publish failed", self._network_name)
        try:
            await super().on_disconnect(expected)
        except Exception:
            log.exception("%s: pydle on_disconnect raised", self._network_name)


async def _resolve_bind_address(
    value: str, server_host: str
) -> tuple[str, int] | None:
    """Turn a hostname/IP into a ``(ip, 0)`` tuple suitable for ``source_address``.

    IP literals (v4/v6) are returned unchanged. DNS names are resolved to an IP
    whose family matches the server's (so the kernel's ``bind()`` won't refuse
    the pair at connect time); if the server is dual-stack we prefer a matching
    family but fall back to the other. Returns ``None`` when resolution fails or
    no compatible IP is found — caller should then connect unbound.
    """
    try:
        ipaddress.ip_address(value)
        return (value, 0)
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        server_infos = await loop.getaddrinfo(
            server_host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        server_infos = []
    server_families = [info[0] for info in server_infos]
    preferred = server_families[0] if server_families else None

    try:
        host_infos = await loop.getaddrinfo(value, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not host_infos:
        return None

    if preferred is not None:
        for family, _type, _proto, _canon, sockaddr in host_infos:
            if family == preferred:
                return (sockaddr[0], 0)
    # No family match (or server family unknown) — take first host result.
    return (host_infos[0][4][0], 0)


class NetworkConnection:
    """Owns one IRC network connection.

    Invariant: at most one underlying pydle `_Client` exists at any time.
    Multi-server fail-over is strictly sequential inside `_run()` — each
    attempt builds a fresh client, connects, awaits disconnect, clears the
    reference, then the loop advances. Never parallel dials.
    """

    BACKOFF_START = 5.0
    BACKOFF_MAX = 300.0

    def __init__(
        self,
        config: NetworkConfig,
        bus: EventBus,
        *,
        roster: ChannelRoster | None = None,
    ) -> None:
        self.config = config
        self._bus = bus
        self._roster = roster
        self._task: asyncio.Task[None] | None = None
        self._client: _Client | None = None
        self._current_server: ServerConfig | None = None
        rl = config.rate_limit
        self._bucket = TokenBucket(
            burst=rl.burst,
            period=rl.period,
            enabled=rl.enabled,
        )

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.connected)

    @property
    def client(self) -> _Client | None:
        return self._client

    def current_server(self) -> ServerConfig | None:
        return self._current_server

    def _ordered_servers(self) -> list[ServerConfig]:
        """Default first, then remaining servers in declaration order."""
        default = next((s for s in self.config.servers if s.is_default), None)
        rest = [s for s in self.config.servers if s is not default]
        return [default, *rest] if default is not None else list(self.config.servers)

    def _build_client(self) -> _Client:
        cfg = self.config
        kwargs: dict[str, Any] = {
            "nickname": cfg.nick,
            "username": cfg.username or cfg.nick,
            "realname": cfg.realname or cfg.nick,
            "network_name": cfg.name,
            "bus": self._bus,
            "autojoin": list(cfg.channels),
            "protocol": cfg.protocol,
            "auth": cfg.auth,
            "roster": self._roster,
        }
        auth = cfg.auth
        if isinstance(auth, SaslAuthConfig):
            if cfg.protocol == "rfc1459":
                log.warning(
                    "%s: SASL auth requested but protocol=rfc1459 disables CAP; SASL will not run",
                    cfg.name,
                )
            kwargs["sasl_username"] = auth.username
            kwargs["sasl_password"] = auth.password
            kwargs["sasl_mechanism"] = auth.mechanism
            if auth.mechanism == "EXTERNAL" and auth.cert_path:
                kwargs["tls_client_cert"] = auth.cert_path
        return _Client(**kwargs)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=f"network:{self.config.name}")

    async def _run(self) -> None:
        backoff = self.BACKOFF_START
        try:
            while True:
                servers = self._ordered_servers()
                if not servers:
                    log.warning("%s: no servers configured; connection idle", self.config.name)
                    return
                clean_exit = False
                for server in servers:
                    self._current_server = server
                    self._client = self._build_client()
                    try:
                        log.info(
                            "%s: dialling %s:%s (tls=%s, default=%s)",
                            self.config.name, server.host, server.port, server.tls, server.is_default,
                        )
                        connect_kwargs: dict[str, Any] = {
                            "hostname": server.host,
                            "port": server.port,
                            "tls": server.tls,
                            "tls_verify": server.tls_verify,
                        }
                        if self.config.hostname:
                            bind = await _resolve_bind_address(
                                self.config.hostname, server.host
                            )
                            if bind is not None:
                                log.info(
                                    "%s: binding outbound socket to %s (from hostname %r)",
                                    self.config.name, bind[0], self.config.hostname,
                                )
                                connect_kwargs["source_address"] = bind
                            else:
                                log.warning(
                                    "%s: could not resolve hostname %r; connecting via default route",
                                    self.config.name, self.config.hostname,
                                )
                        await self._client.connect(**connect_kwargs)
                        # Successful dial → reset fail-over backoff so a later
                        # unexpected drop doesn't start at the previous ceiling.
                        backoff = self.BACKOFF_START
                        await self._client._vb_disconnected.wait()
                        if self._client._vb_expected_disconnect:
                            clean_exit = True
                            break  # stop() or intentional disconnect
                        # Unexpected drop → fall through to try next server,
                        # then outer while loop re-dials the chain after backoff.
                        log.warning(
                            "%s: unexpected disconnect from %s:%s; will try next server",
                            self.config.name, server.host, server.port,
                        )
                    except asyncio.CancelledError:
                        raise
                    except (TimeoutError, OSError, ConnectionError) as exc:
                        log.warning(
                            "%s: server %s:%s failed (%s), trying next",
                            self.config.name, server.host, server.port, exc,
                        )
                    except Exception:
                        log.warning(
                            "%s: server %s:%s failed, trying next",
                            self.config.name, server.host, server.port,
                            exc_info=True,
                        )
                    finally:
                        if self._client is not None:
                            with contextlib.suppress(Exception):
                                if self._client.connected:
                                    await self._client.disconnect(expected=True)
                        self._client = None
                        self._current_server = None

                if clean_exit:
                    return  # stop() or intentional disconnect

                log.warning(
                    "%s: all servers failed; backing off %.1fs",
                    self.config.name, backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2.0, self.BACKOFF_MAX)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Network %s run loop crashed", self.config.name)

    async def stop(self) -> None:
        client = self._client
        if client is not None and client.connected:
            with contextlib.suppress(Exception):
                await client.disconnect(expected=True)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._client = None
        self._current_server = None

    async def reconnect(self) -> None:
        """Tear down and respawn — safe because stop() awaits full teardown first."""
        await self.stop()
        await self.start()

    async def apply_identity(
        self,
        *,
        nick: str | None = None,
        username: str | None = None,
        realname: str | None = None,
        hostname: str | None = None,
    ) -> None:
        """Live-apply identity changes. nick fires NICK immediately; ident/realname
        require reconnect (noted in return flag; caller decides)."""
        if nick is not None and nick != self.config.nick:
            self.config.nick = nick
            if self._client is not None and self._client.connected:
                with contextlib.suppress(Exception):
                    await self._client.set_nickname(nick)
        if username is not None:
            self.config.username = username
        if realname is not None:
            self.config.realname = realname
        if hostname is not None:
            self.config.hostname = hostname or None

    async def apply_channels(self, desired: list[str]) -> None:
        """Diff active channels vs desired, PART extras, JOIN missing. Updates config."""
        desired_set = [c for c in desired if c]
        self.config.channels = list(desired_set)
        client = self._client
        if client is None or not client.connected:
            return
        current = set(client.channels.keys()) if hasattr(client, "channels") else set()
        target = set(desired_set)
        for ch in current - target:
            with contextlib.suppress(Exception):
                await client.part(ch)
        for ch in target - current:
            with contextlib.suppress(Exception):
                await client.join(ch)

    async def send_message(self, target: str, message: str) -> None:
        if self._client is None:
            raise RuntimeError(f"{self.config.name}: not connected")
        try:
            await self._bucket.acquire()
        except BucketOverflow as exc:
            await self._bus.publish(Event(
                kind="rate_limit_drop",
                network=self.config.name,
                payload={"target": target, "preview": message[:80], "reason": str(exc)},
            ))
            raise
        if message.startswith("\x01ACTION ") and message.endswith("\x01"):
            body = message[len("\x01ACTION "):-1]
            await self._client.ctcp(target, "ACTION", body)
            return
        await self._client.message(target, message)

    async def send_raw(self, command: str, *params: str) -> None:
        if self._client is None:
            raise RuntimeError(f"{self.config.name}: not connected")
        try:
            await self._bucket.acquire()
        except BucketOverflow as exc:
            await self._bus.publish(Event(
                kind="rate_limit_drop",
                network=self.config.name,
                payload={"command": command, "params": list(params), "reason": str(exc)},
            ))
            raise
        await self._client.rawmsg(command, *params)

    async def apply_rate_limit(self, rl: RateLimitConfig) -> None:
        """Apply a new RateLimitConfig live. Emits a warning event on disable."""
        was_enabled = self._bucket.enabled
        self._bucket.update(burst=rl.burst, period=rl.period, enabled=rl.enabled)
        self.config.rate_limit = rl
        if was_enabled and not rl.enabled:
            log.warning(
                "%s: outgoing rate limiting DISABLED — risk of server-side flood kill",
                self.config.name,
            )
            await self._bus.publish(Event(
                kind="rate_limit_disabled_warning",
                network=self.config.name,
                payload={"burst": rl.burst, "period": rl.period},
            ))

    async def join(self, channel: str) -> None:
        if self._client is None:
            raise RuntimeError(f"{self.config.name}: not connected")
        await self._client.join(channel)
        if channel not in self.config.channels:
            self.config.channels.append(channel)

    async def part(self, channel: str, reason: str | None = None) -> None:
        if self._client is None:
            raise RuntimeError(f"{self.config.name}: not connected")
        await self._client.part(channel, reason)
        if channel in self.config.channels:
            self.config.channels.remove(channel)

    def channel_users(self, channel: str) -> list[str]:
        if self._roster is not None:
            return sorted((u.nick for u in self._roster.users(self.config.name, channel)),
                          key=str.lower)
        if self._client is None:
            return []
        info = self._client.channels.get(channel)
        if not info:
            return []
        return sorted(info.get("users", set()))
