"""Tests for multi-network IRC layer: protocol toggle, auth dispatch, SASL."""

from __future__ import annotations

import asyncio

import pytest

from tests.fixtures.mock_ircd import MockIrcd
from vibebot.config import (
    NetworkConfig,
    NickServAuthConfig,
    NoAuthConfig,
    QAuthConfig,
    SaslAuthConfig,
)
from vibebot.core.events import EventBus
from vibebot.core.network import NetworkConnection


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> None:
    """Poll until predicate() is truthy, else raise."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met in time")


def _cfg(port: int, auth=None, protocol: str = "ircv3", channels: list[str] | None = None) -> NetworkConfig:
    return NetworkConfig(
        name="mock",
        host="127.0.0.1",
        port=port,
        tls=False,
        protocol=protocol,
        nick="vibebot",
        channels=channels or [],
        auth=auth or NoAuthConfig(),
    )


@pytest.fixture
async def mock_ircd():
    server = MockIrcd()
    yield server
    await server.stop()


async def _run_conn(cfg: NetworkConfig) -> tuple[NetworkConnection, EventBus]:
    bus = EventBus()
    conn = NetworkConnection(cfg, bus)
    await conn.start()
    return conn, bus


async def test_ircv3_mode_sends_cap_ls():
    ircd = MockIrcd(advertise_sasl=False)
    await ircd.start()
    try:
        cfg = _cfg(ircd.port, protocol="ircv3")
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(lambda: any(s.registered for s in ircd.sessions))
            sess = ircd.sessions[0]
            commands = [m.command for m in sess.received]
            assert "CAP" in commands, f"expected CAP LS in ircv3 mode, got: {commands}"
        finally:
            await conn.stop()
    finally:
        await ircd.stop()


async def test_rfc1459_mode_skips_cap():
    ircd = MockIrcd(ignore_cap=True)
    await ircd.start()
    try:
        cfg = _cfg(ircd.port, protocol="rfc1459")
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(lambda: any(s.registered for s in ircd.sessions))
            sess = ircd.sessions[0]
            commands = [m.command for m in sess.received]
            assert "CAP" not in commands, f"rfc1459 mode should not send CAP, got: {commands}"
            assert "NICK" in commands and "USER" in commands
        finally:
            await conn.stop()
    finally:
        await ircd.stop()


async def test_qauth_sends_privmsg_and_mode_then_joins():
    ircd = MockIrcd(autoreply_q=True)
    await ircd.start()
    try:
        auth = QAuthConfig(
            method="q",
            username="testq",
            password="secret",
            hidehost=True,
            wait_before_join=True,
            wait_timeout=2.0,
        )
        cfg = _cfg(ircd.port, auth=auth, channels=["#test"])
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(
                lambda: any(m.command == "JOIN" for s in ircd.sessions for m in s.received),
                timeout=5.0,
            )
            sess = ircd.sessions[0]
            privmsgs = [m for m in sess.received if m.command == "PRIVMSG"]
            modes = [m for m in sess.received if m.command == "MODE"]
            joins = [m for m in sess.received if m.command == "JOIN"]
            assert any(
                m.params and m.params[0].upper().startswith("Q@") and "AUTH testq secret" in (m.params[1] if len(m.params) > 1 else "")
                for m in privmsgs
            ), f"expected Q AUTH PRIVMSG, got: {[m.raw for m in privmsgs]}"
            assert any(m.params[1:] == ["+x"] for m in modes), f"expected MODE +x, got: {modes}"
            # JOIN must arrive AFTER 396 — i.e. after the MODE +x PRIVMSG round trip.
            mode_idx = next(i for i, m in enumerate(sess.received) if m.command == "MODE")
            join_idx = next(i for i, m in enumerate(sess.received) if m.command == "JOIN")
            assert join_idx > mode_idx, "JOIN should follow MODE +x when wait_before_join=True"
            assert joins[0].params[0] == "#test"
        finally:
            await conn.stop()
    finally:
        await ircd.stop()


async def test_nickserv_sends_identify():
    ircd = MockIrcd()
    await ircd.start()
    try:
        auth = NickServAuthConfig(method="nickserv", username="bot", password="pw")
        cfg = _cfg(ircd.port, auth=auth, channels=["#test"])
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(
                lambda: any(m.command == "PRIVMSG" for s in ircd.sessions for m in s.received)
            )
            sess = ircd.sessions[0]
            pm = next(m for m in sess.received if m.command == "PRIVMSG")
            assert pm.params[0] == "NickServ"
            assert pm.params[1] == "IDENTIFY bot pw"
        finally:
            await conn.stop()
    finally:
        await ircd.stop()


async def test_sasl_plain_success():
    ircd = MockIrcd(advertise_sasl=True)
    await ircd.start()
    try:
        auth = SaslAuthConfig(method="sasl", mechanism="PLAIN", username="u", password="p")
        cfg = _cfg(ircd.port, auth=auth)
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(lambda: any(s.registered for s in ircd.sessions), timeout=5.0)
            sess = ircd.sessions[0]
            authenticates = [m for m in sess.received if m.command == "AUTHENTICATE"]
            assert authenticates, f"expected AUTHENTICATE lines, got: {[m.command for m in sess.received]}"
            assert authenticates[0].params[0].upper() == "PLAIN"
        finally:
            await conn.stop()
    finally:
        await ircd.stop()
