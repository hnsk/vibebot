"""One IRC network connection. Wraps a pydle client and bridges events onto the EventBus."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    SaslAuthConfig,
)
from vibebot.core.events import Event, EventBus

log = logging.getLogger(__name__)


class _Client(pydle.Client, SASLSupport):  # type: ignore[misc, valid-type]
    """pydle client with protocol toggle, inline auth dispatch, and EventBus bridge."""

    def __init__(
        self,
        *args: Any,
        network_name: str,
        bus: EventBus,
        autojoin: list[str],
        protocol: str,
        auth: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._network_name = network_name
        self._bus = bus
        self._autojoin = autojoin
        self._vb_protocol = protocol
        self._vb_auth = auth
        self._vb_sasl_failed = False
        self._vb_host_hidden = asyncio.Event()
        self._vb_disconnected = asyncio.Event()

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
                except asyncio.TimeoutError:
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
        await self._publish("join", channel=channel, user=user, ident=meta["ident"], host=meta["host"])
        # When we join a channel, servers send NAMES as part of the JOIN response,
        # but explicitly requesting it guarantees we get a fresh list and lets
        # the UI refresh once RPL_ENDOFNAMES (366) arrives.
        if user == self.nickname:
            try:
                await self.rawmsg("NAMES", channel)
            except Exception:
                log.exception("%s: failed to send NAMES %s", self._network_name, channel)

    async def on_part(self, channel: str, user: str, message: str | None = None) -> None:  # type: ignore[override]
        meta = self._user_meta(user)
        await self._publish("part", channel=channel, user=user, ident=meta["ident"], host=meta["host"], message=message)

    async def on_quit(self, user: str, message: str | None = None) -> None:  # type: ignore[override]
        # pydle sync's user before firing on_quit, so ident/host are still
        # resolvable here; they disappear once _destroy_user runs afterwards.
        meta = self._user_meta(user)
        await self._publish("quit", user=user, ident=meta["ident"], host=meta["host"], message=message)

    async def on_kick(
        self,
        channel: str,
        target: str,
        by: str | None,
        reason: str | None = None,
    ) -> None:  # type: ignore[override]
        tmeta = self._user_meta(target)
        bmeta = self._user_meta(by)
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
        await self._publish("nick", old=old, new=new, ident=meta["ident"], host=meta["host"])

    async def on_notice(self, target: str, source: str, message: str) -> None:  # type: ignore[override]
        await self._publish("notice", target=target, source=source, message=message)

    async def on_mode_change(self, channel: str, modes: Any, by: str | None) -> None:  # type: ignore[override]
        bmeta = self._user_meta(by)
        await self._publish(
            "mode",
            channel=channel,
            modes=list(modes),
            by=by,
            by_ident=bmeta["ident"],
            by_host=bmeta["host"],
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

    async def on_unknown(self, message: Any) -> None:  # type: ignore[override]
        # pydle logs unhandled server replies (numerics like 461/421/401, plus
        # non-numeric server commands) and drops them. Bridge them onto the bus
        # so /raw output and server error replies surface in the UI.
        await super().on_unknown(message)
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
        await super().on_disconnect(expected)
        self._vb_disconnected.set()


class NetworkConnection:
    """Owns one IRC network connection and its background task."""

    def __init__(self, config: NetworkConfig, bus: EventBus) -> None:
        self.config = config
        self._bus = bus
        self._task: asyncio.Task[None] | None = None

        client_kwargs: dict[str, Any] = {
            "nickname": config.nick,
            "username": config.username or config.nick,
            "realname": config.realname or config.nick,
            "network_name": config.name,
            "bus": bus,
            "autojoin": list(config.channels),
            "protocol": config.protocol,
            "auth": config.auth,
        }

        auth = config.auth
        if isinstance(auth, SaslAuthConfig):
            if config.protocol == "rfc1459":
                log.warning(
                    "%s: SASL auth requested but protocol=rfc1459 disables CAP; SASL will not run",
                    config.name,
                )
            client_kwargs["sasl_username"] = auth.username
            client_kwargs["sasl_password"] = auth.password
            client_kwargs["sasl_mechanism"] = auth.mechanism
            if auth.mechanism == "EXTERNAL" and auth.cert_path:
                client_kwargs["tls_client_cert"] = auth.cert_path

        self._client = _Client(**client_kwargs)

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        return bool(self._client.connected)

    @property
    def client(self) -> _Client:
        return self._client

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"network:{self.config.name}")

    async def _run(self) -> None:
        try:
            await self._client.connect(
                hostname=self.config.host,
                port=self.config.port,
                tls=self.config.tls,
                tls_verify=self.config.tls_verify,
            )
            # pydle.connect() already spawns handle_forever() as a background
            # task; awaiting it again here creates a second reader on the same
            # StreamReader and raises "readuntil() called while another
            # coroutine is already waiting for incoming data".
            await self._client._vb_disconnected.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Network %s connection failed", self.config.name)

    async def stop(self) -> None:
        if self._client.connected:
            try:
                await self._client.disconnect(expected=True)
            except Exception:
                log.exception("Error disconnecting from %s", self.config.name)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def send_message(self, target: str, message: str) -> None:
        if message.startswith("\x01ACTION ") and message.endswith("\x01"):
            body = message[len("\x01ACTION "):-1]
            await self._client.ctcp(target, "ACTION", body)
            return
        await self._client.message(target, message)

    async def send_raw(self, command: str, *params: str) -> None:
        await self._client.rawmsg(command, *params)

    async def join(self, channel: str) -> None:
        await self._client.join(channel)

    async def part(self, channel: str, reason: str | None = None) -> None:
        await self._client.part(channel, reason)

    def channel_users(self, channel: str) -> list[str]:
        info = self._client.channels.get(channel)
        if not info:
            return []
        return sorted(info.get("users", set()))
