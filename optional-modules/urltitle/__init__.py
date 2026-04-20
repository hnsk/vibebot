"""`urltitle` module — auto-post page titles for URLs seen in chat.

Scans incoming channel/PM messages for an ``http(s)://`` URL, fetches the
page, extracts a meaningful title (``og:title`` preferred when it carries
more information than ``<title>``), and sends it back to the originating
channel. Non-HTML responses (images, video, binaries) are silently
ignored. A per-module SQLite cache avoids refetching the same URL within
``cache_refresh_minutes``; the reply is still posted on every trigger as
long as a title is known.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx
from pydantic import BaseModel, Field

from vibebot.core.events import Event
from vibebot.modules.base import Module

if TYPE_CHECKING:
    from vibebot.core.network import NetworkConnection

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
_TRAILING_PUNCT = ".,!?);:]>"
_WS_RE = re.compile(r"\s+")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset=['"]?([A-Za-z0-9_\-]+)""",
    re.IGNORECASE,
)
_CTYPE_CHARSET_RE = re.compile(r"charset=([A-Za-z0-9_\-]+)", re.IGNORECASE)
_HTML_CTYPES = ("text/html", "application/xhtml+xml", "application/xml")


class UrlTitleSettings(BaseModel):
    reply_format: str = Field(
        default="\u21aa {title}",
        description="Template for the channel reply. Placeholder: {title}.",
    )
    max_bytes: int = Field(
        default=512 * 1024,
        description="Stop reading the response body after this many bytes.",
    )
    fetch_timeout_seconds: float = Field(
        default=8.0,
        description="Total timeout for one URL fetch (connect + read).",
    )
    cache_refresh_minutes: int = Field(
        default=10,
        description="Refetch the page if the cached row is older than this.",
    )
    max_reply_len: int = Field(
        default=400,
        description="Truncate the final reply to this many characters.",
    )
    user_agent: str = Field(
        default="vibebot-urltitle/1.0 (+https://github.com/hnsk/vibebot)",
        description="User-Agent header used for outbound fetches.",
    )
    ignore_hosts: list[str] = Field(
        default_factory=list,
        description="Hostnames (exact match, lowercase) to skip.",
    )


