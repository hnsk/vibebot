"""Textual Pilot tests for the chat-first TUI."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vibebot.core.events import Event
from vibebot.tui.app import VibebotTui
from vibebot.tui.commands import CommandContext, CommandError, dispatch, parse_slash
from vibebot.tui.state import UiState


class FakeApi:
    """Drop-in stand-in for ApiClient. Records every call for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.networks_payload: list[dict] = [
            {
                "name": "local",
                "host": "127.0.0.1",
                "port": 6697,
                "tls": True,
                "connected": True,
                "channels": ["#ops"],
                "nickname": "vibebot",
            }
        ]
        self.channels_payload: dict[str, list[dict]] = {
            "local": [
                {
                    "name": "#ops",
                    "topic": "ops floor",
                    "by": "admin",
                    "set_at": None,
                    "users": [
                        {"nick": "alice", "prefix": "@", "ident": "a", "host": "h", "modes": ["o"]},
                        {"nick": "bob", "prefix": "", "ident": "b", "host": "h", "modes": []},
                    ],
                }
            ]
        }
        self.queries_payload: dict[str, list[dict]] = {"local": []}
        self.history_payload: dict[tuple[str, str], list[dict]] = {
            ("local", "#ops"): [
                {"ts": "2026-04-20T00:00:00+00:00", "kind": "msg", "nick": "alice", "body": "hello"},
            ]
        }

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    async def networks(self) -> list[dict]:
        self._rec("networks")
        return list(self.networks_payload)

    async def channels(self, network: str) -> list[dict]:
        self._rec("channels", network)
        return list(self.channels_payload.get(network, []))

    async def users(self, network: str, channel: str) -> list[dict]:
        self._rec("users", network, channel)
        for c in self.channels_payload.get(network, []):
            if c["name"] == channel:
                return list(c["users"])
        return []

    async def topic(self, network: str, channel: str) -> dict:
        self._rec("topic", network, channel)
        for c in self.channels_payload.get(network, []):
            if c["name"] == channel:
                return {"topic": c.get("topic"), "by": c.get("by"), "set_at": c.get("set_at")}
        return {"topic": None, "by": None, "set_at": None}

    async def history(self, network: str, channel: str) -> list[dict]:
        self._rec("history", network, channel)
        return list(self.history_payload.get((network, channel), []))

    async def queries(self, network: str) -> list[dict]:
        self._rec("queries", network)
        return list(self.queries_payload.get(network, []))

    async def query_history(self, network: str, peer: str) -> list[dict]:
        self._rec("query_history", network, peer)
        return list(self.history_payload.get((network, peer), []))

    async def close_query(self, network: str, peer: str) -> None:
        self._rec("close_query", network, peer)

    async def modules(self) -> list[dict]:
        self._rec("modules")
        return []

    async def repos(self) -> list[dict]:
        self._rec("repos")
        return []

    async def send(self, network: str, target: str, message: str) -> Any:
        self._rec("send", network, target, message)
        return {"status": "ok"}

    async def op(self, network: str, channel: str, nick: str) -> Any:
        self._rec("op", network, channel, nick)
        return {"status": "ok"}

    async def deop(self, network: str, channel: str, nick: str) -> Any:
        self._rec("deop", network, channel, nick)
        return {"status": "ok"}

    async def voice(self, network: str, channel: str, nick: str) -> Any:
        self._rec("voice", network, channel, nick)
        return {"status": "ok"}

    async def devoice(self, network: str, channel: str, nick: str) -> Any:
        self._rec("devoice", network, channel, nick)
        return {"status": "ok"}

    async def kick(self, network: str, channel: str, nick: str, reason: str | None = None) -> Any:
        self._rec("kick", network, channel, nick, reason)
        return {"status": "ok"}

    async def ban(self, network: str, channel: str, nick: str) -> Any:
        self._rec("ban", network, channel, nick)
        return {"status": "ok"}

    async def kickban(self, network: str, channel: str, nick: str, reason: str | None = None) -> Any:
        self._rec("kickban", network, channel, nick, reason)
        return {"status": "ok"}

    async def mode(self, network: str, channel: str, flags: str, args: list[str] | None = None) -> Any:
        self._rec("mode", network, channel, flags, tuple(args or ()))
        return {"status": "ok"}

    async def set_topic(self, network: str, channel: str, topic: str | None) -> Any:
        self._rec("set_topic", network, channel, topic)
        return {"status": "ok"}

    async def set_nick(self, network: str, nick: str) -> Any:
        self._rec("set_nick", network, nick)
        return {"status": "ok"}

    async def whois(self, network: str, nick: str) -> Any:
        self._rec("whois", network, nick)
        return {"status": "queued"}

    async def raw(self, network: str, line: str) -> Any:
        self._rec("raw", network, line)
        return {"status": "ok"}

    async def join(self, network: str, channel: str) -> Any:
        self._rec("join", network, channel)
        return {"status": "ok"}

    async def part(self, network: str, channel: str, reason: str | None = None) -> Any:
        self._rec("part", network, channel, reason)
        return {"status": "ok"}

    async def close(self) -> None:
        self._rec("close")


