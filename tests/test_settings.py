"""Tests for config round-trip, SettingsService, and settings API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vibebot.api.app import build_app
from vibebot.config import (
    ApiConfig,
    BotConfig,
    Config,
    ConfigWriteError,
    NetworkConfig,
    RateLimitConfig,
    ServerConfig,
    load_config,
    save_config,
)
from vibebot.core.bot import VibeBot
from vibebot.core.events import Event
from vibebot.core.network import NetworkConnection
from vibebot.core.settings import SettingsError

TOKEN = "t"


def _make_config() -> Config:
    return Config(
        bot=BotConfig(database=":memory:"),
        api=ApiConfig(tokens=[TOKEN]),
        networks=[
            NetworkConfig(
                name="net1",
                nick="bot",
                servers=[
                    ServerConfig(host="a.example", port=6697, tls=True, is_default=True),
                    ServerConfig(host="b.example", port=6697, tls=True),
                ],
                channels=["#a"],
            )
        ],
    )


# ---------------- config round-trip ----------------

def test_save_config_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "cfg.toml"
    src.write_text(
        "# sentinel comment\n[bot]\ndatabase = 'x'\n\n"
        "[api]\nhost = '127.0.0.1'\nport = 8080\ntokens = ['t']\n\n"
        "[[networks]]\nname = 'net1'\nnick = 'bot'\n\n"
        "[[networks.servers]]\nhost = 'a.example'\nport = 6697\ntls = true\nis_default = true\n",
        encoding="utf-8",
    )
    cfg = load_config(src)
    save_config(src, cfg)
    reloaded = load_config(src)
    assert reloaded == cfg
    assert "# sentinel comment" in src.read_text(encoding="utf-8")


def test_save_config_readonly_raises(tmp_path: Path) -> None:
    import os
    import stat

    src = tmp_path / "cfg.toml"
    save_config(src, _make_config())
    os.chmod(src, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        with pytest.raises(ConfigWriteError):
            save_config(src, _make_config())
    finally:
        os.chmod(src, stat.S_IRUSR | stat.S_IWUSR)


def test_save_config_rename_ebusy_falls_back_to_inplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    src = tmp_path / "cfg.toml"
    save_config(src, _make_config())

    real_replace = Path.replace

    def boom(self: Path, target: Any) -> None:
        if str(self).endswith(".tmp"):
            raise OSError(errno.EBUSY, "simulated bind-mount EBUSY")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", boom)
    cfg = _make_config()
    cfg.networks[0].nick = "changed"
    save_config(src, cfg)
    reloaded = load_config(src)
    assert reloaded.networks[0].nick == "changed"
    assert not (tmp_path / "cfg.toml.tmp").exists()


async def test_settings_save_readonly_returns_settings_error(tmp_path: Path) -> None:
    import os
    import stat

    path = tmp_path / "cfg.toml"
    bot = VibeBot(_make_config(), config_path=path)
    await bot.settings.save_to_disk()
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    try:
        with pytest.raises(SettingsError):
            await bot.settings.save_to_disk()
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def test_legacy_network_migrates() -> None:
    cfg = Config.model_validate(
        {
            "networks": [
                {"name": "n", "host": "h", "port": 6667, "tls": False, "nick": "bot"},
            ]
        }
    )
    net = cfg.networks[0]
    assert len(net.servers) == 1
    assert net.servers[0].host == "h"
    assert net.servers[0].is_default is True


def test_two_defaults_rejected() -> None:
    with pytest.raises(Exception):
        NetworkConfig(
            name="x",
            nick="bot",
            servers=[
                ServerConfig(host="a", is_default=True),
                ServerConfig(host="b", is_default=True),
            ],
        )


def test_ordered_servers_default_first() -> None:
    cfg = NetworkConfig(
        name="x",
        nick="bot",
        servers=[
            ServerConfig(host="a"),
            ServerConfig(host="b", is_default=True),
            ServerConfig(host="c"),
        ],
    )
    conn = NetworkConnection.__new__(NetworkConnection)
    conn.config = cfg
    ordered = [s.host for s in conn._ordered_servers()]
    assert ordered == ["b", "a", "c"]


# ---------------- settings service ----------------

class _FakeNetwork:
    """Stand-in for NetworkConnection that records lifecycle calls."""

    def __init__(self, config: NetworkConfig, bus: Any, *, roster: Any = None) -> None:
        self.config = config
        self._bus = bus
        self._roster = roster
        self.started = False
        self.stopped = False
        self.identity_calls: list[dict[str, Any]] = []
        self.channels_applied: list[list[str]] = []

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def connected(self) -> bool:
        return self.started and not self.stopped

    @property
    def client(self) -> Any:
        return None

    def current_server(self) -> Any:
        if not self.started or not self.config.servers:
            return None
        return self.config.servers[0]

    async def start(self) -> None:
        self.started = True
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    async def apply_identity(self, **kw: Any) -> None:
        self.identity_calls.append(kw)

    async def apply_channels(self, desired: list[str]) -> None:
        self.channels_applied.append(list(desired))

    async def apply_rate_limit(self, rl: Any) -> None:
        self.rate_limit_calls = getattr(self, "rate_limit_calls", [])
        self.rate_limit_calls.append(rl)
        self.config.rate_limit = rl


@pytest.fixture
def bot_with_fake_network(monkeypatch: pytest.MonkeyPatch):
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    cfg = _make_config()
    bot = VibeBot(cfg, config_path=None)
    fake = _FakeNetwork(cfg.networks[0], bot.bus, roster=bot.roster)
    bot.networks[cfg.networks[0].name] = fake
    return bot, fake


async def test_settings_add_channel_live(bot_with_fake_network) -> None:
    bot, fake = bot_with_fake_network
    await fake.start()
    await bot.settings.add_channel("net1", "#new")
    assert "#new" in bot.config.networks[0].channels
    assert fake.channels_applied[-1] == ["#a", "#new"]


async def test_settings_remove_channel(bot_with_fake_network) -> None:
    bot, fake = bot_with_fake_network
    await fake.start()
    await bot.settings.remove_channel("net1", "#a")
    assert "#a" not in bot.config.networks[0].channels


async def test_settings_add_remove_server(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    await bot.settings.add_server(
        "net1", ServerConfig(host="c.example", port=6697, tls=True, is_default=False)
    )
    assert len(bot.config.networks[0].servers) == 3
    await bot.settings.remove_server("net1", 2)
    assert len(bot.config.networks[0].servers) == 2


async def test_settings_remove_last_server_leaves_empty(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    bot.config.networks[0].servers = [bot.config.networks[0].servers[0]]
    bot.config.networks[0].servers[0].is_default = True
    await bot.settings.remove_server("net1", 0)
    assert bot.config.networks[0].servers == []
    snap = bot.settings._network_snapshot("net1")
    assert snap["servers"] == []


async def test_settings_add_channel_autoprefixes(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    await bot.settings.add_channel("net1", "foo")
    assert "#foo" in bot.config.networks[0].channels
    await bot.settings.add_channel("net1", "!quux")
    await bot.settings.add_channel("net1", "&local")
    await bot.settings.add_channel("net1", "+mode")
    chans = bot.config.networks[0].channels
    assert "!quux" in chans
    assert "&local" in chans
    assert "+mode" in chans


async def test_settings_set_default_server(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    await bot.settings.set_default_server("net1", 1)
    servers = bot.config.networks[0].servers
    assert servers[0].is_default is False
    assert servers[1].is_default is True


async def test_settings_disconnect_and_connect(bot_with_fake_network) -> None:
    bot, fake = bot_with_fake_network
    await fake.start()
    await bot.settings.disconnect("net1")
    assert fake.stopped is True
    await bot.settings.connect("net1")
    assert bot.networks["net1"].started is True


async def test_settings_reconnect_without_changes(bot_with_fake_network) -> None:
    bot, fake = bot_with_fake_network
    await fake.start()
    assert fake.stopped is False
    await bot.settings.reconnect("net1")
    # _FakeNetwork.reconnect cycles stop→start; final state is started again.
    assert fake.started is True


async def test_settings_update_rate_limit_applies_live(bot_with_fake_network) -> None:
    bot, fake = bot_with_fake_network
    await fake.start()
    await bot.settings.update_network(
        "net1", rate_limit={"enabled": False, "burst": 3, "period": 1.5}
    )
    assert fake.rate_limit_calls, "apply_rate_limit must be invoked"
    applied = fake.rate_limit_calls[-1]
    assert applied.enabled is False
    assert applied.burst == 3
    assert applied.period == 1.5


async def test_settings_update_rate_limit_rejects_invalid(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    with pytest.raises(SettingsError):
        await bot.settings.update_network("net1", rate_limit={"burst": 0})


async def test_warn_disabled_rate_limits_fires_event(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    bot.config.networks[0].rate_limit = RateLimitConfig(enabled=False, burst=5, period=2.0)
    warnings: list[Event] = []

    async def _collect(event: Event) -> None:
        warnings.append(event)

    bot.bus.subscribe("rate_limit_disabled_warning", _collect)
    await bot.settings.warn_disabled_rate_limits()
    assert len(warnings) == 1
    assert warnings[0].network == "net1"


async def test_settings_reconnect_missing_raises(bot_with_fake_network) -> None:
    bot, _ = bot_with_fake_network
    bot.networks.pop("net1", None)
    with pytest.raises(SettingsError):
        await bot.settings.reconnect("net1")


async def test_settings_reload_reverts_in_memory_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    path = tmp_path / "cfg.toml"
    bot = VibeBot(_make_config(), config_path=path)
    await bot.settings.save_to_disk()

    await bot.settings.add_channel("net1", "#ephemeral")
    assert "#ephemeral" in bot.config.networks[0].channels

    await bot.settings.reload_from_disk()
    assert "#ephemeral" not in bot.config.networks[0].channels
    assert bot.networks["net1"].started is True


async def test_settings_reload_adds_and_removes_networks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    path = tmp_path / "cfg.toml"
    bot = VibeBot(_make_config(), config_path=path)
    await bot.settings.save_to_disk()

    # Rewrite disk: drop net1, add net2.
    alt = Config(
        bot=BotConfig(database=":memory:"),
        api=ApiConfig(tokens=[TOKEN]),
        networks=[
            NetworkConfig(
                name="net2",
                nick="bot2",
                servers=[ServerConfig(host="z.example", port=6697, tls=True, is_default=True)],
                channels=["#z"],
            )
        ],
    )
    save_config(path, alt)

    await bot.settings.reload_from_disk()
    assert "net1" not in bot.networks
    assert "net2" in bot.networks
    assert bot.networks["net2"].started is True
    assert [n.name for n in bot.config.networks] == ["net2"]


async def test_settings_reload_requires_path() -> None:
    bot = VibeBot(_make_config(), config_path=None)
    with pytest.raises(SettingsError):
        await bot.settings.reload_from_disk()


async def test_settings_add_network_spawns_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    bot = VibeBot(_make_config(), config_path=None)
    new_cfg = NetworkConfig(
        name="net2",
        nick="bot2",
        servers=[ServerConfig(host="x", is_default=True)],
    )
    await bot.settings.add_network(new_cfg)
    assert "net2" in bot.networks
    assert bot.networks["net2"].started is True


async def test_settings_remove_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    bot = VibeBot(_make_config(), config_path=None)
    fake = _FakeNetwork(bot.config.networks[0], bot.bus)
    bot.networks["net1"] = fake
    await fake.start()
    await bot.settings.remove_network("net1")
    assert "net1" not in bot.networks
    assert fake.stopped is True


async def test_settings_save_requires_path() -> None:
    bot = VibeBot(_make_config(), config_path=None)
    with pytest.raises(SettingsError):
        await bot.settings.save_to_disk()


async def test_settings_save_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    path = tmp_path / "cfg.toml"
    cfg = _make_config()
    bot = VibeBot(cfg, config_path=path)
    await bot.settings.save_to_disk()
    assert path.exists()
    reloaded = load_config(path)
    assert reloaded.networks[0].name == "net1"
    assert len(reloaded.networks[0].servers) == 2


# ---------------- API ----------------

@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import vibebot.core.settings as settings_mod

    monkeypatch.setattr(settings_mod, "NetworkConnection", _FakeNetwork)
    path = tmp_path / "cfg.toml"
    bot = VibeBot(_make_config(), config_path=path)
    app = build_app(bot)
    return bot, TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def test_api_get_settings(api_client) -> None:
    _bot, client = api_client
    r = client.get("/api/settings", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["networks"][0]["name"] == "net1"
    assert len(body["networks"][0]["servers"]) == 2


def test_api_add_and_delete_server(api_client) -> None:
    _bot, client = api_client
    r = client.post(
        "/api/settings/networks/net1/servers",
        headers=_auth(),
        json={"host": "c.example", "port": 6697, "tls": True, "tls_verify": True, "is_default": False},
    )
    assert r.status_code == 201
    assert len(r.json()["servers"]) == 3
    r = client.delete("/api/settings/networks/net1/servers/2", headers=_auth())
    assert r.status_code == 204


def test_api_mark_default(api_client) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/networks/net1/servers/1/default", headers=_auth())
    assert r.status_code == 200
    servers = r.json()["servers"]
    assert servers[0]["is_default"] is False
    assert servers[1]["is_default"] is True


def test_api_add_and_delete_channel(api_client) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/networks/net1/channels", headers=_auth(), json={"channel": "#new"})
    assert r.status_code == 201
    assert "#new" in r.json()["channels"]
    r = client.delete("/api/settings/networks/net1/channels/%23new", headers=_auth())
    assert r.status_code == 204


def test_api_patch_identity(api_client) -> None:
    _bot, client = api_client
    r = client.patch("/api/settings/networks/net1", headers=_auth(), json={"nick": "newnick"})
    assert r.status_code == 200
    assert r.json()["nick"] == "newnick"


def test_api_save_to_disk(api_client, tmp_path: Path) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/save", headers=_auth())
    assert r.status_code == 200
    saved_path = Path(r.json()["path"])
    assert saved_path.exists()
    reloaded = load_config(saved_path)
    assert reloaded.networks[0].name == "net1"


def test_api_create_and_remove_network(api_client) -> None:
    _bot, client = api_client
    r = client.post(
        "/api/settings/networks",
        headers=_auth(),
        json={
            "name": "net2",
            "nick": "b2",
            "servers": [{"host": "h", "port": 6697, "tls": True, "is_default": True}],
            "channels": [],
        },
    )
    assert r.status_code == 201
    r = client.delete("/api/settings/networks/net2", headers=_auth())
    assert r.status_code == 204


def test_api_create_network_without_servers(api_client) -> None:
    _bot, client = api_client
    r = client.post(
        "/api/settings/networks",
        headers=_auth(),
        json={"name": "net3", "nick": "b3", "servers": [], "channels": []},
    )
    assert r.status_code == 201
    assert r.json()["servers"] == []

    r = client.get("/api/settings/networks/net3", headers=_auth())
    assert r.status_code == 200
    assert r.json()["servers"] == []

    r = client.post(
        "/api/settings/networks/net3/servers",
        headers=_auth(),
        json={"host": "later.example", "port": 6697, "tls": True, "tls_verify": True, "is_default": False},
    )
    assert r.status_code == 201
    servers = r.json()["servers"]
    assert len(servers) == 1
    assert servers[0]["is_default"] is True


def test_api_add_channel_autoprefixes(api_client) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/networks/net1/channels", headers=_auth(), json={"channel": "foo"})
    assert r.status_code == 201
    assert "#foo" in r.json()["channels"]


def test_api_connect_disconnect(api_client) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/networks/net1/connect", headers=_auth())
    assert r.status_code == 200
    r = client.post("/api/settings/networks/net1/disconnect", headers=_auth())
    assert r.status_code == 200


def test_api_reconnect(api_client) -> None:
    _bot, client = api_client
    # bring net1 up via connect so reconnect has something to cycle
    r = client.post("/api/settings/networks/net1/connect", headers=_auth())
    assert r.status_code == 200
    r = client.post("/api/settings/networks/net1/reconnect", headers=_auth())
    assert r.status_code == 200
    assert r.json()["name"] == "net1"


def test_api_reconnect_unknown_network(api_client) -> None:
    _bot, client = api_client
    r = client.post("/api/settings/networks/nope/reconnect", headers=_auth())
    assert r.status_code == 404


def test_api_reload(api_client, tmp_path: Path) -> None:
    bot, client = api_client
    # save current state, then mutate runtime, then reload from disk
    r = client.post("/api/settings/save", headers=_auth())
    assert r.status_code == 200
    r = client.post("/api/settings/networks/net1/channels", headers=_auth(), json={"channel": "#tmp"})
    assert r.status_code == 201
    assert "#tmp" in bot.config.networks[0].channels

    r = client.post("/api/settings/reload", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "#tmp" not in body["networks"][0]["channels"]
    assert "#tmp" not in bot.config.networks[0].channels
