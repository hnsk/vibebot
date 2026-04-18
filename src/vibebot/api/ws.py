"""WebSocket stream of the bot's event bus for live UI updates."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from vibebot.core.events import Event

router = APIRouter()


@router.websocket("/ws/events")
async def events_stream(ws: WebSocket) -> None:
    token = ws.query_params.get("token")
    tokens: set[str] = set(ws.app.state.api_tokens)
    if token not in tokens:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    bot = ws.app.state.bot
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)

    async def forwarder(event: Event) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(event)

    bot.bus.subscribe("*", forwarder)
    try:
        while True:
            event = await queue.get()
            await ws.send_json(asdict(event))
    except WebSocketDisconnect:
        return
    finally:
        bot.bus.unsubscribe("*", forwarder)
