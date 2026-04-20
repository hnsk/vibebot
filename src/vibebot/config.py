"""Configuration loading and persistence for vibebot."""

from __future__ import annotations

import errno
import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

import tomlkit
from pydantic import BaseModel, Field, model_validator


class ConfigWriteError(OSError):
    """Raised when the config file cannot be persisted (e.g. read-only mount)."""


class BotConfig(BaseModel):
    database: str = "data/vibebot.db"
    modules_dir: str = "data/modules"
    modules_data_dir: str = "data/modules-data"
    log_level: str = "INFO"
    auto_install_requirements: bool = False
    pip_timeout_s: float = Field(600.0, ge=10.0, le=3600.0)


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
    NoAuthConfig | SaslAuthConfig | QAuthConfig | NickServAuthConfig,
    Field(discriminator="method"),
]


class ServerConfig(BaseModel):
    host: str
    port: int = 6697
    tls: bool = True
    tls_verify: bool = True
    is_default: bool = False


class RateLimitConfig(BaseModel):
    """Outgoing-message throttle. Defaults mirror irssi (burst 5, ~2s spacing)."""

    enabled: bool = True
    burst: int = Field(5, ge=1, le=50)
    period: float = Field(2.0, ge=0.1, le=30.0)


_LEGACY_SERVER_FIELDS = ("host", "port", "tls", "tls_verify")


def _migrate_legacy_network(raw: dict[str, Any]) -> dict[str, Any]:
    """If a network dict uses the pre-multi-server schema, wrap into servers list."""
    if raw.get("servers"):
        return raw
    legacy = {k: raw[k] for k in _LEGACY_SERVER_FIELDS if k in raw}
    if not legacy:
        return raw
    migrated = {k: v for k, v in raw.items() if k not in _LEGACY_SERVER_FIELDS}
    migrated["servers"] = [{**legacy, "is_default": True}]
    return migrated


class NetworkConfig(BaseModel):
    name: str
    servers: list[ServerConfig] = Field(default_factory=list)
    protocol: Literal["ircv3", "rfc1459"] = "ircv3"
    nick: str
    username: str | None = None
    realname: str | None = None
    hostname: str | None = None
    channels: list[str] = Field(default_factory=list)
    auth: AuthConfig = Field(default_factory=NoAuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_singleton(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return _migrate_legacy_network(dict(data))

    @model_validator(mode="after")
    def _one_default(self) -> NetworkConfig:
        defaults = [s for s in self.servers if s.is_default]
        if len(defaults) > 1:
            raise ValueError(
                f"network {self.name!r}: multiple servers flagged is_default"
            )
        if not defaults and self.servers:
            self.servers[0].is_default = True
        return self

    @property
    def default_server(self) -> ServerConfig | None:
        if not self.servers:
            return None
        for s in self.servers:
            if s.is_default:
                return s
        return self.servers[0]


class RepoConfig(BaseModel):
    name: str
    url: str
    branch: str = "main"
    subdir: str | None = None
    enabled: bool = True


class Config(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    networks: list[NetworkConfig] = Field(default_factory=list)
    repos: list[RepoConfig] = Field(default_factory=list)


def _migrate_raw(data: dict[str, Any]) -> dict[str, Any]:
    nets = data.get("networks")
    if isinstance(nets, list):
        data["networks"] = [_migrate_legacy_network(n) if isinstance(n, dict) else n for n in nets]
    return data


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    data = _migrate_raw(data)
    return Config.model_validate(data)


def save_config(path: Path, config: Config) -> None:
    """Persist a Config back to TOML, preserving comments/formatting where possible.

    Writes atomically via rename when possible; falls back to in-place write for
    single-file bind mounts (Docker) where rename-over the mount point returns
    EBUSY. Raises ConfigWriteError if the file is not writable.
    """
    if path.exists():
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    else:
        doc = tomlkit.document()

    payload = config.model_dump(mode="python", exclude_none=True)
    _apply_mapping(doc, payload)
    rendered = tomlkit.dumps(doc)

    if path.exists() and not os.access(path, os.W_OK):
        raise ConfigWriteError(
            errno.EACCES,
            f"config path is not writable: {path} (mounted read-only?)",
        )

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(rendered, encoding="utf-8")
        try:
            tmp.replace(path)
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM):
                raise
            # bind-mounted single file (Docker) — rewrite in place
            try:
                path.write_text(rendered, encoding="utf-8")
            except OSError as inner:
                raise ConfigWriteError(inner.errno, str(inner)) from inner
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        if isinstance(exc, ConfigWriteError):
            raise
        raise ConfigWriteError(exc.errno, str(exc)) from exc


def _apply_mapping(doc: Any, payload: dict[str, Any]) -> None:
    """Rewrite scalar/array leaves in `doc` from `payload`. Keeps top-level comments."""
    _set_or_add(doc, "bot", payload.get("bot", {}))
    _set_or_add(doc, "api", payload.get("api", {}))
    _replace_array_of_tables(doc, "networks", payload.get("networks", []))
    _replace_array_of_tables(doc, "repos", payload.get("repos", []))


def _set_or_add(doc: Any, key: str, value: dict[str, Any]) -> None:
    if key in doc:
        existing = doc[key]
        for k in list(existing.keys()):
            if k not in value:
                del existing[k]
        for k, v in value.items():
            existing[k] = v
    else:
        doc[key] = value


def _replace_array_of_tables(doc: Any, key: str, rows: list[dict[str, Any]]) -> None:
    if key in doc:
        del doc[key]
    aot = tomlkit.aot()
    for row in rows:
        table = tomlkit.table()
        for k, v in row.items():
            if isinstance(v, dict):
                sub = tomlkit.table()
                for sk, sv in v.items():
                    sub[sk] = sv
                table[k] = sub
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                inner = tomlkit.aot()
                for item in v:
                    sub = tomlkit.table()
                    for sk, sv in item.items():
                        sub[sk] = sv
                    inner.append(sub)
                table[k] = inner
            else:
                table[k] = v
        aot.append(table)
    doc[key] = aot