class UrlTitleModule(Module):
    name = "urltitle"
    description = "Post page title when a URL is seen in chat."
    Settings = UrlTitleSettings

    def __init__(self, bot: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(bot, config)
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()

    async def on_load(self) -> None:
        db_path = self.data_dir / "cache.sqlite"
        self._db = await aiosqlite.connect(str(db_path))
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS url_cache ("
            "url TEXT PRIMARY KEY, "
            "title TEXT, "
            "fetched_at INTEGER NOT NULL)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fetched_at ON url_cache(fetched_at)"
        )
        await self._db.commit()
        self.register_trigger(
            "message",
            handler=self.handle_message,
            regex=r"https?://\S+",
        )

    async def on_unload(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def handle_message(self, event: Event) -> None:
        message: str = event.get("message", "") or ""
        url = _extract_url(message)
        if not url:
            return

        source: str = event.get("source", "") or ""
        target: str = event.get("target", "") or ""
        if not source or not target:
            return
        conn = self.bot.networks.get(event.network)
        if conn is None:
            return
        reply_to = target if target.startswith("#") else source

        host = _hostname(url)
        if host and host in {h.lower() for h in self.settings.ignore_hosts}:
            return

        ttl = max(0, self.settings.cache_refresh_minutes) * 60
        now = int(time.time())
        cached = await self._cache_get(url)
        if cached is not None:
            cached_title, fetched_at = cached
            if ttl and (now - fetched_at) < ttl:
                if cached_title is None:
                    return
                await self._reply(conn, reply_to, cached_title)
                return

        title = await self._fetch_title(url)
        await self._cache_put(url, title, now)
        if title is not None:
            await self._reply(conn, reply_to, title)

    async def _reply(
        self,
        conn: NetworkConnection,
        reply_to: str,
        title: str,
    ) -> None:
        try:
            text = self.settings.reply_format.format(title=title)
        except (KeyError, IndexError):
            text = title
        text = _truncate(text, self.settings.max_reply_len)
        await conn.send_message(reply_to, text)

    async def _cache_get(self, url: str) -> tuple[str | None, int] | None:
        if self._db is None:
            return None
        async with (
            self._db_lock,
            self._db.execute(
                "SELECT title, fetched_at FROM url_cache WHERE url = ?",
                (url,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], int(row[1]))

    async def _cache_put(self, url: str, title: str | None, fetched_at: int) -> None:
        if self._db is None:
            return
        async with self._db_lock:
            await self._db.execute(
                "INSERT INTO url_cache(url, title, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET title=excluded.title, "
                "fetched_at=excluded.fetched_at",
                (url, title, fetched_at),
            )
            await self._db.commit()

    async def _fetch_title(self, url: str) -> str | None:
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self.settings.fetch_timeout_seconds,
                    follow_redirects=True,
                    max_redirects=5,
                    headers={
                        "User-Agent": self.settings.user_agent,
                        "Accept-Language": "en;q=0.5",
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                    },
                ) as client,
                client.stream("GET", url) as resp,
            ):
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").lower()
                if not any(ctype.startswith(t) for t in _HTML_CTYPES):
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= self.settings.max_bytes:
                        break
                charset = _detect_charset(ctype, bytes(buf))
                text = bytes(buf).decode(charset, errors="replace")
            return _extract_title(text)
        except (httpx.HTTPError, UnicodeError, ValueError, OSError) as exc:
            log.debug("urltitle fetch failed for %s: %s", url, exc)
            return None


def _extract_url(message: str) -> str | None:
    match = _URL_RE.search(message)
    if not match:
        return None
    url = match.group(0)
    while url and url[-1] in _TRAILING_PUNCT:
        url = url[:-1]
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _hostname(url: str) -> str | None:
    m = re.match(r"https?://([^/:?#]+)", url, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower()


def _detect_charset(content_type: str, body: bytes) -> str:
    m = _CTYPE_CHARSET_RE.search(content_type)
    if m:
        return m.group(1)
    sniff = body[:2048]
    m2 = _META_CHARSET_RE.search(sniff)
    if m2:
        try:
            return m2.group(1).decode("ascii")
        except UnicodeDecodeError:
            pass
    return "utf-8"


class _Done(Exception):
    pass


class _HeadParser(HTMLParser):
    """Collects <title>, og:title, and og:description from the document head.

    Stops parsing as soon as </head> is seen by raising _Done.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.og_title: str | None = None
        self.og_description: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            prop = a.get("property", "").lower()
            name = a.get("name", "").lower()
            content = a.get("content", "")
            if not content:
                return
            if prop == "og:title" and self.og_title is None:
                self.og_title = content
            elif prop == "og:description" and self.og_description is None:
                self.og_description = content
            elif name == "twitter:title" and self.og_title is None:
                self.og_title = content

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            raise _Done()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)


def _extract_title(page: str) -> str | None:
    parser = _HeadParser()
    try:
        parser.feed(page)
        parser.close()
    except _Done:
        pass
    except Exception:
        pass

    raw_title = _normalize("".join(parser.title_parts))
    og_title = _normalize(parser.og_title or "")

    chosen = _pick_title(raw_title, og_title)
    if not chosen:
        og_desc = _normalize(parser.og_description or "")
        if og_desc:
            return html.unescape(og_desc)
        return None
    return html.unescape(chosen)


def _pick_title(title: str, og_title: str) -> str:
    if og_title and _og_is_better(title, og_title):
        return og_title
    return title or og_title


def _og_is_better(title: str, og_title: str) -> bool:
    if not title:
        return True
    if len(title) < 8:
        return True
    t = title.lower()
    o = og_title.lower()
    if o == t:
        return False
    if t in o and len(og_title) > len(title) + 4:
        return True
    if o.startswith(t) or t.startswith(o):
        return len(og_title) > len(title)
    return False


def _normalize(value: str) -> str:
    return _WS_RE.sub(" ", value).strip()


def _truncate(text: str, limit: int) -> str:
    if limit <= 1 or len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= int(limit * 0.6):
        cut = cut[:space]
    return cut.rstrip() + "\u2026"
