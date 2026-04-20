"""WebSocket event feed for the TUI.

Subscribes to `/ws/events` on the bot's API (see `src/vibebot/api/ws.py`) and
drops parsed `Event` objects into an asyncio.Queue. Reconnects with simple
backoff; a status callback mirrors the web UI's `ws: online/offline` footer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from websockets.exceptions import WebSocketException

from vibebot.core.events import Event

log = logging.getLogger(__name__)

StatusCb = Callable[[str], None]
EventCb = Callable[[Event], Awaitable[None]]

_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0)


def _to_ws_url(api_url: str, token: str) -> str:
    """Translate `http(s)://host[:port]` into `ws(s)://host[:port]/ws/events?token=…`."""
    p = urlparse(api_url)
    scheme = "wss" if p.scheme == "https" else "ws"
    return urlunparse((scheme, p.netloc, "/ws/events", "", f"token={token}", ""))


class WsFeed:
    """Long-lived task that streams bot events into a Queue.

    The task reconnects forever until `stop()` is called. Consumers pull Events
    off `feed.events` (an asyncio.Queue). Status callbacks fire with
    "online"/"offline"/"connecting" so the app can update its footer.
    """

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        on_status: StatusCb | None = None,
        queue_size: int = 512,
    ) -> None:
        self._url = _to_ws_url(api_url, token)
        self.events: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._on_status = on_status or (lambda _s: None)
        self._task: asyncio.Task | None = None
        self._stop = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._run(), name="vibebot-tui-ws")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        attempt = 0
        while not self._stop:
            self._on_status("connecting")
            try:
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=10) as ws:
                    attempt = 0
                    self._on_status("online")
                    async for raw in ws:
                        event = _parse_event(raw)
                        if event is None:
                            continue
                        try:
                            self.events.put_nowait(event)
                        except asyncio.QueueFull:
                            # Drop oldest to keep the UI live rather than freezing.
                            with contextlib.suppress(asyncio.QueueEmpty):
                                self.events.get_nowait()
                            self.events.put_nowait(event)
            except asyncio.CancelledError:
                self._on_status("offline")
                raise
            except (WebSocketException, OSError) as exc:
                log.debug("tui ws connection lost: %s", exc)
            self._on_status("offline")
            if self._stop:
                return
            delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return


def _parse_event(raw: Any) -> Event | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind")
    network = data.get("network") or ""
    payload = data.get("payload") or {}
    if not isinstance(kind, str) or not isinstance(payload, dict):
        return None
    return Event(kind=kind, network=network, payload=payload)
