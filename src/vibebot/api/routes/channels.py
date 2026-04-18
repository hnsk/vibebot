"""Channel + user listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/networks/{network}/channels", tags=["channels"], dependencies=[Depends(require_token)])


@router.get("")
async def list_channels(network: str, request: Request) -> list[dict]:
    conn = _conn(request, network)
    channels = getattr(conn.client, "channels", {}) or {}
    return [
        {"name": name, "users": sorted(info.get("users", set())) if isinstance(info, dict) else []}
        for name, info in channels.items()
    ]


@router.get("/{channel}/users")
async def list_users(network: str, channel: str, request: Request) -> list[str]:
    conn = _conn(request, network)
    return conn.channel_users(channel)


def _conn(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc
