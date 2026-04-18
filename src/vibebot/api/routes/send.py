"""Send messages / perform operator actions as the bot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/networks/{network}", tags=["send"], dependencies=[Depends(require_token)])


class SendBody(BaseModel):
    target: str
    message: str


class OpBody(BaseModel):
    channel: str
    nick: str
    reason: str | None = None


@router.post("/send")
async def send(network: str, body: SendBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.send_message(body.target, body.message)
    return {"status": "ok"}


@router.post("/op")
async def op(network: str, body: OpBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.client.set_mode(body.channel, "+o", body.nick)
    return {"status": "ok"}


@router.post("/deop")
async def deop(network: str, body: OpBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.client.set_mode(body.channel, "-o", body.nick)
    return {"status": "ok"}


@router.post("/kick")
async def kick(network: str, body: OpBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.client.kick(body.channel, body.nick, body.reason or "")
    return {"status": "ok"}


@router.post("/ban")
async def ban(network: str, body: OpBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.client.ban(body.channel, body.nick)
    return {"status": "ok"}


def _conn(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc
