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
    RateLimitConfig,
    SaslAuthConfig,
)
from vibebot.core.events import Event, EventBus
from vibebot.core.network import NetworkConnection


async def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> None:
    """Poll until predicate() is truthy, else raise."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise TimeoutError("condition not met in time")


def _cfg(
    port: int,
    auth=None,
    protocol: str = "ircv3",
    channels: list[str] | None = None,
    rate_limit: RateLimitConfig | None = None,
) -> NetworkConfig:
    return NetworkConfig(
        name="mock",
        host="127.0.0.1",
        port=port,
        tls=False,
        protocol=protocol,
        nick="vibebot",
        channels=channels or [],
        auth=auth or NoAuthConfig(),
        rate_limit=rate_limit or RateLimitConfig(),
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


async def test_outgoing_rate_limit_paces_bursts():
    ircd = MockIrcd()
    await ircd.start()
    try:
        cfg = _cfg(ircd.port, rate_limit=RateLimitConfig(enabled=True, burst=3, period=0.2))
        conn, _ = await _run_conn(cfg)
        try:
            await _wait_until(lambda: any(s.registered for s in ircd.sessions))
            start = asyncio.get_event_loop().time()
            for i in range(6):
                await conn.send_message("#chan", f"msg{i}")
            elapsed = asyncio.get_event_loop().time() - start
            # Burst of 3 is instant; remaining 3 each wait ~period (0.2s) → >=0.6s total.
            assert elapsed >= 0.55, f"burst of 6 with period=0.2 should take >=0.55s, got {elapsed:.3f}"
            assert elapsed < 1.5, f"should not exceed 1.5s, got {elapsed:.3f}"

            sess = ircd.sessions[0]
            # All six must arrive on the wire after throttling.
            await _wait_until(
                lambda: len([m for m in sess.received if m.command == "PRIVMSG" and m.params and m.params[0] == "#chan"]) == 6,
                timeout=2.0,
            )
        finally:
            await conn.stop()
    finally:
        await ircd.stop()


async def test_apply_rate_limit_disabled_emits_warning_and_bypasses():
    ircd = MockIrcd()
    await ircd.start()
    try:
        cfg = _cfg(ircd.port, rate_limit=RateLimitConfig(enabled=True, burst=1, period=30.0))
        bus = EventBus()
        conn = NetworkConnection(cfg, bus)
        warnings: list[Event] = []

        async def on_warn(event: Event) -> None:
            warnings.append(event)

        bus.subscribe("rate_limit_disabled_warning", on_warn)
        await conn.start()
        try:
            await _wait_until(lambda: any(s.registered for s in ircd.sessions))
            await conn.send_message("#chan", "first")  # consumes the single token

            await conn.apply_rate_limit(RateLimitConfig(enabled=False, burst=1, period=30.0))
            assert warnings, "disabling the limiter should publish rate_limit_disabled_warning"
            assert warnings[0].network == "mock"

            start = asyncio.get_event_loop().time()
            for i in range(10):
                await conn.send_message("#chan", f"fast{i}")
            elapsed = asyncio.get_event_loop().time() - start
            # With bucket disabled the 10 sends complete immediately.
            assert elapsed < 0.2, f"disabled limiter should be instant, got {elapsed:.3f}"
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
