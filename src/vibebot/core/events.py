"""In-process async pub/sub bus used to decouple IRC networks from modules."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EventHandler = Callable[["Event"], Awaitable[None]]


@dataclass(slots=True)
class Event:
    kind: str
    network: str
    payload: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class EventBus:
    """Simple async pub/sub. Handlers run concurrently; exceptions are logged, not propagated."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, kind: str, handler: EventHandler) -> None:
        self._handlers[kind].append(handler)

    def unsubscribe(self, kind: str, handler: EventHandler) -> None:
        handlers = self._handlers.get(kind)
        if not handlers:
            return
        with contextlib.suppress(ValueError):
            handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        handlers = list(self._handlers.get(event.kind, ()))
        handlers.extend(self._handlers.get("*", ()))
        if not handlers:
            return
        await asyncio.gather(
            *(self._dispatch(h, event) for h in handlers),
            return_exceptions=False,
        )

    async def _dispatch(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            log.exception("Event handler %r failed on %s", handler, event.kind)
