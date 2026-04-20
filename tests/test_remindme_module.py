"""Tests for the built-in !remindme module."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.core.events import Event
from vibebot.modules.builtin.remindme import (
    MAX_SECONDS,
    RemindMeModule,
    RemindMeSettings,
    parse_duration,
)


# ------------------------------- parser --------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1s", 1),
        ("5m", 300),
        ("2h", 7200),
        ("3d", 259200),
        ("1w", 604800),
        ("1y", 31_536_000),
        ("1h30m", 5400),
        ("2h15m30s", 2 * 3600 + 15 * 60 + 30),
    ],
)
def test_parse_duration_short(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 second", 1),
        ("2 seconds", 2),
        ("5 minutes", 300),
        ("1 minute", 60),
        ("2 hours", 7200),
        ("1 hour", 3600),
        ("3 days", 259200),
        ("1 day", 86400),
        ("1 week", 604800),
        ("2 weeks", 1209600),
        ("1 year", 31_536_000),
    ],
)
def test_parse_duration_long(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


def test_parse_duration_long_case_insensitive() -> None:
    assert parse_duration("1 DAY") == 86400
    assert parse_duration("5 Minutes") == 300


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "0s", "-5m", "5x", "5", "m", "5 fortnights", "1 1 1"],
)
def test_parse_duration_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_rejects_over_cap() -> None:
    with pytest.raises(ValueError):
        parse_duration(f"{(MAX_SECONDS // 86400) + 2}d")


# ------------------------- module integration --------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.client = SimpleNamespace(users={})

    async def send_message(self, target: str, message: str) -> None:
        self.sent.append((target, message))


def _make_bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "mdata"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["t0ken"]),
        networks=[],
        repos=[RepoConfig(name="sample", url="https://example.com/x.git")],
    )
    return VibeBot(cfg)


@pytest.fixture()
async def bot(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    await b.scheduler.start()
    await b.schedules.rehydrate()
    try:
        yield b
    finally:
        await b.scheduler.stop()
        await b.db.close()


async def _install_module(bot: VibeBot, conn: _FakeConn) -> RemindMeModule:
    module = RemindMeModule(bot)
    module._repo = "__builtin__"
    module._name = "remindme"
    module.settings = RemindMeSettings()
    await module.on_load()
    bot.networks["mock"] = conn  # type: ignore[assignment]
    return module


def _event(message: str, target: str = "#room", source: str = "alice") -> Event:
    return Event(
        kind="message",
        network="mock",
        payload={"message": message, "source": source, "target": target},
    )


async def test_command_schedules_reminder(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module.on_message(_event("!remindme 1h hello world"))

    schedules = await bot.schedules.list()
    assert len(schedules) == 1
    dto = schedules[0]
    assert dto.owner_nick == "alice"
    assert dto.repo_name == "__builtin__"
    assert dto.module_name == "remindme"
    assert dto.handler_name == "remind"
    assert dto.trigger["type"] == "date"
    assert dto.payload == {
        "network": "mock",
        "reply_to": "#room",
        "nick": "alice",
        "message": "hello world",
    }

    assert conn.sent, "expected confirmation message"
    target, text = conn.sent[0]
    assert target == "#room"
    assert "reminder set" in text
    assert dto.id[:8] in text


async def test_pm_reminder_replies_to_sender(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    # PM: target is the bot's nick (non-channel); reply should go back to alice.
    await module.on_message(
        _event("!remindme 5m private note", target="vibebot")
    )

    dto = (await bot.schedules.list())[0]
    assert dto.payload["reply_to"] == "alice"


async def test_fire_sends_reminder(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module._fire(
        {
            "network": "mock",
            "reply_to": "#room",
            "nick": "alice",
            "message": "buy milk",
        }
    )

    assert ("#room", "alice: reminder — buy milk") in conn.sent


async def test_fire_uses_custom_reply_format(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    module.settings = RemindMeSettings(reply_format="[{nick}] {message}!")

    await module._fire(
        {"network": "mock", "reply_to": "#room", "nick": "alice", "message": "go"}
    )

    assert conn.sent[-1] == ("#room", "[alice] go!")


async def test_fire_missing_network_noop(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module._fire(
        {
            "network": "nonexistent",
            "reply_to": "#room",
            "nick": "alice",
            "message": "x",
        }
    )

    assert conn.sent == []


async def test_long_form_duration_two_tokens(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module.on_message(_event("!remindme 1 day stand up"))

    schedules = await bot.schedules.list()
    assert len(schedules) == 1
    assert schedules[0].payload["message"] == "stand up"


async def test_invalid_duration_replies_usage(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module.on_message(_event("!remindme xyz hello"))

    assert await bot.schedules.list() == []
    assert conn.sent
    assert "bad duration" in conn.sent[0][1] or "usage" in conn.sent[0][1]


async def test_missing_message_replies_usage(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module.on_message(_event("!remindme 1h"))

    assert await bot.schedules.list() == []
    assert conn.sent
    assert "usage" in conn.sent[0][1].lower()


async def test_wrong_prefix_ignored(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    await module.on_message(_event("!notremindme 1h hi"))
    await module.on_message(_event("hello world"))

    assert await bot.schedules.list() == []
    assert conn.sent == []


async def test_custom_command_setting(bot: VibeBot) -> None:
    conn = _FakeConn()
    module = RemindMeModule(bot)
    module._repo = "__builtin__"
    module._name = "remindme"
    module.settings = RemindMeSettings(command="!rem")
    await module.on_load()
    bot.networks["mock"] = conn  # type: ignore[assignment]

    await module.on_message(_event("!remindme 1h ignored"))
    assert await bot.schedules.list() == []

    await module.on_message(_event("!rem 1h works"))
    schedules = await bot.schedules.list()
    assert len(schedules) == 1
    assert schedules[0].payload["message"] == "works"
