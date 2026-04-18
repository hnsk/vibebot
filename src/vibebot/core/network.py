"""One IRC network connection. Wraps a pydle client and bridges events onto the EventBus."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pydle

from vibebot.config import NetworkConfig
from vibebot.core.events import Event, EventBus

log = logging.getLogger(__name__)


# pydle.Client bundles RFC1459, IRCv3, TLS, CTCP, ISUPPORT, WHOX, account, etc.
# (No SASL feature in pydle 1.1 — SASL plaintext is handled via IRCv3 CAP negotiation by callers if needed.)
class _Client(pydle.Client):  # type: ignore[misc, valid-type]
    """pydle client that forwards events onto the shared event bus."""

    def __init__(
        self,
        *args: Any,
        network_name: str,
        bus: EventBus,
        autojoin: list[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._network_name = network_name
        self._bus = bus
        self._autojoin = autojoin

    async def _publish(self, kind: str, **payload: Any) -> None:
        await self._bus.publish(Event(kind=kind, network=self._network_name, payload=payload))

    async def on_connect(self) -> None:  # type: ignore[override]
        await super().on_connect()
        for channel in self._autojoin:
            await self.join(channel)
        await self._publish("connect")

    async def on_message(self, target: str, source: str, message: str) -> None:  # type: ignore[override]
        await self._publish("message", target=target, source=source, message=message)

    async def on_join(self, channel: str, user: str) -> None:  # type: ignore[override]
        await self._publish("join", channel=channel, user=user)

    async def on_part(self, channel: str, user: str, message: str | None = None) -> None:  # type: ignore[override]
        await self._publish("part", channel=channel, user=user, message=message)

    async def on_kick(
        self,
        channel: str,
        target: str,
        by: str | None,
        reason: str | None = None,
    ) -> None:  # type: ignore[override]
        await self._publish("kick", channel=channel, target=target, by=by, reason=reason)

    async def on_nick_change(self, old: str, new: str) -> None:  # type: ignore[override]
        await self._publish("nick", old=old, new=new)


class NetworkConnection:
    """Owns one IRC network connection and its background task."""

    def __init__(self, config: NetworkConfig, bus: EventBus) -> None:
        self.config = config
        self._bus = bus
        self._task: asyncio.Task[None] | None = None
        self._client = _Client(
            nickname=config.nick,
            username=config.username or config.nick,
            realname=config.realname or config.nick,
            network_name=config.name,
            bus=bus,
            autojoin=list(config.channels),
        )

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
                tls_verify=True,
            )
            await self._client.handle_forever()
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
        await self._client.message(target, message)

    async def join(self, channel: str) -> None:
        await self._client.join(channel)

    async def part(self, channel: str, reason: str | None = None) -> None:
        await self._client.part(channel, reason)

    def channel_users(self, channel: str) -> list[str]:
        info = self._client.channels.get(channel)
        if not info:
            return []
        return sorted(info.get("users", set()))
