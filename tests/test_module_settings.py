"""Tests for per-module Pydantic settings: helpers, loader wiring, API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, HttpUrl, SecretStr, ValidationError

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config
from vibebot.core.bot import VibeBot
from vibebot.modules.settings import (
    ModuleSettingsError,
    SECRET_PLACEHOLDER,
    mask_secrets,
    merge_and_validate,
    sanitize_segment,
    schema_with_secret_flags,
    secret_field_names,
)

from tests.fixtures import settings_module as fx

TOKEN = "tok"


# ---------------- helpers ----------------

def test_sanitize_segment_accepts_plain() -> None:
    assert sanitize_segment("weather") == "weather"
    assert sanitize_segment("foo-bar_1") == "foo-bar_1"


@pytest.mark.parametrize("bad", ["", "..", ".", ".hidden", "a/b", "a\\b", "a\x00b"])
def test_sanitize_segment_rejects(bad: str) -> None:
    with pytest.raises(ModuleSettingsError):
        sanitize_segment(bad)


def test_secret_field_names_detects_secretstr() -> None:
    assert secret_field_names(fx.Settings) == {"api_key"}


def test_mask_secrets_replaces_secretstr() -> None:
    model = fx.Settings(api_key=SecretStr("s3cr3t"), greeting="hi")
    dumped = mask_secrets(model)
    assert dumped["api_key"] == SECRET_PLACEHOLDER
    assert dumped["greeting"] == "hi"


def test_merge_and_validate_empty_secret_preserves() -> None:
    stored = {"api_key": "original", "greeting": "hi", "endpoint": "https://api.example.com", "poll_interval": 300}
    merged = merge_and_validate(fx.Settings, stored, {"api_key": "", "greeting": "bye"})
    assert merged.api_key.get_secret_value() == "original"
    assert merged.greeting == "bye"


def test_merge_and_validate_new_secret_overwrites() -> None:
    stored = {"api_key": "old"}
    merged = merge_and_validate(fx.Settings, stored, {"api_key": "new"})
    assert merged.api_key.get_secret_value() == "new"


def test_merge_and_validate_type_failure_raises() -> None:
    with pytest.raises(ValidationError):
        merge_and_validate(fx.Settings, {}, {"poll_interval": 5})  # below ge=30


def test_schema_with_secret_flags() -> None:
    schema = schema_with_secret_flags(fx.Settings)
    assert schema["properties"]["api_key"]["secret"] is True
    assert "secret" not in schema["properties"]["greeting"]


# ---------------- loader wiring ----------------

def _make_bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "modules-data"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=[TOKEN]),
    )
    return VibeBot(cfg)


@pytest.fixture()
async def bot(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    yield b
    await b.db.close()


async def test_loader_persists_defaults_on_first_load(bot: VibeBot) -> None:
    fx.Example.on_load_called = False
    loaded = await bot.modules._finalize_load("repoA", "example", fx)
    assert loaded.enabled is True
    assert loaded.instance.settings.greeting == "hello"
    assert loaded.instance.settings.poll_interval == 300
    stored = await bot.modules.get_stored_settings("repoA", "example")
    assert stored["greeting"] == "hello"
    assert stored["poll_interval"] == 300
    assert fx.Example.on_load_called is True


async def test_loader_accepts_valid_stored(bot: VibeBot) -> None:
    await bot.modules.save_settings(
        "repoB",
        "example",
        {
            "api_key": "abc",
            "endpoint": "https://other.example.com/",
            "poll_interval": 120,
            "greeting": "yo",
        },
    )
    loaded = await bot.modules._finalize_load("repoB", "example", fx)
    assert loaded.enabled is True
    assert loaded.instance.settings.greeting == "yo"
    assert loaded.instance.settings.poll_interval == 120
    assert loaded.instance.settings.api_key.get_secret_value() == "abc"


async def test_loader_disables_on_validation_error(bot: VibeBot) -> None:
    # RequiresKey.Settings has a required api_key with no default; empty stored
    # config must trip ValidationError.
    fx.Example.on_load_called = False
    # Use the RequiresKey class — _find_module_class returns the first
    # Module subclass whose __module__ matches, so make a throwaway module
    # that only exposes RequiresKey.
    import types
    synthetic = types.ModuleType("test_requires_module")
    synthetic.RequiresKey = fx.RequiresKey
    fx.RequiresKey.__module__ = "test_requires_module"
    loaded = await bot.modules._finalize_load("repoC", "requires_key", synthetic)
    assert loaded.enabled is False
    # Module is still tracked so the UI can surface the error.
    assert ("repoC", "requires_key") in bot.modules._loaded
    from sqlalchemy import select
    from vibebot.storage.models import ModuleState
    async with bot.db.session() as s:
        row = (
            await s.execute(
                select(ModuleState).where(
                    ModuleState.repo_name == "repoC",
                    ModuleState.module_name == "requires_key",
                )
            )
        ).scalar_one()
    assert row.last_error is not None
    assert "api_key" in row.last_error


async def test_save_settings_preserves_loaded_enabled_flags(bot: VibeBot) -> None:
    await bot.modules._finalize_load("repoD", "example", fx)
    await bot.modules.save_settings("repoD", "example", {"api_key": "x", "greeting": "g", "endpoint": "https://api.example.com/", "poll_interval": 60})
    from sqlalchemy import select
    from vibebot.storage.models import ModuleState
    async with bot.db.session() as s:
        row = (
            await s.execute(
                select(ModuleState).where(
                    ModuleState.repo_name == "repoD", ModuleState.module_name == "example"
                )
            )
        ).scalar_one()
    assert row.loaded is True
    assert row.enabled is True
    assert row.last_error is None
    assert json.loads(row.config_json)["greeting"] == "g"


# ---------------- API ----------------

@pytest.fixture()
async def api_client(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    # Pre-load example module so the API can find it.
    await b.modules._finalize_load("repo", "example", fx)
    app = build_app(b)
    with TestClient(app) as c:
        yield c, b
    await b.db.close()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


async def test_api_returns_schema_with_secret_flag(api_client) -> None:
    client, _ = api_client
    r = client.get("/api/modules/repo/example/settings/schema", headers=_auth())
    assert r.status_code == 200
    schema = r.json()
    assert schema["declared"] is True
    assert schema["properties"]["api_key"]["secret"] is True


async def test_api_get_masks_secrets(api_client) -> None:
    client, bot = api_client
    await bot.modules.save_settings("repo", "example", {
        "api_key": "sekret",
        "endpoint": "https://api.example.com/",
        "poll_interval": 300,
        "greeting": "hi",
    })
    r = client.get("/api/modules/repo/example/settings", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["declared"] is True
    assert body["values"]["api_key"] == SECRET_PLACEHOLDER
    assert body["values"]["greeting"] == "hi"


async def test_api_put_empty_string_preserves_secret(api_client) -> None:
    client, bot = api_client
    await bot.modules.save_settings("repo", "example", {
        "api_key": "keep-me",
        "endpoint": "https://api.example.com/",
        "poll_interval": 300,
        "greeting": "hi",
    })
    r = client.put(
        "/api/modules/repo/example/settings",
        json={"api_key": "", "greeting": "bye"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.json()["reload_required"] is True
    stored = await bot.modules.get_stored_settings("repo", "example")
    assert stored["api_key"] == "keep-me"
    assert stored["greeting"] == "bye"


async def test_api_put_validation_error(api_client) -> None:
    client, _ = api_client
    r = client.put(
        "/api/modules/repo/example/settings",
        json={"poll_interval": 5},  # below ge=30
        headers=_auth(),
    )
    assert r.status_code == 422


async def test_api_put_unknown_module_404(api_client) -> None:
    client, _ = api_client
    r = client.put(
        "/api/modules/repo/missing/settings",
        json={},
        headers=_auth(),
    )
    assert r.status_code == 404
