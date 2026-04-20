"""Tests for the per-module scratch data directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibebot.config import ApiConfig, BotConfig, Config
from vibebot.core.bot import VibeBot
from vibebot.modules.settings import ModuleSettingsError

from tests.fixtures import settings_module as fx


def _make_bot(tmp_path: Path) -> VibeBot:
    return VibeBot(
        Config(
            bot=BotConfig(
                database=str(tmp_path / "bot.db"),
                modules_dir=str(tmp_path / "modules"),
                modules_data_dir=str(tmp_path / "mdata"),
            ),
            api=ApiConfig(tokens=["t"]),
        )
    )


async def test_data_dir_autocreates(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    try:
        loaded = await bot.modules._finalize_load("builtin", "example", fx)
        d = loaded.instance.data_dir
        assert d.exists() and d.is_dir()
        assert d == (tmp_path / "mdata" / "builtin" / "example").resolve()
        # Second access is idempotent.
        assert loaded.instance.data_dir == d
    finally:
        await bot.db.close()


async def test_data_dir_rejects_escape_in_repo(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    try:
        instance = fx.Example(bot)
        instance._repo = "../evil"
        instance._name = "example"
        with pytest.raises(ModuleSettingsError):
            _ = instance.data_dir
    finally:
        await bot.db.close()


async def test_data_dir_rejects_slash_in_name(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    try:
        instance = fx.Example(bot)
        instance._repo = "ok"
        instance._name = "a/b"
        with pytest.raises(ModuleSettingsError):
            _ = instance.data_dir
    finally:
        await bot.db.close()
