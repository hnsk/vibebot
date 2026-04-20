"""Unit tests for core.network._resolve_bind_address."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

from vibebot.core.network import _resolve_bind_address


def _info(family: int, ip: str) -> tuple:
    # getaddrinfo returns (family, type, proto, canonname, sockaddr).
    if family == socket.AF_INET6:
        sockaddr = (ip, 0, 0, 0)
    else:
        sockaddr = (ip, 0)
    return (family, socket.SOCK_STREAM, 0, "", sockaddr)


async def test_ipv4_literal_fast_path():
    with patch("asyncio.get_running_loop") as loop_mock:
        result = await _resolve_bind_address("192.0.2.5", "irc.example.org")
    assert result == ("192.0.2.5", 0)
    loop_mock.assert_not_called()


async def test_ipv6_literal_fast_path():
    with patch("asyncio.get_running_loop") as loop_mock:
        result = await _resolve_bind_address("2001:db8::1", "irc.example.org")
    assert result == ("2001:db8::1", 0)
    loop_mock.assert_not_called()


async def test_hostname_picks_ipv4_when_server_is_v4():
    async def fake_getaddrinfo(host, *args, **kwargs):
        if host == "irc.example.org":
            return [_info(socket.AF_INET, "198.51.100.10")]
        if host == "vhost.example.org":
            return [
                _info(socket.AF_INET6, "2001:db8::42"),
                _info(socket.AF_INET, "192.0.2.77"),
            ]
        return []

    with patch("asyncio.get_running_loop") as loop_mock:
        loop_mock.return_value.getaddrinfo = AsyncMock(side_effect=fake_getaddrinfo)
        result = await _resolve_bind_address("vhost.example.org", "irc.example.org")
    assert result == ("192.0.2.77", 0)


async def test_hostname_picks_ipv6_when_server_is_v6():
    async def fake_getaddrinfo(host, *args, **kwargs):
        if host == "irc.example.org":
            return [_info(socket.AF_INET6, "2001:db8::1")]
        if host == "vhost.example.org":
            return [
                _info(socket.AF_INET, "192.0.2.77"),
                _info(socket.AF_INET6, "2001:db8::42"),
            ]
        return []

    with patch("asyncio.get_running_loop") as loop_mock:
        loop_mock.return_value.getaddrinfo = AsyncMock(side_effect=fake_getaddrinfo)
        result = await _resolve_bind_address("vhost.example.org", "irc.example.org")
    assert result == ("2001:db8::42", 0)


async def test_hostname_unresolvable_returns_none():
    async def fake_getaddrinfo(host, *args, **kwargs):
        if host == "irc.example.org":
            return [_info(socket.AF_INET, "198.51.100.10")]
        raise socket.gaierror("nodename nor servname provided")

    with patch("asyncio.get_running_loop") as loop_mock:
        loop_mock.return_value.getaddrinfo = AsyncMock(side_effect=fake_getaddrinfo)
        result = await _resolve_bind_address("not-a-real.invalid", "irc.example.org")
    assert result is None


async def test_family_mismatch_falls_back_to_first_host_result():
    # Server is v6-only; hostname is v4-only. We fall back rather than fail —
    # the kernel will reject at connect time, letting the retry loop re-try
    # without a bind after the warning path on the next reconnection cycle.
    async def fake_getaddrinfo(host, *args, **kwargs):
        if host == "irc.example.org":
            return [_info(socket.AF_INET6, "2001:db8::1")]
        if host == "vhost.example.org":
            return [_info(socket.AF_INET, "192.0.2.77")]
        return []

    with patch("asyncio.get_running_loop") as loop_mock:
        loop_mock.return_value.getaddrinfo = AsyncMock(side_effect=fake_getaddrinfo)
        result = await _resolve_bind_address("vhost.example.org", "irc.example.org")
    assert result == ("192.0.2.77", 0)


async def test_server_unresolvable_still_returns_hostname_ip():
    # If we can't resolve the server host for family detection, fall through
    # to whatever getaddrinfo gives us for the bind hostname.
    async def fake_getaddrinfo(host, *args, **kwargs):
        if host == "irc.example.org":
            raise socket.gaierror("temp failure")
        if host == "vhost.example.org":
            return [_info(socket.AF_INET, "192.0.2.77")]
        return []

    with patch("asyncio.get_running_loop") as loop_mock:
        loop_mock.return_value.getaddrinfo = AsyncMock(side_effect=fake_getaddrinfo)
        result = await _resolve_bind_address("vhost.example.org", "irc.example.org")
    assert result == ("192.0.2.77", 0)
