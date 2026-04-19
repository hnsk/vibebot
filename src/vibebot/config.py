"""Configuration loading for vibebot."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    database: str = "data/vibebot.db"
    modules_dir: str = "data/modules"
    log_level: str = "INFO"


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    tokens: list[str] = Field(default_factory=list)


class NoAuthConfig(BaseModel):
    method: Literal["none"] = "none"


class SaslAuthConfig(BaseModel):
    method: Literal["sasl"]
    mechanism: Literal["PLAIN", "EXTERNAL", "SCRAM-SHA-256", "SCRAM-SHA-1"] = "PLAIN"
    username: str | None = None
    password: str | None = None
    cert_path: str | None = None  # EXTERNAL only
    required: bool = False


class QAuthConfig(BaseModel):
    method: Literal["q"]
    username: str
    password: str
    service: str = "Q@CServe.quakenet.org"
    hidehost: bool = True
    wait_before_join: bool = True
    wait_timeout: float = 10.0
    required: bool = False


class NickServAuthConfig(BaseModel):
    method: Literal["nickserv"]
    username: str
    password: str
    service_nick: str = "NickServ"
    command_template: str = "IDENTIFY {username} {password}"
    required: bool = False


AuthConfig = Annotated[
    Union[NoAuthConfig, SaslAuthConfig, QAuthConfig, NickServAuthConfig],
    Field(discriminator="method"),
]


class NetworkConfig(BaseModel):
    name: str
    host: str
    port: int = 6697
    tls: bool = True
    tls_verify: bool = True
    protocol: Literal["ircv3", "rfc1459"] = "ircv3"
    nick: str
    username: str | None = None
    realname: str | None = None
    channels: list[str] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=NoAuthConfig)


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
