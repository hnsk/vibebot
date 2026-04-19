"""Channel + user listing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/networks/{network}/channels", tags=["channels"], dependencies=[Depends(require_token)])

# Mode-letter priority for picking a single visible prefix per user.
# Ordered most→least privileged; first hit wins.
_MODE_PRIORITY = ("q", "a", "o", "h", "v")


def _user_objs(client, channel: str) -> list[dict]:
    info = getattr(client, "channels", {}).get(channel)
    if not isinstance(info, dict):
        return []
    nicks = sorted(info.get("users", set()))
    modes = info.get("modes", {}) if isinstance(info.get("modes"), dict) else {}
    # pydle stores _nickname_prefixes as {symbol: mode_letter} (e.g. {'@': 'o'}).
    # Invert to look up the visible glyph by mode letter.
    raw_prefixes = getattr(client, "_nickname_prefixes", {}) or {}
    letter_to_symbol = {letter: symbol for symbol, letter in raw_prefixes.items()}
    users_dir = getattr(client, "users", {}) or {}

    out: list[dict] = []
    for nick in nicks:
        prefix = ""
        for letter in _MODE_PRIORITY:
            holders = modes.get(letter)
            if holders and nick in holders:
                prefix = letter_to_symbol.get(letter, "")
                break
        if not prefix:
            for letter, symbol in letter_to_symbol.items():
                holders = modes.get(letter)
                if holders and nick in holders:
                    prefix = symbol
                    break
        u = users_dir.get(nick) if isinstance(users_dir, dict) else None
        ident = (u or {}).get("username") if isinstance(u, dict) else None
        host = (u or {}).get("hostname") if isinstance(u, dict) else None
        out.append({
            "nick": nick,
            "prefix": prefix or "",
            "ident": ident or "*",
            "host": host or "*",
        })
    return out


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
    channels = getattr(conn.client, "channels", {}) or {}
    return [
        {
            "name": name,
            "users": _user_objs(conn.client, name),
            **_topic_info(conn.client, name),
        }
        for name in channels
    ]


@router.get("/{channel}/users")
async def list_users(network: str, channel: str, request: Request) -> list[dict]:
    conn = _conn(request, network)
    return _user_objs(conn.client, channel)


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


def _conn(request: Request, name: str):
    bot = request.app.state.bot
    try:
        return bot.networks[name]
    except KeyError as exc:
        raise HTTPException(404, f"Unknown network {name!r}") from exc
