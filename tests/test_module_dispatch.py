"""End-to-end test: ModuleManager routes events through the trigger registry."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibebot.config import ApiConfig, BotConfig, Config
from vibebot.core.bot import VibeBot
from vibebot.core.events import Event
from vibebot.modules.base import Module, on_ctcp, on_message, on_mode


class _AModule(Module):
    name = "a_mod"

    async def on_load(self) -> None:
        self.calls: list[str] = []

    @on_message(exact="!foo")
    async def on_foo(self, event: Event) -> None:
        self.calls.append(event.get("message", ""))


class _BModule(Module):
    name = "b_mod"

    async def on_load(self) -> None:
        self.calls: list[str] = []

    @on_message(exact="!bar")
    async def on_bar(self, event: Event) -> None:
        self.calls.append(event.get("message", ""))


class _ModeModule(Module):
    name = "mode_mod"

    async def on_load(self) -> None:
        self.ops: list[tuple] = []

    @on_mode(letters=["o"], direction="+")
    async def on_op(self, event: Event) -> None:
        self.ops.append(tuple(event.get("modes_parsed", [])))


class _CTCPModule(Module):
    name = "ctcp_mod"

    async def on_load(self) -> None:
        self.versions: list[str] = []

    @on_ctcp(type="VERSION")
    async def on_version(self, event: Event) -> None:
        self.versions.append(event.get("ctcp_type", ""))


class _DynamicModule(Module):
    """Registers a trigger from on_load based on a setting."""

    name = "dyn_mod"

    async def on_load(self) -> None:
        self.hits: list[str] = []
        self.register_trigger(
            "message",
            handler=self._hit,
            startswith="!dyn",
        )

    async def _hit(self, event: Event) -> None:
        self.hits.append(event.get("message", ""))


def _make_bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "mdata"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["t"]),
    )
    return VibeBot(cfg)


@pytest.fixture()
async def bot(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    try:
        yield b
    finally:
        await b.db.close()


async def _install(bot: VibeBot, cls: type[Module]) -> Module:
    module = cls(bot)
    module._repo = "test"
    module._name = cls.name
    await module.on_load()
    bot.modules._register_triggers("test", cls.name, module, cls)
    return module


async def _settle() -> None:
    """Yield control so spawn_guarded tasks complete."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_message_dispatch_only_to_matching_trigger(bot: VibeBot) -> None:
    a = await _install(bot, _AModule)
    b = await _install(bot, _BModule)
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!foo", "source": "alice", "target": "#c",
    }))
    await _settle()
    assert a.calls == ["!foo"]
    assert b.calls == []


async def test_mode_dispatch_letters_filter(bot: VibeBot) -> None:
    m = await _install(bot, _ModeModule)
    await bot.bus.publish(Event(kind="mode", network="n", payload={
        "channel": "#c", "modes_parsed": [("+", "o", "alice")],
    }))
    await bot.bus.publish(Event(kind="mode", network="n", payload={
        "channel": "#c", "modes_parsed": [("+", "v", "bob")],
    }))
    await _settle()
    assert m.ops == [(("+", "o", "alice"),)]


async def test_ctcp_dispatch_type_filter(bot: VibeBot) -> None:
    c = await _install(bot, _CTCPModule)
    await bot.bus.publish(Event(kind="ctcp", network="n", payload={
        "ctcp_type": "VERSION", "source": "x", "target": "bot",
    }))
    await bot.bus.publish(Event(kind="ctcp", network="n", payload={
        "ctcp_type": "PING", "source": "x", "target": "bot",
    }))
    await _settle()
    assert c.versions == ["VERSION"]


async def test_unload_stops_dispatch(bot: VibeBot) -> None:
    a = await _install(bot, _AModule)
    bot.modules._registry.remove_for_module("test", _AModule.name)
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!foo", "source": "alice", "target": "#c",
    }))
    await _settle()
    assert a.calls == []


async def test_disable_suppresses_dispatch(bot: VibeBot) -> None:
    a = await _install(bot, _AModule)
    bot.modules._registry.set_enabled("test", _AModule.name, False)
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!foo", "source": "alice", "target": "#c",
    }))
    await _settle()
    assert a.calls == []

    bot.modules._registry.set_enabled("test", _AModule.name, True)
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!foo", "source": "alice", "target": "#c",
    }))
    await _settle()
    assert a.calls == ["!foo"]


async def test_dynamic_trigger_registered_from_on_load(bot: VibeBot) -> None:
    d = await _install(bot, _DynamicModule)
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!dyn hello", "source": "alice", "target": "#c",
    }))
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "hello", "source": "alice", "target": "#c",
    }))
    await _settle()
    assert d.hits == ["!dyn hello"]


async def test_own_nick_echo_suppressed(bot: VibeBot) -> None:
    a = await _install(bot, _AModule)
    # Stash a connection-like stub so _own_nick_of returns "bot".

    class _FakeConn:
        class _Client:
            nickname = "bot"
        client = _Client()

    bot.networks["n"] = _FakeConn()  # type: ignore[assignment]
    await bot.bus.publish(Event(kind="message", network="n", payload={
        "message": "!foo", "source": "bot", "target": "#c",
    }))
    await _settle()
    assert a.calls == []
