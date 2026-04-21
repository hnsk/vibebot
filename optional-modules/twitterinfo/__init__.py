"""`twitterinfo` module — post tweet / x.com status info for links seen in chat.

When a message contains a ``twitter.com`` / ``x.com`` status URL (and a
handful of common mirror hosts: ``fxtwitter.com``, ``vxtwitter.com``,
``fixupx.com``), the bot calls the public syndication endpoint
(``cdn.syndication.twimg.com/tweet-result`` — the same one Twitter uses
to render embedded tweets) and replies with one line containing the
author handle, display name, age, text, and engagement counts. No API
key, no login, no JavaScript engine required — the endpoint returns
JSON directly.

An opt-in fallback to ``api.fxtwitter.com`` can be enabled in settings
for the case where the syndication endpoint is unavailable.

Per-tweet results are cached in an on-disk SQLite file for
``cache_refresh_minutes`` so the same link re-shared is a free hit.
Only IRC bold (``\\x02``) is used — no colour codes — because users run
mixed light/dark clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as html_mod
import logging
import math
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiosqlite
import httpx
from pydantic import BaseModel, Field

from vibebot.core.events import Event
from vibebot.modules.base import Module

if TYPE_CHECKING:
    from vibebot.core.network import NetworkConnection

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"(?:https?://|\bwww\.)(?:[a-z0-9-]+\.)?"
    r"(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)"
    r"\S*",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ".,!?);:]>"
_STATUS_PATH_RE = re.compile(
    r"^/(?:[^/]+/status(?:es)?|i/web/status)/(\d+)(?:/|$)"
)
_TW_HOSTS = {
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "fxtwitter.com",
    "www.fxtwitter.com",
    "vxtwitter.com",
    "www.vxtwitter.com",
    "fixupx.com",
    "www.fixupx.com",
}

_SYNDICATION_URL = "https://cdn.syndication.twimg.com/tweet-result"
_FXTWITTER_URL = "https://api.fxtwitter.com/_/status/{tweet_id}"
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class TwitterInfoSettings(BaseModel):
    reply_format: str = Field(
        default=(
            "\x02@{handle}\x02 ({name}){verified} \u00b7 {age} \u00b7 "
            "{text}{engagement}{reply}{media}{quoted}"
        ),
        description=(
            "Template for the channel reply. Use the toolbar to insert IRC "
            "formatting (bold/italic/underline/colour) — keep colour choices "
            "legible on both dark and light clients."
        ),
        json_schema_extra={
            "ui_widget": "irc_format",
            "ui_variables": {
                "handle": "Twitter handle (without @).",
                "name": "Display name.",
                "verified": "Verified badge marker (empty if not verified).",
                "age": "How long ago the tweet was posted (e.g. 5m, 2h, 3d).",
                "text": "Tweet body.",
                "engagement": "Likes / retweets summary (empty if disabled).",
                "reply": "In-reply-to marker (empty if not a reply).",
                "media": "Attached-media marker (empty if none).",
                "quoted": "Quoted-tweet summary (empty if none).",
            },
        },
    )
    fetch_timeout_seconds: float = Field(
        default=8.0,
        description="Total timeout for one API call (connect + read).",
    )
    cache_refresh_minutes: int = Field(
        default=60,
        description="Refetch tweet metadata if the cached row is older than this.",
    )
    max_reply_len: int = Field(
        default=400,
        description="Truncate the final reply to this many visible characters.",
    )
    show_engagement: bool = Field(
        default=True,
        description="Include \u2665 likes and \u21bb retweets in the reply.",
    )
    expand_quoted_tweet: bool = Field(
        default=True,
        description="Inline a short '@handle: text' form of a quoted tweet when present.",
    )
    fallback_fxtwitter: bool = Field(
        default=False,
        description=(
            "If the primary syndication endpoint fails, fall back to "
            "api.fxtwitter.com (third-party service). Off by default."
        ),
    )
    user_agent: str = Field(
        default="vibebot-twitterinfo/1.0 (+https://github.com/hnsk/vibebot)",
        description="User-Agent header used for outbound API calls.",
    )


class TwitterInfoModule(Module):
    name = "twitterinfo"
    description = (
        "Post tweet / x.com status author, text, age, engagement when a status "
        "URL is seen. Uses Twitter's public syndication endpoint — no API key."
    )
    Settings = TwitterInfoSettings

    def __init__(self, bot: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(bot, config)
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def on_load(self) -> None:
        db_path = self.data_dir / "cache.sqlite"
        self._db = await aiosqlite.connect(str(db_path))
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS tweet_cache ("
            "tweet_id TEXT PRIMARY KEY, "
            "handle TEXT, "
            "name TEXT, "
            "verified INTEGER, "
            "text TEXT, "
            "created_at TEXT, "
            "likes INTEGER, "
            "retweets INTEGER, "
            "replies INTEGER, "
            "reply_to TEXT, "
            "media TEXT, "
            "quoted TEXT, "
            "fetched_at INTEGER NOT NULL)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tw_fetched_at "
            "ON tweet_cache(fetched_at)"
        )
        await self._db.commit()
        self.register_trigger(
            "message",
            handler=self.handle_message,
            regex=(
                r"(?:https?://|\bwww\.)(?:[a-z0-9-]+\.)?"
                r"(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|"
                r"fixupx\.com)\S*"
            ),
            case_sensitive=False,
        )

    async def on_unload(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def handle_message(self, event: Event) -> None:
        message: str = event.get("message", "") or ""
        tweet_ids = _extract_tweet_ids(message)
        if not tweet_ids:
            return

        source: str = event.get("source", "") or ""
        target: str = event.get("target", "") or ""
        if not source or not target:
            return
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        reply_to = target if target.startswith("#") else source

        for tid in tweet_ids:
            await self._process_tweet_id(conn, reply_to, tid)

    async def _process_tweet_id(
        self,
        conn: NetworkConnection,
        reply_to: str,
        tweet_id: str,
    ) -> None:
        ttl = max(0, self.settings.cache_refresh_minutes) * 60
        now = int(time.time())
        cached = await self._cache_get(tweet_id)
        if cached is not None and ttl and (now - cached["fetched_at"]) < ttl:
            if cached["handle"] is None:
                return
            await self._reply(conn, reply_to, cached)
            return

        fetched = await self._fetch_tweet(tweet_id)
        if fetched is None:
            await self._cache_put(tweet_id, None, now)
            return
        await self._cache_put(tweet_id, fetched, now)
        await self._reply(conn, reply_to, fetched)

    async def _reply(
        self,
        conn: NetworkConnection,
        reply_to: str,
        row: dict[str, Any],
    ) -> None:
        fields = _render_fields(
            row,
            show_engagement=self.settings.show_engagement,
            expand_quoted_tweet=self.settings.expand_quoted_tweet,
        )
        try:
            text = self.settings.reply_format.format(**fields)
        except (KeyError, IndexError):
            text = _fallback_render(fields)

        limit = self.settings.max_reply_len
        if _visible_len(text) > limit:
            text = _truncate_bold_aware(
                self.settings.reply_format, fields, limit
            )
        await conn.send_message(reply_to, text)

    async def _cache_get(self, tweet_id: str) -> dict[str, Any] | None:
        if self._db is None:
            return None
        async with (
            self._db_lock,
            self._db.execute(
                "SELECT handle, name, verified, text, created_at, likes, "
                "retweets, replies, reply_to, media, quoted, fetched_at "
                "FROM tweet_cache WHERE tweet_id = ?",
                (tweet_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return None
        return {
            "handle": row[0],
            "name": row[1],
            "verified": bool(row[2]) if row[2] is not None else False,
            "text": row[3] or "",
            "created_at": row[4] or "",
            "likes": int(row[5] or 0),
            "retweets": int(row[6] or 0),
            "replies": int(row[7] or 0),
            "reply_to": row[8] or "",
            "media": row[9] or "",
            "quoted": row[10] or "",
            "fetched_at": int(row[11]),
        }

    async def _cache_put(
        self,
        tweet_id: str,
        data: dict[str, Any] | None,
        fetched_at: int,
    ) -> None:
        if self._db is None:
            return
        row: tuple[Any, ...]
        if data is None:
            row = (tweet_id, None, None, None, None, None, 0, 0, 0, None, None,
                   None, fetched_at)
        else:
            row = (
                tweet_id,
                data.get("handle"),
                data.get("name"),
                1 if data.get("verified") else 0,
                data.get("text", ""),
                data.get("created_at", ""),
                int(data.get("likes", 0)),
                int(data.get("retweets", 0)),
                int(data.get("replies", 0)),
                data.get("reply_to", ""),
                data.get("media", ""),
                data.get("quoted", ""),
                fetched_at,
            )
        async with self._db_lock:
            await self._db.execute(
                "INSERT INTO tweet_cache("
                "tweet_id, handle, name, verified, text, created_at, "
                "likes, retweets, replies, reply_to, media, quoted, "
                "fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tweet_id) DO UPDATE SET "
                "handle=excluded.handle, name=excluded.name, "
                "verified=excluded.verified, text=excluded.text, "
                "created_at=excluded.created_at, likes=excluded.likes, "
                "retweets=excluded.retweets, replies=excluded.replies, "
                "reply_to=excluded.reply_to, media=excluded.media, "
                "quoted=excluded.quoted, fetched_at=excluded.fetched_at",
                row,
            )
            await self._db.commit()

    async def _fetch_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        parsed = await self._fetch_syndication(tweet_id)
        if parsed is not None:
            return parsed
        if self.settings.fallback_fxtwitter:
            return await self._fetch_fxtwitter(tweet_id)
        return None

    async def _fetch_syndication(
        self, tweet_id: str
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.fetch_timeout_seconds,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "application/json",
                },
            ) as client:
                resp = await client.get(
                    _SYNDICATION_URL,
                    params={
                        "id": tweet_id,
                        "token": _calc_token(tweet_id),
                        "lang": "en",
                    },
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.debug(
                "twitterinfo syndication fetch failed for %s: %s",
                tweet_id, exc,
            )
            return None
        return _parse_syndication(data)

    async def _fetch_fxtwitter(
        self, tweet_id: str
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.fetch_timeout_seconds,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "application/json",
                },
            ) as client:
                resp = await client.get(
                    _FXTWITTER_URL.format(tweet_id=tweet_id)
                )
                if resp.status_code in (401, 404):
                    return None
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.debug(
                "twitterinfo fxtwitter fetch failed for %s: %s",
                tweet_id, exc,
            )
            return None
        return _parse_fxtwitter(data)


# --------------------------- pure helpers --------------------------------


def _extract_tweet_ids(message: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(message):
        raw = match.group(0)
        while raw and raw[-1] in _TRAILING_PUNCT:
            raw = raw[:-1]
        if raw.lower().startswith("www."):
            raw = "https://" + raw
        tid = _extract_tweet_id(raw)
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return out


def _extract_tweet_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _TW_HOSTS:
        return None
    path = parsed.path or ""
    m = _STATUS_PATH_RE.match(path)
    if not m:
        return None
    return m.group(1)


def _calc_token(tweet_id: str) -> str:
    """Port of react-tweet's ``getToken``: ``((id/1e15)*PI)`` rendered in
    base 36, with zeros and the decimal point stripped out. Endpoint
    currently accepts any number; this matches what the official embed
    widget sends so we stay inconspicuous if validation tightens.
    """
    try:
        n = int(tweet_id)
    except ValueError:
        return "0"
    val = (n / 1e15) * math.pi
    rendered = _float_to_base36(val)
    stripped = "".join(c for c in rendered if c not in "0.")
    return stripped or "0"


def _float_to_base36(v: float) -> str:
    """Approximate JS ``Number.prototype.toString(36)`` for positive finite v."""
    if v < 0:
        return "-" + _float_to_base36(-v)
    int_part = int(v)
    frac = v - int_part
    if int_part == 0:
        int_str = "0"
    else:
        int_str = ""
        n = int_part
        while n:
            int_str = _BASE36[n % 36] + int_str
            n //= 36
    if frac == 0:
        return int_str
    frac_str = ""
    for _ in range(13):
        frac *= 36
        d = int(frac)
        frac -= d
        frac_str += _BASE36[d]
        if frac == 0:
            break
    return f"{int_str}.{frac_str}"


def _parse_syndication(data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    user = data.get("user") or {}
    handle = user.get("screen_name") or ""
    name = user.get("name") or ""
    if not handle:
        return None
    verified = bool(user.get("verified") or user.get("is_blue_verified"))
    text = _clean_text(
        data.get("text") or "",
        entities=data.get("entities") or {},
        display_text_range=data.get("display_text_range"),
    )
    created_at = data.get("created_at") or ""
    likes = _int(data.get("favorite_count"))
    retweets = _int(data.get("retweet_count"))
    replies = _int(data.get("reply_count") or data.get("conversation_count"))
    reply_to = data.get("in_reply_to_screen_name") or ""
    media = _summarize_media_syndication(data)
    quoted = _summarize_quoted_syndication(data.get("quoted_tweet"))
    return {
        "handle": handle,
        "name": name,
        "verified": verified,
        "text": text,
        "created_at": created_at,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "reply_to": reply_to,
        "media": media,
        "quoted": quoted,
    }


def _parse_fxtwitter(data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    tweet = data.get("tweet") or {}
    author = tweet.get("author") or {}
    handle = author.get("screen_name") or ""
    if not handle:
        return None
    name = author.get("name") or ""
    verified_field = author.get("verified")
    verified = bool(verified_field) and verified_field != "None"
    text = _collapse_ws(tweet.get("text") or "")
    created_at = tweet.get("created_at") or ""
    likes = _int(tweet.get("likes"))
    retweets = _int(tweet.get("retweets"))
    replies = _int(tweet.get("replies"))
    reply_to = ""
    replying_to = tweet.get("replying_to")
    if isinstance(replying_to, str):
        reply_to = replying_to
    elif isinstance(replying_to, dict):
        reply_to = replying_to.get("screen_name") or ""
    media = _summarize_media_fxtwitter(tweet.get("media") or {})
    quoted = ""
    q = tweet.get("quote")
    if isinstance(q, dict):
        q_author = q.get("author") or {}
        qh = q_author.get("screen_name") or ""
        qt = _collapse_ws(q.get("text") or "")
        if qh:
            quoted = f"@{qh}: {qt}" if qt else f"@{qh}"
    return {
        "handle": handle,
        "name": name,
        "verified": verified,
        "text": text,
        "created_at": created_at,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "reply_to": reply_to,
        "media": media,
        "quoted": quoted,
    }


def _clean_text(
    text: str,
    *,
    entities: dict[str, Any],
    display_text_range: list[int] | None,
) -> str:
    """Apply display_text_range, expand t.co URLs, strip trailing media
    t.co, decode HTML entities, collapse whitespace.
    """
    if display_text_range and len(display_text_range) == 2:
        start, end = display_text_range
        with contextlib.suppress(TypeError, ValueError):
            text = text[int(start):int(end)]

    urls = entities.get("urls") or []
    for u in urls:
        short = u.get("url") or ""
        expanded = u.get("expanded_url") or u.get("display_url") or ""
        if short and expanded:
            text = text.replace(short, expanded)

    for m in entities.get("media") or []:
        short = m.get("url") or ""
        if short:
            text = text.replace(short, "")

    text = html_mod.unescape(text)
    return _collapse_ws(text)


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _summarize_media_syndication(data: dict[str, Any]) -> str:
    details = data.get("mediaDetails") or []
    if not details:
        photos = data.get("photos") or []
        if photos:
            return _photo_tag(len(photos))
        if data.get("video"):
            return "video"
        return ""
    photos = sum(1 for d in details if d.get("type") == "photo")
    gifs = sum(1 for d in details if d.get("type") == "animated_gif")
    videos = sum(1 for d in details if d.get("type") == "video")
    parts: list[str] = []
    if photos:
        parts.append(_photo_tag(photos))
    if gifs:
        parts.append("gif" if gifs == 1 else f"{gifs} gifs")
    if videos:
        parts.append("video" if videos == 1 else f"{videos} videos")
    return ", ".join(parts)


def _summarize_media_fxtwitter(media: dict[str, Any]) -> str:
    photos = len(media.get("photos") or [])
    videos = len(media.get("videos") or [])
    parts: list[str] = []
    if photos:
        parts.append(_photo_tag(photos))
    if videos:
        parts.append("video" if videos == 1 else f"{videos} videos")
    return ", ".join(parts)


def _photo_tag(n: int) -> str:
    return "photo" if n == 1 else f"{n} photos"


def _summarize_quoted_syndication(quoted: Any) -> str:
    if not isinstance(quoted, dict):
        return ""
    user = quoted.get("user") or {}
    handle = user.get("screen_name") or ""
    if not handle:
        return ""
    text = _clean_text(
        quoted.get("text") or "",
        entities=quoted.get("entities") or {},
        display_text_range=quoted.get("display_text_range"),
    )
    return f"@{handle}: {text}" if text else f"@{handle}"


def _render_fields(
    row: dict[str, Any],
    *,
    show_engagement: bool,
    expand_quoted_tweet: bool,
) -> dict[str, str]:
    verified = " \u2713" if row.get("verified") else ""
    age = _format_age(row.get("created_at") or "")
    engagement = ""
    if show_engagement:
        bits: list[str] = []
        if row.get("likes"):
            bits.append(f"\u2665{row['likes']}")
        if row.get("retweets"):
            bits.append(f"\u21bb{row['retweets']}")
        if bits:
            engagement = " \u00b7 " + " ".join(bits)
    reply = ""
    if row.get("reply_to"):
        reply = f" \u00b7 \u21b3@{row['reply_to']}"
    media = f" \u00b7 [{row['media']}]" if row.get("media") else ""
    quoted = ""
    if expand_quoted_tweet and row.get("quoted"):
        quoted = f" \u2503 {row['quoted']}"
    return {
        "handle": row.get("handle") or "",
        "name": row.get("name") or "",
        "verified": verified,
        "age": age,
        "text": row.get("text") or "",
        "engagement": engagement,
        "reply": reply,
        "media": media,
        "quoted": quoted,
    }


def _fallback_render(fields: dict[str, str]) -> str:
    return (
        f"@{fields.get('handle', '')} ({fields.get('name', '')})"
        f"{fields.get('verified', '')} \u00b7 {fields.get('age', '')} "
        f"\u00b7 {fields.get('text', '')}"
        f"{fields.get('engagement', '')}{fields.get('reply', '')}"
        f"{fields.get('media', '')}{fields.get('quoted', '')}"
    )


def _format_age(created_at: str, *, now: datetime | None = None) -> str:
    if not created_at:
        return "?"
    stamp = _parse_twitter_ts(created_at)
    if stamp is None:
        return "?"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    delta = now - stamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    if years < 1:
        years = 1
    return f"{years}y ago"


def _parse_twitter_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def _visible_len(text: str) -> int:
    return len(text) - text.count("\x02") - text.count("\x0f")


def _truncate_bold_aware(
    template: str, fields: dict[str, str], limit: int
) -> str:
    """Render ``template`` with ``fields``, shrinking ``text`` until the
    visible length fits ``limit``. Ellipsis is appended inside the text.
    """
    try:
        rendered = template.format(**fields)
    except (KeyError, IndexError):
        rendered = _fallback_render(fields)
        if _visible_len(rendered) <= limit:
            return rendered
        cut = rendered[: limit - 1].rstrip()
        return cut + "\u2026"

    if _visible_len(rendered) <= limit:
        return rendered

    original_text = fields.get("text", "")
    over = _visible_len(rendered) - limit
    keep = max(1, len(original_text) - over - 1)
    while keep > 0:
        truncated = original_text[:keep].rstrip() + "\u2026"
        new_fields = dict(fields)
        new_fields["text"] = truncated
        try:
            candidate = template.format(**new_fields)
        except (KeyError, IndexError):
            break
        if _visible_len(candidate) <= limit:
            return candidate
        keep -= 1

    # Fallback: render without quoted/media and hard-cut.
    stripped = dict(fields)
    stripped["quoted"] = ""
    stripped["media"] = ""
    stripped["text"] = ""
    try:
        bare = template.format(**stripped)
    except (KeyError, IndexError):
        bare = _fallback_render(stripped)
    rendered_cut = bare[: limit - 1].rstrip()
    if "\x02" in bare and rendered_cut.count("\x02") % 2 == 1:
        rendered_cut += "\x02"
    return rendered_cut + "\u2026"
