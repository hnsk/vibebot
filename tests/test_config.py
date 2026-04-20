"""Configuration loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vibebot.config import (
    Config,
    NetworkConfig,
    NickServAuthConfig,
    NoAuthConfig,
    QAuthConfig,
    RateLimitConfig,
    SaslAuthConfig,
    load_config,
    save_config,
)


def test_load_example():
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(example)
    assert cfg.bot.database
    assert cfg.api.port == 8080
    assert cfg.networks[0].nick == "vibebot"


def test_default_auth_is_none():
    net = NetworkConfig(name="n", host="h", nick="bot")
    assert isinstance(net.auth, NoAuthConfig)
    assert net.protocol == "ircv3"


def test_sasl_auth_discriminated():
    net = NetworkConfig(
        name="n", host="h", nick="bot",
        auth={"method": "sasl", "mechanism": "PLAIN", "username": "u", "password": "p"},
    )
    assert isinstance(net.auth, SaslAuthConfig)
    assert net.auth.mechanism == "PLAIN"


def test_q_auth_discriminated():
    net = NetworkConfig(
        name="n", host="h", nick="bot",
        auth={"method": "q", "username": "u", "password": "p"},
    )
    assert isinstance(net.auth, QAuthConfig)
    assert net.auth.hidehost is True
    assert net.auth.wait_before_join is True


def test_nickserv_auth_discriminated():
    net = NetworkConfig(
        name="n", host="h", nick="bot",
        auth={"method": "nickserv", "username": "u", "password": "p"},
    )
    assert isinstance(net.auth, NickServAuthConfig)
    assert net.auth.service_nick == "NickServ"


def test_invalid_sasl_mechanism_rejected():
    with pytest.raises(ValidationError):
        NetworkConfig(
            name="n", host="h", nick="bot",
            auth={"method": "sasl", "mechanism": "BOGUS"},
        )


def test_protocol_rfc1459_accepted():
    net = NetworkConfig(name="n", host="h", nick="bot", protocol="rfc1459")
    assert net.protocol == "rfc1459"


def test_roundtrip():
    src = Config(networks=[
        NetworkConfig(
            name="q",
            host="irc.quakenet.org",
            nick="bot",
            auth=QAuthConfig(method="q", username="u", password="p"),
        )
    ])
    reloaded = Config.model_validate(src.model_dump())
    assert isinstance(reloaded.networks[0].auth, QAuthConfig)


def test_rate_limit_defaults():
    net = NetworkConfig(name="n", host="h", nick="bot")
    assert net.rate_limit.enabled is True
    assert net.rate_limit.burst == 5
    assert net.rate_limit.period == 2.0


def test_rate_limit_validation():
    with pytest.raises(ValidationError):
        NetworkConfig(name="n", host="h", nick="bot", rate_limit={"burst": 0})
    with pytest.raises(ValidationError):
        NetworkConfig(name="n", host="h", nick="bot", rate_limit={"period": 0})


def test_rate_limit_toml_roundtrip(tmp_path):
    cfg = Config(networks=[
        NetworkConfig(
            name="n",
            host="h",
            nick="bot",
            rate_limit=RateLimitConfig(enabled=False, burst=3, period=1.5),
        )
    ])
    path = tmp_path / "rt.toml"
    save_config(path, cfg)
    reloaded = load_config(path)
    rl = reloaded.networks[0].rate_limit
    assert rl.enabled is False
    assert rl.burst == 3
    assert rl.period == 1.5
