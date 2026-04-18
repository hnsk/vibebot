"""EventBus pub/sub tests."""

from __future__ import annotations

from vibebot.core.events import Event, EventBus


async def test_subscribe_publish_roundtrip():
    bus = EventBus()
    seen: list[Event] = []

    async def handler(event: Event) -> None:
        seen.append(event)

    bus.subscribe("message", handler)
    await bus.publish(Event(kind="message", network="net", payload={"x": 1}))
    assert len(seen) == 1
    assert seen[0].get("x") == 1


async def test_wildcard_subscriber_receives_all():
    bus = EventBus()
    seen: list[str] = []

    async def any_handler(event: Event) -> None:
        seen.append(event.kind)

    bus.subscribe("*", any_handler)
    await bus.publish(Event(kind="join", network="n"))
    await bus.publish(Event(kind="message", network="n"))
    assert seen == ["join", "message"]


async def test_handler_exception_does_not_break_bus():
    bus = EventBus()
    ok_seen = []

    async def ok(event: Event) -> None:
        ok_seen.append(event.kind)

    async def bad(_event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("x", bad)
    bus.subscribe("x", ok)
    await bus.publish(Event(kind="x", network="n"))
    assert ok_seen == ["x"]


async def test_unsubscribe():
    bus = EventBus()
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.kind)

    bus.subscribe("x", handler)
    bus.unsubscribe("x", handler)
    await bus.publish(Event(kind="x", network="n"))
    assert seen == []
