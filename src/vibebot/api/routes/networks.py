"""Network CRUD + status routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/networks", tags=["networks"], dependencies=[Depends(require_token)])


@router.get("")
async def list_networks(request: Request) -> list[dict]:
    bot = request.app.state.bot
    return [
        {
            "name": conn.name,
            "host": conn.config.host,
            "port": conn.config.port,
            "tls": conn.config.tls,
            "connected": conn.connected,
            "channels": list(conn.config.channels),
            "nickname": getattr(conn.client, "nickname", None) or conn.config.nick,
        }
        for conn in bot.networks.values()
    ]


@router.post("/{name}/join")
async def join_channel(name: str, channel: str, request: Request) -> dict:
    conn = _get(request, name)
    await conn.join(channel)
    return {"status": "ok"}


@router.post("/{name}/part")
async def part_channel(name: str, channel: str, reason: str | None = None, *, request: Request) -> dict:
    conn = _get(request, name)
    await conn.part(channel, reason)
    return {"status": "ok"}


def _get(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc
