"""Fixture module used by test_module_settings / test_module_data_dir.

Not auto-loaded by the module manager — tests feed this module object into
``_finalize_load`` directly.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl, SecretStr

from vibebot.modules.base import Module


class Settings(BaseModel):
    model_config = {"validate_default": True}

    api_key: SecretStr = Field(default=SecretStr(""), description="API key")
    endpoint: HttpUrl = Field(default="https://api.example.com", description="Base URL")
    poll_interval: int = Field(default=300, ge=30, le=86400)
    greeting: str = "hello"


class Example(Module):
    name = "example"
    description = "Settings fixture module."
    Settings = Settings

    on_load_called: bool = False

    async def on_load(self) -> None:  # noqa: D401 (override)
        type(self).on_load_called = True


class Settings_Required(BaseModel):
    api_key: SecretStr  # no default — forces ValidationError on empty config


class RequiresKey(Module):
    name = "requires_key"
    description = "Fixture module with a required secret."
    Settings = Settings_Required
