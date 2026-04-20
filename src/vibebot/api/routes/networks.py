"""Network CRUD + status routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/networks", tags=["networks"], dependencies=[Depends(require_token)])


@router.get("")
async def list_networks(request: Request) -> list[dict]:
    bot = request.app.state.bot
    out = []
    for conn in bot.networks.values():
        server = conn.config.default_server
        out.append(
            {
                "name": conn.name,
                "host": server.host if server else None,
                "port": server.port if server else None,
                "tls": server.tls if server else None,
                "connected": conn.connected,
                "channels": list(conn.config.channels),
                "nickname": getattr(conn.client, "nickname", None) or conn.config.nick,
            }
        )
    return out


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