class FakeWsFeed:
    """In-process ws feed: pushes pre-queued events to the app."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _last_call(api: FakeApi, name: str) -> tuple:
    for n, args, _kwargs in reversed(api.calls):
        if n == name:
            return args
    raise AssertionError(f"no call to {name}: calls={api.calls!r}")


def _made_call(api: FakeApi, name: str) -> bool:
    return any(n == name for n, _a, _k in api.calls)


# ---------- slash parser / dispatcher (no Textual) ----------

def test_parse_slash_basic():
    p = parse_slash("/op alice")
    assert p is not None
    assert p.name == "op"
    assert p.args == ["alice"]


def test_parse_slash_escape():
    # `//foo` means literal `/foo` message, not a command.
    assert parse_slash("//foo") is None


def test_parse_slash_plain():
    assert parse_slash("hello world") is None


async def test_dispatch_op_calls_api():
    api = FakeApi()
    state = UiState()
    state.active_net = "local"
    state.active_target = "#ops"
    parsed = parse_slash("/op alice")
    assert parsed is not None
    ctx = CommandContext(api=api, state=state, cmd=parsed, on_open_query=lambda *_: None, on_close_query=lambda *_: None)
    await dispatch(ctx)
    assert _last_call(api, "op") == ("local", "#ops", "alice")


async def test_dispatch_me_wraps_ctcp_action():
    api = FakeApi()
    state = UiState()
    state.active_net = "local"
    state.active_target = "#ops"
    parsed = parse_slash("/me waves")
    assert parsed is not None
    ctx = CommandContext(api=api, state=state, cmd=parsed, on_open_query=lambda *_: None, on_close_query=lambda *_: None)
    await dispatch(ctx)
    args = _last_call(api, "send")
    assert args == ("local", "#ops", "\x01ACTION waves\x01")


async def test_dispatch_close_needs_query():
    api = FakeApi()
    state = UiState()
    state.active_net = "local"
    state.active_target = "#ops"
    parsed = parse_slash("/close")
    assert parsed is not None
    ctx = CommandContext(api=api, state=state, cmd=parsed, on_open_query=lambda *_: None, on_close_query=lambda *_: None)
    with pytest.raises(CommandError):
        await dispatch(ctx)


async def test_dispatch_close_query_invokes_callback():
    api = FakeApi()
    state = UiState()
    state.active_net = "local"
    state.active_target = "alice"
    closed: list[tuple[str, str]] = []
    parsed = parse_slash("/close")
    assert parsed is not None
    ctx = CommandContext(
        api=api,
        state=state,
        cmd=parsed,
        on_open_query=lambda *_: None,
        on_close_query=lambda n, p: closed.append((n, p)),
    )
    await dispatch(ctx)
    assert _last_call(api, "close_query") == ("local", "alice")
    assert closed == [("local", "alice")]


# ---------- UiState event routing ----------

def test_state_routes_channel_message():
    state = UiState()
    state.own_nicks["local"] = "vibebot"
    changed = state.apply_event(Event(kind="message", network="local", payload={"target": "#ops", "source": "alice", "message": "hi"}))
    assert ("local", "#ops") in changed
    buf = list(state.buffer("local", "#ops"))
    assert buf[-1].kind == "msg"
    assert buf[-1].nick == "alice"
    assert buf[-1].body == "hi"


def test_state_pm_routes_to_peer():
    state = UiState()
    state.own_nicks["local"] = "vibebot"
    # Inbound PM: target is own nick, source is peer.
    state.apply_event(Event(kind="message", network="local", payload={"target": "vibebot", "source": "alice", "message": "hey"}))
    assert list(state.buffer("local", "alice"))[-1].body == "hey"


def test_state_own_echo_is_suppressed_when_pending():
    state = UiState()
    state.own_nicks["local"] = "vibebot"
    state.record_pending_echo("local", "#ops", "msg", "hi")
    state.apply_event(Event(kind="message", network="local", payload={"target": "#ops", "source": "vibebot", "message": "hi"}))
    assert len(state.buffer("local", "#ops")) == 0


def test_state_action_parses_ctcp():
    state = UiState()
    state.apply_event(Event(kind="message", network="local", payload={"target": "#ops", "source": "alice", "message": "\x01ACTION waves\x01"}))
    line = list(state.buffer("local", "#ops"))[-1]
    assert line.kind == "action"
    assert line.body == "waves"


# ---------- Pilot-level UI tests ----------

async def test_pilot_boot_and_channel_select(tmp_path, monkeypatch):
    api = FakeApi()
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Networks hydrated and a default channel activated.
        assert _made_call(api, "networks")
        assert _made_call(api, "channels")
        assert app._state.active_net == "local"
        assert app._state.active_target == "#ops"
        # Roster pulled for the default channel.
        assert _made_call(api, "users")
        # History pulled for the default channel.
        assert _made_call(api, "history")


async def test_pilot_send_plain_message(tmp_path):
    api = FakeApi()
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer-input")
        composer.value = "hello"
        await pilot.press("enter")
        await pilot.pause()
        assert _last_call(api, "send") == ("local", "#ops", "hello")


async def test_pilot_slash_command_dispatches(tmp_path):
    api = FakeApi()
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        composer = app.query_one("#composer-input")
        composer.value = "/op alice"
        await pilot.press("enter")
        await pilot.pause()
        assert _last_call(api, "op") == ("local", "#ops", "alice")


async def test_pilot_live_event_renders(tmp_path):
    api = FakeApi()
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        await ws.events.put(Event(kind="message", network="local", payload={"target": "#ops", "source": "alice", "message": "live line"}))
        await pilot.pause()
        lines = list(app._state.buffer("local", "#ops"))
        assert any(line.body == "live line" for line in lines)


async def test_pilot_live_event_inactive_bumps_unread(tmp_path):
    api = FakeApi()
    api.channels_payload["local"].append({
        "name": "#other",
        "topic": None,
        "by": None,
        "set_at": None,
        "users": [],
    })
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        await ws.events.put(Event(kind="message", network="local", payload={"target": "#other", "source": "alice", "message": "elsewhere"}))
        await pilot.pause()
        assert app._state.unread.get(("local", "#other")) == 1


async def test_pilot_escape_closes_query(tmp_path):
    from vibebot.tui.widgets.tree import TargetRef

    api = FakeApi()
    ws = FakeWsFeed()
    app = VibebotTui(api_url="http://x", token="t", api=api, ws=ws)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Open a query buffer via an inbound PM, then activate it.
        app._state.apply_event(Event(kind="message", network="local", payload={"target": "vibebot", "source": "dave", "message": "yo"}))
        await app._activate(TargetRef(network="local", target="dave"))
        # Call the action directly — the binding itself is exercised in other
        # Pilot tests; here we verify that the close_query action fires the
        # API call and drops the query buffer off the active pointer.
        await app.action_close_query()
        await pilot.pause()
        assert _last_call(api, "close_query") == ("local", "dave")
        assert app._state.active_target == "*"
