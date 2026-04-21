"""`youtubeinfo` module — post YouTube video info for links seen in chat.

When a message contains a YouTube URL (``youtube.com/watch``, ``youtu.be``,
``/shorts/``, ``/live/``, ``/embed/``; ``m.``/``music.`` subdomains included),
the bot calls the YouTube Data API v3 ``videos.list`` endpoint and replies
with a line containing the title, channel, duration, and age. Only IRC bold
(``\\x02``) is used — no colour codes — because users run mixed light/dark
clients.

Per-video results are cached in an on-disk SQLite file for
``cache_refresh_minutes`` so the same link re-shared is a free hit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import aiosqlite
import httpx
from pydantic import BaseModel, Field, SecretStr

from vibebot.core.events import Event
from vibebot.modules.base import Module

if TYPE_CHECKING:
    from vibebot.core.network import NetworkConnection

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"(?:https?://|\bwww\.)(?:[a-z0-9-]+\.)?(?:youtube\.com|youtu\.be)\S*",
    re.IGNORECASE,
)
_TRAILING_PUNCT = ".,!?);:]>"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_API_URL = "https://www.googleapis.com/youtube/v3/videos"
_DUR_RE = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_PATH_ID_PREFIXES = ("/shorts/", "/live/", "/embed/", "/v/")


class YouTubeInfoSettings(BaseModel):
    api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "YouTube Data API v3 key. Module stays inert until a key is set."
        ),
    )
    reply_format: str = Field(
        default="\x02{title}\x02 \u2014 {channel} \u00b7 {duration} \u00b7 {age}",
        description=(
            "Template for the channel reply. Use the toolbar to insert IRC "
            "formatting (bold/italic/underline/colour) — keep colour choices "
            "legible on both dark and light clients."
        ),
        json_schema_extra={
            "ui_widget": "irc_format",
            "ui_variables": {
                "title": "Video title.",
                "channel": "Channel name.",
                "duration": "Runtime, e.g. 4:12 or 1:02:30.",
                "age": "How long ago the video was published.",
            },
        },
    )
    fetch_timeout_seconds: float = Field(
        default=8.0,
        description="Total timeout for one API call (connect + read).",
    )
    cache_refresh_minutes: int = Field(
        default=60,
        description="Refetch video metadata if the cached row is older than this.",
    )
    max_reply_len: int = Field(
        default=400,
        description="Truncate the final reply to this many visible characters.",
    )
    user_agent: str = Field(
        default="vibebot-youtubeinfo/1.0 (+https://github.com/hnsk/vibebot)",
        description="User-Agent header used for outbound API calls.",
    )


class YouTubeInfoModule(Module):
    name = "youtubeinfo"
    description = (
        "Post YouTube video title/channel/duration/age when a YouTube URL is seen."
    )
    Settings = YouTubeInfoSettings

    def __init__(self, bot: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(bot, config)
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()
        self._warned_no_key = False

    async def on_load(self) -> None:
        db_path = self.data_dir / "cache.sqlite"
        self._db = await aiosqlite.connect(str(db_path))
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS video_cache ("
            "video_id TEXT PRIMARY KEY, "
            "title TEXT, "
            "channel TEXT, "
            "duration TEXT, "
            "published_at TEXT, "
            "fetched_at INTEGER NOT NULL)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fetched_at ON video_cache(fetched_at)"
        )
        await self._db.commit()
        self.register_trigger(
            "message",
            handler=self.handle_message,
            regex=(
                r"(?:https?://|\bwww\.)(?:[a-z0-9-]+\.)?"
                r"(?:youtube\.com|youtu\.be)\S*"
            ),
            case_sensitive=False,
        )

    async def on_unload(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def handle_message(self, event: Event) -> None:
        message: str = event.get("message", "") or ""
        video_ids = _extract_video_ids(message)
        if not video_ids:
            return

        source: str = event.get("source", "") or ""
        target: str = event.get("target", "") or ""
        if not source or not target:
            return
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        reply_to = target if target.startswith("#") else source

        for vid in video_ids:
            await self._process_video_id(conn, reply_to, vid)

    async def _process_video_id(
        self,
        conn: NetworkConnection,
        reply_to: str,
        video_id: str,
    ) -> None:
        api_key = self.settings.api_key.get_secret_value()
        if not api_key:
            if not self._warned_no_key:
                log.debug(
                    "youtubeinfo: api_key unset; module inert until configured"
                )
                self._warned_no_key = True
            return

        ttl = max(0, self.settings.cache_refresh_minutes) * 60
        now = int(time.time())
        cached = await self._cache_get(video_id)
        if cached is not None:
            title, channel, duration, published, fetched_at = cached
            if ttl and (now - fetched_at) < ttl:
                if title is None:
                    return
                await self._reply(
                    conn, reply_to, title, channel or "", duration or "",
                    published or "",
                )
                return

        fetched = await self._fetch_video(video_id, api_key)
        if fetched is None:
            await self._cache_put(video_id, None, None, None, None, now)
            return
        title, channel, duration, published = fetched
        await self._cache_put(video_id, title, channel, duration, published, now)
        await self._reply(conn, reply_to, title, channel, duration, published)

    async def _reply(
        self,
        conn: NetworkConnection,
        reply_to: str,
        title: str,
        channel: str,
        duration_iso: str,
        published_iso: str,
    ) -> None:
        duration = _format_duration(duration_iso)
        age = _format_age(published_iso)
        fields = {
            "title": title,
            "channel": channel,
            "duration": duration,
            "age": age,
        }
        try:
            text = self.settings.reply_format.format(**fields)
        except (KeyError, IndexError):
            text = f"{title} \u2014 {channel} \u00b7 {duration} \u00b7 {age}"

        limit = self.settings.max_reply_len
        if _visible_len(text) > limit:
            text = _truncate_bold_aware(
                self.settings.reply_format, fields, limit
            )
        await conn.send_message(reply_to, text)

    async def _cache_get(
        self, video_id: str
    ) -> tuple[str | None, str | None, str | None, str | None, int] | None:
        if self._db is None:
            return None
        async with (
            self._db_lock,
            self._db.execute(
                "SELECT title, channel, duration, published_at, fetched_at "
                "FROM video_cache WHERE video_id = ?",
                (video_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1], row[2], row[3], int(row[4]))

    async def _cache_put(
        self,
        video_id: str,
        title: str | None,
        channel: str | None,
        duration: str | None,
        published_at: str | None,
        fetched_at: int,
    ) -> None:
        if self._db is None:
            return
        async with self._db_lock:
            await self._db.execute(
                "INSERT INTO video_cache("
                "video_id, title, channel, duration, published_at, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(video_id) DO UPDATE SET "
                "title=excluded.title, channel=excluded.channel, "
                "duration=excluded.duration, "
                "published_at=excluded.published_at, "
                "fetched_at=excluded.fetched_at",
                (video_id, title, channel, duration, published_at, fetched_at),
            )
            await self._db.commit()

    async def _fetch_video(
        self, video_id: str, api_key: str
    ) -> tuple[str, str, str, str] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.fetch_timeout_seconds,
                headers={
                    "User-Agent": self.settings.user_agent,
                    "Accept": "application/json",
                },
            ) as client:
                resp = await client.get(
                    _API_URL,
                    params={
                        "part": "snippet,contentDetails",
                        "id": video_id,
                        "key": api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.debug("youtubeinfo fetch failed for %s: %s", video_id, exc)
            return None

        try:
            items = data.get("items") or []
            if not items:
                return None
            item = items[0]
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            title = snippet.get("title") or ""
            channel = snippet.get("channelTitle") or ""
            published = snippet.get("publishedAt") or ""
            duration = content.get("duration") or ""
            if not title:
                return None
            return (title, channel, duration, published)
        except (KeyError, IndexError, TypeError) as exc:
            log.debug("youtubeinfo: unexpected API shape for %s: %s", video_id, exc)
            return None


# --------------------------- pure helpers --------------------------------


def _extract_video_ids(message: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(message):
        raw = match.group(0)
        while raw and raw[-1] in _TRAILING_PUNCT:
            raw = raw[:-1]
        if raw.lower().startswith("www."):
            raw = "https://" + raw
        vid = _extract_video_id(raw)
        if vid is None or vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
    return out


def _extract_video_id(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _YT_HOSTS:
        return None
    path = parsed.path or ""
    if host.endswith("youtu.be"):
        segment = path.lstrip("/").split("/", 1)[0]
        return segment if _ID_RE.match(segment) else None
    if path == "/watch" or path.startswith("/watch/"):
        q = parse_qs(parsed.query)
        vals = q.get("v") or []
        if vals and _ID_RE.match(vals[0]):
            return vals[0]
        return None
    for prefix in _PATH_ID_PREFIXES:
        if path.startswith(prefix):
            segment = path[len(prefix):].split("/", 1)[0]
            return segment if _ID_RE.match(segment) else None
    return None


def _format_duration(iso: str) -> str:
    if not iso or iso == "P0D":
        return "LIVE"
    m = _DUR_RE.match(iso)
    if not m:
        return "LIVE"
    d, h, mn, s = (int(x) if x else 0 for x in m.groups())
    total_h = d * 24 + h
    if total_h:
        return f"{total_h}:{mn:02d}:{s:02d}"
    return f"{mn}:{s:02d}"


def _format_age(published_iso: str, *, now: datetime | None = None) -> str:
    if not published_iso:
        return "?"
    try:
        stamp = datetime.fromisoformat(published_iso.replace("Z", "+00:00"))
    except ValueError:
        return "?"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    delta = now - stamp
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
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


def _visible_len(text: str) -> int:
    return len(text) - text.count("\x02") - text.count("\x0f")


def _truncate_bold_aware(
    template: str, fields: dict[str, str], limit: int
) -> str:
    """Render ``template`` with ``fields``, shrinking ``title`` until the
    visible length fits ``limit``. Ellipsis is appended inside the bold run.
    """
    try:
        rendered = template.format(**fields)
    except (KeyError, IndexError):
        rendered = (
            f"{fields.get('title', '')} \u2014 {fields.get('channel', '')} "
            f"\u00b7 {fields.get('duration', '')} \u00b7 "
            f"{fields.get('age', '')}"
        )
        if _visible_len(rendered) <= limit:
            return rendered
        cut = rendered[: limit - 1].rstrip()
        return cut + "\u2026"

    original_title = fields.get("title", "")
    if _visible_len(rendered) <= limit:
        return rendered

    over = _visible_len(rendered) - limit
    keep = max(1, len(original_title) - over - 1)
    while keep > 0:
        truncated = original_title[:keep].rstrip() + "\u2026"
        new_fields = dict(fields)
        new_fields["title"] = truncated
        try:
            candidate = template.format(**new_fields)
        except (KeyError, IndexError):
            break
        if _visible_len(candidate) <= limit:
            return candidate
        keep -= 1

    # Fallback: render without bold-aware budget and hard-cut.
    rendered_cut = rendered[: limit - 1].rstrip()
    if "\x02" in rendered and rendered_cut.count("\x02") % 2 == 1:
        rendered_cut += "\x02"
    return rendered_cut + "\u2026"
