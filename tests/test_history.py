"""Unit tests for vibebot.core.history.ChannelHistory."""

from __future__ import annotations

import pytest

from vibebot.core.events import Event, EventBus
from vibebot.core.history import ChannelHistory


@pytest.fixture()
def bus_with_history() -> tuple[EventBus, ChannelHistory]:
    bus = EventBus()
    hist = ChannelHistory(capacity=5)
    hist.attach(bus)
    return bus, hist


async def test_message_appended_as_msg(bus_with_history):
    bus, hist = bus_with_history
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "#c", "source": "alice", "message": "hi"}))
    snap = hist.snapshot("n", "#c")
    assert len(snap) == 1
    assert snap[0]["kind"] == "msg"
    assert snap[0]["nick"] == "alice"
    assert snap[0]["body"] == "hi"


async def test_ctcp_action_appended_as_action(bus_with_history):
    bus, hist = bus_with_history
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "#c", "source": "alice", "message": "\x01ACTION waves\x01"}))
    snap = hist.snapshot("n", "#c")
    assert snap[0]["kind"] == "action"
    assert snap[0]["body"] == "waves"


async def test_pm_inbound_keyed_on_source(bus_with_history):
    bus, hist = bus_with_history
    # Inbound PM: target is our nick, source is the peer. Without an own-nick
    # resolver the peer falls back to source (works for inbound).
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "mybot", "source": "bob", "message": "psst"}))
    snap = hist.snapshot("n", "bob")
    assert len(snap) == 1
    assert snap[0]["kind"] == "msg"
    assert snap[0]["nick"] == "bob"
    assert snap[0]["body"] == "psst"


async def test_pm_outbound_keyed_on_peer_with_resolver():
    bus = EventBus()
    hist = ChannelHistory(capacity=5, own_nick_of=lambda _net: "mybot")
    hist.attach(bus)
    # Outbound echo (pydle synthesizes on_message for our own sends): target is
    # the peer, source is our own nick. Must bucket under the peer, flagged self.
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "bob", "source": "mybot", "message": "hi"}))
    snap = hist.snapshot("n", "bob")
    assert len(snap) == 1
    assert snap[0]["body"] == "hi"
    assert snap[0].get("self") is True


async def test_pm_ctcp_action_round_trip():
    bus = EventBus()
    hist = ChannelHistory(capacity=5, own_nick_of=lambda _net: "mybot")
    hist.attach(bus)
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "mybot", "source": "bob",
                                     "message": "\x01ACTION waves\x01"}))
    snap = hist.snapshot("n", "bob")
    assert snap[0]["kind"] == "action"
    assert snap[0]["body"] == "waves"


async def test_peers_lists_known_pm_targets():
    bus = EventBus()
    hist = ChannelHistory(capacity=5, own_nick_of=lambda _net: "mybot")
    hist.attach(bus)
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "mybot", "source": "alice", "message": "hi"}))
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "#chan", "source": "x", "message": "noise"}))
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "charlie", "source": "mybot", "message": "hey"}))
    assert hist.peers("n") == ["alice", "charlie"]


async def test_nick_change_carries_pm_buffer_forward():
    bus = EventBus()
    hist = ChannelHistory(capacity=5, own_nick_of=lambda _net: "mybot")
    hist.attach(bus)
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "mybot", "source": "alice", "message": "hi"}))
    await bus.publish(Event(kind="nick", network="n",
                            payload={"old": "alice", "new": "alice_"}))
    assert hist.snapshot("n", "alice") == []
    snap = hist.snapshot("n", "alice_")
    # Original message + the rename event line are both present.
    assert any(l.get("body") == "hi" for l in snap)
    assert any(l.get("event") == "nick" for l in snap)


async def test_join_event_shape(bus_with_history):
    bus, hist = bus_with_history
    await bus.publish(Event(kind="join", network="n",
                            payload={"channel": "#c", "user": "alice"}))
    snap = hist.snapshot("n", "#c")
    assert snap[0]["kind"] == "event"
    assert snap[0]["event"] == "join"
    assert "alice" in snap[0]["body"]


async def test_capacity_trim(bus_with_history):
    bus, hist = bus_with_history  # capacity=5
    for i in range(8):
        await bus.publish(Event(kind="message", network="n",
                                payload={"target": "#c", "source": "a", "message": f"m{i}"}))
    snap = hist.snapshot("n", "#c")
    assert len(snap) == 5
    assert snap[0]["body"] == "m3"
    assert snap[-1]["body"] == "m7"


async def test_quit_fans_out_to_known_channels(bus_with_history):
    bus, hist = bus_with_history
    # Seed two channels so history has buffers for them.
    await bus.publish(Event(kind="join", network="n", payload={"channel": "#a", "user": "alice"}))
    await bus.publish(Event(kind="join", network="n", payload={"channel": "#b", "user": "alice"}))
    await bus.publish(Event(kind="quit", network="n", payload={"user": "alice", "message": "bye"}))
    for ch in ("#a", "#b"):
        snap = hist.snapshot("n", ch)
        assert any(l.get("event") == "quit" for l in snap)


async def test_snapshot_returns_copy(bus_with_history):
    bus, hist = bus_with_history
    await bus.publish(Event(kind="message", network="n",
                            payload={"target": "#c", "source": "a", "message": "m"}))
    s1 = hist.snapshot("n", "#c")
    s1.clear()
    assert len(hist.snapshot("n", "#c")) == 1
