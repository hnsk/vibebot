"""Configuration loading for vibebot."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    database: str = "data/vibebot.db"
    modules_dir: str = "data/modules"
    log_level: str = "INFO"


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    tokens: list[str] = Field(default_factory=list)


class NetworkConfig(BaseModel):
    name: str
    host: str
    port: int = 6697
    tls: bool = True
    nick: str
    username: str | None = None
    realname: str | None = None
    channels: list[str] = Field(default_factory=list)
    sasl_username: str | None = None
    sasl_password: str | None = None


class RepoConfig(BaseModel):
    name: str
    url: str
    branch: str = "main"
    enabled: bool = True


class Config(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    networks: list[NetworkConfig] = Field(default_factory=list)
    repos: list[RepoConfig] = Field(default_factory=list)


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config.model_validate(data)
