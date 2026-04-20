"""Channel + user listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from vibebot.api.auth import require_token
from vibebot.core.roster import ChannelRoster, RosterUser

router = APIRouter(prefix="/api/networks/{network}/channels", tags=["channels"], dependencies=[Depends(require_token)])

# Mode-letter priority for picking a single visible prefix per user.
# Ordered most→least privileged; first hit wins.
_MODE_PRIORITY = ("q", "a", "o", "h", "v")


def _visible_prefix(modes: set[str], letter_to_symbol: dict[str, str]) -> str:
    for letter in _MODE_PRIORITY:
        if letter in modes:
            sym = letter_to_symbol.get(letter)
            if sym:
                return sym
    for letter in modes:
        sym = letter_to_symbol.get(letter)
        if sym:
            return sym
    return ""


def _user_objs(client, roster: ChannelRoster, network: str, channel: str) -> list[dict]:
    raw_prefixes = getattr(client, "_nickname_prefixes", {}) or {}
    letter_to_symbol = {letter: symbol for symbol, letter in raw_prefixes.items()}
    users: list[RosterUser] = roster.users(network, channel)
    users.sort(key=lambda u: u.nick.lower())
    return [
        {
            "nick": u.nick,
            "prefix": _visible_prefix(u.modes, letter_to_symbol),
            "ident": u.ident or "*",
            "host": u.host or "*",
            "modes": sorted(u.modes),
        }
        for u in users
    ]


def _topic_info(client, channel: str) -> dict:
    info = getattr(client, "channels", {}).get(channel)
    if not isinstance(info, dict):
        return {"topic": None, "by": None, "set_at": None}
    set_at = info.get("topic_set")
    # topic_set is a datetime; stringify for JSON safety (the UI only needs
    # the human label, not to do date math).
    set_iso = set_at.isoformat() if hasattr(set_at, "isoformat") else (str(set_at) if set_at else None)
    return {
        "topic": info.get("topic"),
        "by": info.get("topic_by"),
        "set_at": set_iso,
    }


@router.get("")
async def list_channels(network: str, request: Request) -> list[dict]:
    conn = _conn(request, network)
    bot = request.app.state.bot
    # pydle's `channels` dict is still authoritative for channel-level metadata
    # (topic/modes). Membership + user masks come from the roster, which is
    # populated by /WHO on join and updated from live events.
    channels = getattr(conn.client, "channels", {}) or {}
    roster_chans = {c.lower() for c in bot.roster.channels(network)}
    names = list(channels.keys())
    for c in bot.roster.channels(network):
        if c not in names and c.lower() in roster_chans:
            names.append(c)
    return [
        {
            "name": name,
            "users": _user_objs(conn.client, bot.roster, network, name),
            **_topic_info(conn.client, name),
        }
        for name in names
    ]


@router.get("/{channel}/users")
async def list_users(network: str, channel: str, request: Request) -> list[dict]:
    conn = _conn(request, network)
    bot = request.app.state.bot
    return _user_objs(conn.client, bot.roster, network, channel)


@router.get("/{channel}/topic")
async def channel_topic(network: str, channel: str, request: Request) -> dict:
    conn = _conn(request, network)
    return _topic_info(conn.client, channel)


@router.get("/{channel}/history")
async def channel_history(network: str, channel: str, request: Request) -> list[dict]:
    bot = request.app.state.bot
    # Validate the network exists so unknown nets return 404 consistently.
    _conn(request, network)
    return bot.history.snapshot(network, channel)


queries_router = APIRouter(
    prefix="/api/networks/{network}/queries",
    tags=["queries"],
    dependencies=[Depends(require_token)],
)


@queries_router.get("")
async def list_queries(network: str, request: Request) -> list[dict]:
    """List PM peers for which we have any recorded history on this network."""
    bot = request.app.state.bot
    _conn(request, network)
    return [{"peer": peer} for peer in bot.history.peers(network)]


@queries_router.get("/{peer}/history")
async def query_history(network: str, peer: str, request: Request) -> list[dict]:
    bot = request.app.state.bot
    _conn(request, network)
    return bot.history.snapshot(network, peer)


@queries_router.delete("/{peer}", status_code=204)
async def close_query(network: str, peer: str, request: Request) -> Response:
    """Close a PM buffer: drop its history so the peer disappears from the sidebar."""
    bot = request.app.state.bot
    _conn(request, network)
    bot.history.clear(network, peer)
    return Response(status_code=204)


def _conn(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc
