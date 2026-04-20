"""Runtime mutation of bot configuration with optional persistence.

Single-writer service: all mutations serialize behind an asyncio.Lock so
concurrent API calls cannot race the `bot.networks` dict or the live
`Config` object.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibebot.config import (
    Config,
    ConfigWriteError,
    NetworkConfig,
    RateLimitConfig,
    ServerConfig,
    load_config,
    save_config,
)
from vibebot.core.events import Event
from vibebot.core.network import NetworkConnection

if TYPE_CHECKING:
    from vibebot.core.bot import VibeBot

log = logging.getLogger(__name__)


class SettingsError(Exception):
    """Raised for user-correctable input errors (unknown network, etc.)."""


_NON_DIRTY_EVENTS = frozenset({
    "connect_requested",
    "disconnect_requested",
    "reconnect_requested",
    "config_saved",
    "config_reloaded",
})


class SettingsService:
    def __init__(self, bot: VibeBot, config_path: Path | None) -> None:
        self._bot = bot
        self._config_path = config_path
        self._lock = asyncio.Lock()
        self._dirty = False

    # ---------------- snapshots ----------------

    def snapshot(self) -> dict[str, Any]:
        cfg = self._bot.config
        return {
            "bot": cfg.bot.model_dump(),
            "api": cfg.api.model_dump(),
            "networks": [self._network_snapshot(n.name) for n in cfg.networks],
            "repos": [r.model_dump() for r in cfg.repos],
            "config_path": str(self._config_path) if self._config_path else None,
            "dirty": self._dirty,
        }

    def _network_snapshot(self, name: str) -> dict[str, Any]:
        cfg = self._find_network(name)
        conn = self._bot.networks.get(name)
        current = conn.current_server() if conn else None
        return {
            **cfg.model_dump(),
            "connected": bool(conn and conn.connected),
            "current_server": current.model_dump() if current else None,
        }

    # ---------------- network CRUD ----------------

    async def add_network(self, network: NetworkConfig) -> None:
        async with self._lock:
            if any(n.name == network.name for n in self._bot.config.networks):
                raise SettingsError(f"network {network.name!r} already exists")
            self._bot.config.networks.append(network)
            conn = NetworkConnection(network, self._bot.bus, roster=self._bot.roster)
            self._bot.networks[network.name] = conn
            await conn.start()
            await self._notify("network_added", name=network.name)

    async def remove_network(self, name: str) -> None:
        async with self._lock:
            self._find_network(name)
            conn = self._bot.networks.pop(name, None)
            if conn is not None:
                await conn.stop()
            self._bot.config.networks = [n for n in self._bot.config.networks if n.name != name]
            await self._notify("network_removed", name=name)

    async def update_network(
        self,
        name: str,
        *,
        nick: str | None = None,
        username: str | None = None,
        realname: str | None = None,
        hostname: str | None = None,
        protocol: str | None = None,
        auth: dict[str, Any] | None = None,
        rate_limit: dict[str, Any] | None = None,
        reconnect: bool = False,
    ) -> NetworkConfig:
        async with self._lock:
            cfg = self._find_network(name)
            conn = self._bot.networks.get(name)

            needs_reconnect = False

            if nick is not None and conn is not None:
                await conn.apply_identity(nick=nick)
            elif nick is not None:
                cfg.nick = nick

            if username is not None and username != cfg.username:
                cfg.username = username or None
                needs_reconnect = True
            if realname is not None and realname != cfg.realname:
                cfg.realname = realname or None
                needs_reconnect = True
            if hostname is not None and hostname != cfg.hostname:
                cfg.hostname = hostname or None
                needs_reconnect = True
            if protocol is not None and protocol != cfg.protocol:
                cfg.protocol = protocol  # type: ignore[assignment]
                needs_reconnect = True
            if auth is not None:
                new_auth = _coerce_auth(auth)
                cfg.auth = new_auth
                needs_reconnect = True
            if rate_limit is not None:
                try:
                    new_rl = RateLimitConfig.model_validate(rate_limit)
                except Exception as exc:
                    raise SettingsError(f"invalid rate_limit: {exc}") from exc
                if conn is not None:
                    await conn.apply_rate_limit(new_rl)
                else:
                    cfg.rate_limit = new_rl

            if reconnect and needs_reconnect and conn is not None and conn.connected:
                await conn.reconnect()

            await self._notify("network_updated", name=name)
            return cfg

    # ---------------- servers within network ----------------

    async def add_server(
        self,
        network: str,
        server: ServerConfig,
        index: int | None = None,
    ) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            if server.is_default:
                for s in cfg.servers:
                    s.is_default = False
            if index is None:
                cfg.servers.append(server)
            else:
                cfg.servers.insert(index, server)
            if not any(s.is_default for s in cfg.servers):
                cfg.servers[0].is_default = True
            await self._notify("server_added", name=network)

    async def update_server(
        self,
        network: str,
        index: int,
        server: ServerConfig,
    ) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            if not 0 <= index < len(cfg.servers):
                raise SettingsError(f"server index {index} out of range for {network!r}")
            cfg.servers[index] = server
            defaults = [s for s in cfg.servers if s.is_default]
            if len(defaults) != 1:
                for i, s in enumerate(cfg.servers):
                    s.is_default = i == 0
            await self._notify("server_updated", name=network)

    async def remove_server(self, network: str, index: int) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            if not 0 <= index < len(cfg.servers):
                raise SettingsError(f"server index {index} out of range for {network!r}")
            removed = cfg.servers.pop(index)
            if removed.is_default and cfg.servers:
                cfg.servers[0].is_default = True
            await self._notify("server_removed", name=network)

    async def set_default_server(self, network: str, index: int) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            if not 0 <= index < len(cfg.servers):
                raise SettingsError(f"server index {index} out of range for {network!r}")
            for i, s in enumerate(cfg.servers):
                s.is_default = i == index
            await self._notify("server_default_set", name=network)

    # ---------------- channels within network ----------------

    async def add_channel(self, network: str, channel: str) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            channel = channel.strip()
            if not channel:
                raise SettingsError("channel name required")
            if channel[0] not in "#&!+":
                channel = "#" + channel
            if channel not in cfg.channels:
                cfg.channels.append(channel)
            conn = self._bot.networks.get(network)
            if conn is not None and conn.connected:
                await conn.apply_channels(list(cfg.channels))
            await self._notify("channel_added", name=network, channel=channel)

    async def remove_channel(self, network: str, channel: str) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            if channel not in cfg.channels:
                raise SettingsError(f"{network!r} has no channel {channel!r}")
            cfg.channels.remove(channel)
            conn = self._bot.networks.get(network)
            if conn is not None and conn.connected:
                await conn.apply_channels(list(cfg.channels))
            await self._notify("channel_removed", name=network, channel=channel)

    # ---------------- connection lifecycle ----------------

    async def connect(self, network: str) -> None:
        async with self._lock:
            cfg = self._find_network(network)
            conn = self._bot.networks.get(network)
            if conn is None:
                conn = NetworkConnection(cfg, self._bot.bus, roster=self._bot.roster)
                self._bot.networks[network] = conn
            await conn.start()
            await self._notify("connect_requested", name=network)

    async def disconnect(self, network: str) -> None:
        async with self._lock:
            self._find_network(network)
            conn = self._bot.networks.get(network)
            if conn is not None:
                await conn.stop()
            await self._notify("disconnect_requested", name=network)

    async def reconnect(self, network: str) -> None:
        async with self._lock:
            self._find_network(network)
            conn = self._bot.networks.get(network)
            if conn is None:
                raise SettingsError(f"{network!r} is not running")
            await conn.reconnect()
            await self._notify("reconnect_requested", name=network)

    # ---------------- persistence ----------------

    async def save_to_disk(self) -> None:
        async with self._lock:
            if self._config_path is None:
                raise SettingsError("no config path bound; cannot persist")
            try:
                save_config(self._config_path, self._bot.config)
            except ConfigWriteError as exc:
                raise SettingsError(str(exc)) from exc
            await self._notify("config_saved", path=str(self._config_path))

    async def reload_from_disk(self) -> None:
        async with self._lock:
            if self._config_path is None:
                raise SettingsError("no config path bound; cannot reload")
            try:
                new_cfg = load_config(self._config_path)
            except Exception as exc:
                raise SettingsError(f"failed to reload config: {exc}") from exc
            await self._reconcile_networks(new_cfg)
            self._bot.config = new_cfg
            await self._notify("config_reloaded", path=str(self._config_path))
            await self.warn_disabled_rate_limits()

    async def _reconcile_networks(self, new_cfg: Config) -> None:
        """Apply `new_cfg.networks` to the live runtime.

        Removed networks are stopped and dropped. Added networks are spawned.
        Networks present in both are stopped, re-created against the new cfg,
        and restarted — simplest semantics for "revert to disk".
        """
        old_names = {n.name for n in self._bot.config.networks}
        new_by_name = {n.name: n for n in new_cfg.networks}

        for name in old_names - new_by_name.keys():
            conn = self._bot.networks.pop(name, None)
            if conn is not None:
                await conn.stop()

        for name, net_cfg in new_by_name.items():
            existing = self._bot.networks.get(name)
            if existing is not None:
                await existing.stop()
                self._bot.networks.pop(name, None)
            conn = NetworkConnection(net_cfg, self._bot.bus, roster=self._bot.roster)
            self._bot.networks[name] = conn
            await conn.start()

    # ---------------- helpers ----------------

    def _find_network(self, name: str) -> NetworkConfig:
        for n in self._bot.config.networks:
            if n.name == name:
                return n
        raise SettingsError(f"unknown network {name!r}")

    async def warn_disabled_rate_limits(self) -> None:
        """Log + publish a warning for each network that has rate limiting disabled."""
        for net in self._bot.config.networks:
            if not net.rate_limit.enabled:
                log.warning(
                    "%s: outgoing rate limiting is DISABLED in config — risk of server-side flood kill",
                    net.name,
                )
                await self._bot.bus.publish(Event(
                    kind="rate_limit_disabled_warning",
                    network=net.name,
                    payload={"burst": net.rate_limit.burst, "period": net.rate_limit.period},
                ))

    async def _notify(self, kind: str, **payload: Any) -> None:
        if kind in ("config_saved", "config_reloaded"):
            self._dirty = False
        elif kind not in _NON_DIRTY_EVENTS:
            self._dirty = True
        try:
            await self._bot.bus.publish(
                Event(kind="settings_changed", network=payload.get("name"), payload={"event": kind, **payload})
            )
        except Exception:
            log.exception("failed to publish settings_changed event")


_AUTH_METHODS = {"none", "sasl", "q", "nickserv"}


def _coerce_auth(data: dict[str, Any]):
    """Validate an auth dict against the discriminated union."""
    from vibebot.config import (
        NickServAuthConfig,
        NoAuthConfig,
        QAuthConfig,
        SaslAuthConfig,
    )

    method = data.get("method")
    if method not in _AUTH_METHODS:
        raise SettingsError(f"unknown auth method {method!r}")
    if method == "none":
        return NoAuthConfig.model_validate(data)
    if method == "sasl":
        return SaslAuthConfig.model_validate(data)
    if method == "q":
        return QAuthConfig.model_validate(data)
    return NickServAuthConfig.model_validate(data)
