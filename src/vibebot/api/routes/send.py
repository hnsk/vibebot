"""Send messages and perform operator actions as the bot."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from pydle.protocol import ProtocolViolation

from vibebot.api.auth import require_token
from vibebot.core.events import Event

log = logging.getLogger(__name__)

# Strong refs to fire-and-forget background tasks (whois). Without this the GC
# can drop the task before pydle's whois future resolves.
_BG_TASKS: set[asyncio.Task] = set()

router = APIRouter(
    prefix="/api/networks/{network}",
    tags=["send"],
    dependencies=[Depends(require_token)],
)


class SendBody(BaseModel):
    target: str
    message: str


class ChannelNickBody(BaseModel):
    channel: str
    nick: str
    reason: str | None = None


class ModeBody(BaseModel):
    channel: str
    flags: str
    args: list[str] = Field(default_factory=list)


class TopicBody(BaseModel):
    channel: str
    topic: str | None = None


class NickBody(BaseModel):
    nick: str


class WhoisBody(BaseModel):
    nick: str


class RawBody(BaseModel):
    line: str


@router.post("/send")
async def send(network: str, body: SendBody, request: Request) -> dict:
    conn = _conn(request, network)
    await conn.send_message(body.target, body.message)
    return {"status": "ok"}


@router.post("/op")
async def op(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).set_mode(body.channel, "+o", body.nick)
    return {"status": "ok"}


@router.post("/deop")
async def deop(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).set_mode(body.channel, "-o", body.nick)
    return {"status": "ok"}


@router.post("/voice")
async def voice(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).set_mode(body.channel, "+v", body.nick)
    return {"status": "ok"}


@router.post("/devoice")
async def devoice(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).set_mode(body.channel, "-v", body.nick)
    return {"status": "ok"}


@router.post("/kick")
async def kick(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).kick(body.channel, body.nick, body.reason or "")
    return {"status": "ok"}


@router.post("/ban")
async def ban(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).ban(body.channel, body.nick)
    return {"status": "ok"}


@router.post("/kickban")
async def kickban(network: str, body: ChannelNickBody, request: Request) -> dict:
    await _client(request, network).kickban(body.channel, body.nick, body.reason or "")
    return {"status": "ok"}


@router.post("/mode")
async def mode(network: str, body: ModeBody, request: Request) -> dict:
    if not body.flags:
        raise HTTPException(400, "flags required")
    await _client(request, network).set_mode(body.channel, body.flags, *body.args)
    return {"status": "ok"}


@router.post("/topic")
async def topic(network: str, body: TopicBody, request: Request) -> dict:
    client = _client(request, network)
    if body.topic is None:
        await client.rawmsg("TOPIC", body.channel)
    else:
        await client.set_topic(body.channel, body.topic)
    return {"status": "ok"}


@router.post("/nick")
async def nick(network: str, body: NickBody, request: Request) -> dict:
    if not body.nick:
        raise HTTPException(400, "nick required")
    await _client(request, network).set_nickname(body.nick)
    return {"status": "ok"}


@router.post("/whois", status_code=202)
async def whois(network: str, body: WhoisBody, request: Request) -> dict:
    conn = _conn(request, network)
    if not body.nick:
        raise HTTPException(400, "nick required")
    bus = request.app.state.bot.bus
    task = asyncio.create_task(
        _run_whois(conn, bus, body.nick),
        name=f"whois:{network}:{body.nick}",
    )
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return {"status": "queued", "nick": body.nick}


@router.post("/raw")
async def raw(network: str, body: RawBody, request: Request) -> dict:
    line = body.line.strip()
    if not line:
        raise HTTPException(400, "line required")
    command, args = _parse_raw(line)
    try:
        await _client(request, network).rawmsg(command, *args)
    except ProtocolViolation as exc:
        raise HTTPException(400, f"invalid IRC message: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


def _parse_raw(line: str) -> tuple[str, list[str]]:
    """Parse IRC-style raw line. Trailing param begins with ':' and may contain spaces."""
    head, sep, trailing = line.partition(" :")
    parts = head.split(" ") if head else []
    if sep:
        parts.append(trailing)
    if not parts or not parts[0]:
        raise ValueError("empty command")
    return parts[0], parts[1:]


async def _run_whois(conn, bus, nick: str) -> None:
    network = conn.name
    client = conn.client
    if client is None:
        await bus.publish(Event(kind="whois", network=network, payload={"nick": nick, "error": "not connected"}))
        return
    try:
        info = await client.whois(nick)
    except Exception as exc:
        log.warning("whois %s on %s failed: %s", nick, network, exc)
        await bus.publish(Event(kind="whois", network=network, payload={"nick": nick, "error": str(exc)}))
        return
    payload: dict = {"nick": nick}
    if info:
        for k, v in dict(info).items():
            payload[k] = list(v) if isinstance(v, (set, frozenset)) else v
    await bus.publish(Event(kind="whois", network=network, payload=payload))


def _conn(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc


def _client(request: Request, name: str):
    conn = _conn(request, name)
    if conn.client is None:
        raise HTTPException(409, f"network {name!r} not connected")
    return conn.client
