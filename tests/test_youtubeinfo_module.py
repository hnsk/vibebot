"""Tests for the youtubeinfo optional module."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.core.events import Event

_YTI_PATH = (
    Path(__file__).resolve().parent.parent
    / "optional-modules"
    / "youtubeinfo"
    / "__init__.py"
)
_spec = importlib.util.spec_from_file_location(
    "vibebot_module._test.youtubeinfo", _YTI_PATH
)
assert _spec is not None and _spec.loader is not None
_yti_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _yti_mod
_spec.loader.exec_module(_yti_mod)

YouTubeInfoModule = _yti_mod.YouTubeInfoModule
YouTubeInfoSettings = _yti_mod.YouTubeInfoSettings
_extract_video_id = _yti_mod._extract_video_id
_extract_video_ids = _yti_mod._extract_video_ids
_format_duration = _yti_mod._format_duration
_format_age = _yti_mod._format_age
_visible_len = _yti_mod._visible_len

TEST_REPO = "vibebot-optional"

# ------------------------------- video id ------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("http://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=10", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube.com/live/aBcDeFgHiJk", "aBcDeFgHiJk"),
        ("https://www.youtube.com/embed/aBcDeFgHiJk", "aBcDeFgHiJk"),
    ],
)
def test_extract_video_id_valid(url: str, expected: str) -> None:
    assert _extract_video_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=tooShort",
        "https://www.youtube.com/watch",
        "https://youtu.be/",
        "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx",
    ],
)
def test_extract_video_id_invalid(url: str) -> None:
    assert _extract_video_id(url) is None


def test_extract_video_ids_in_surrounding_text() -> None:
    msg = (
        "hey check this out https://www.youtube.com/watch?v=dQw4w9WgXcQ, "
        "also www.youtube.com/shorts/aBcDeFgHiJk! the end."
    )
    assert _extract_video_ids(msg) == ["dQw4w9WgXcQ", "aBcDeFgHiJk"]


def test_extract_video_ids_deduplicates_by_id() -> None:
    msg = (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ "
        "https://youtu.be/dQw4w9WgXcQ?t=10"
    )
    assert _extract_video_ids(msg) == ["dQw4w9WgXcQ"]


# ------------------------------- duration ------------------------------------


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("PT1H2M3S", "1:02:03"),
        ("PT4M5S", "4:05"),
        ("PT42S", "0:42"),
        ("PT1M", "1:00"),
        ("PT2H", "2:00:00"),
        ("PT25H30M", "25:30:00"),
        ("P1DT2H3M4S", "26:03:04"),
        ("P0D", "LIVE"),
        ("", "LIVE"),
        ("GARBAGE", "LIVE"),
    ],
)
def test_format_duration(iso: str, expected: str) -> None:
    assert _format_duration(iso) == expected


# ---------------------------------- age --------------------------------------


def test_format_age_buckets() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    cases = [
        (now - timedelta(seconds=5), "just now"),
        (now - timedelta(seconds=30), "just now"),
        (now - timedelta(minutes=5), "5m ago"),
        (now - timedelta(hours=3), "3h ago"),
        (now - timedelta(days=2), "2d ago"),
        (now - timedelta(days=45), "1mo ago"),
        (now - timedelta(days=400), "1y ago"),
        (now - timedelta(days=365 * 3), "3y ago"),
    ]
    for stamp, expected in cases:
        assert _format_age(stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), now=now) == expected


def test_format_age_invalid() -> None:
    assert _format_age("") == "?"
    assert _format_age("not-a-date") == "?"


# ------------------------- module integration --------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.client = SimpleNamespace(users={})

    async def send_message(self, target: str, message: str) -> None:
        self.sent.append((target, message))


def _make_bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "mdata"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["t0ken"]),
        networks=[],
        repos=[RepoConfig(name="sample", url="https://example.com/x.git")],
    )
    return VibeBot(cfg)


@pytest.fixture()
async def bot(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    try:
        yield b
    finally:
        await b.db.close()


async def _install_module(
    bot: VibeBot,
    conn: _FakeConn,
    *,
    settings: YouTubeInfoSettings | None = None,
) -> YouTubeInfoModule:
    module = YouTubeInfoModule(bot)
    module._repo = TEST_REPO
    module._name = "youtubeinfo"
    module.settings = settings or YouTubeInfoSettings(api_key=SecretStr("k"))
    await module.on_load()
    bot.modules._register_triggers(
        TEST_REPO, "youtubeinfo", module, YouTubeInfoModule
    )
    bot.networks["mock"] = conn  # type: ignore[assignment]
    return module


def _event(message: str, target: str = "#room", source: str = "alice") -> Event:
    return Event(
        kind="message",
        network="mock",
        payload={"message": message, "source": source, "target": target},
    )


def _api_payload(
    *,
    title: str = "Rick Astley - Never Gonna Give You Up",
    channel: str = "Rick Astley",
    duration: str = "PT3M32S",
    published: str = "2009-10-25T06:57:33Z",
) -> dict[str, Any]:
    return {
        "items": [
            {
                "snippet": {
                    "title": title,
                    "channelTitle": channel,
                    "publishedAt": published,
                },
                "contentDetails": {"duration": duration},
            }
        ]
    }


class _Counter:
    def __init__(self) -> None:
        self.calls = 0


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> _Counter:
    """Replace ``httpx.AsyncClient`` inside the loaded module with one
    wired to ``MockTransport(handler)``. Returns a counter of HTTP calls.
    """
    counter = _Counter()

    def counting(request: httpx.Request) -> httpx.Response:
        counter.calls += 1
        return handler(request)

    transport = httpx.MockTransport(counting)
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(_yti_mod.httpx, "AsyncClient", factory)
    return counter


async def test_handle_message_happy_path(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "key=k" in str(request.url)
        assert "id=dQw4w9WgXcQ" in str(request.url)
        return httpx.Response(200, json=_api_payload())

    _install_transport(monkeypatch, handler)

    await module.handle_message(
        _event("look https://www.youtube.com/watch?v=dQw4w9WgXcQ here")
    )

    assert len(conn.sent) == 1
    target, text = conn.sent[0]
    assert target == "#room"
    assert "\x02Rick Astley - Never Gonna Give You Up\x02" in text
    assert "Rick Astley" in text
    assert "3:32" in text
    assert "ago" in text


async def test_cache_hit_avoids_second_fetch(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_api_payload())

    counter = _install_transport(monkeypatch, handler)

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )
    await module.handle_message(
        _event("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    )

    assert counter.calls == 1
    assert len(conn.sent) == 2
    assert conn.sent[0][1] == conn.sent[1][1]


async def test_empty_items_response_silent(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    _install_transport(monkeypatch, handler)

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )

    assert conn.sent == []


async def test_api_key_unset_is_inert(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(
        bot, conn, settings=YouTubeInfoSettings(api_key=SecretStr(""))
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP should be made when api_key is empty")

    _install_transport(monkeypatch, handler)

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )

    assert conn.sent == []


async def test_pm_replies_to_sender(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_api_payload()),
    )

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ", target="vibebot")
    )

    assert conn.sent
    assert conn.sent[0][0] == "alice"


async def test_reply_contains_no_colour_codes(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_api_payload()),
    )

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )

    text = conn.sent[0][1]
    assert "\x03" not in text
    assert "\x0f" not in text


async def test_truncate_respects_bold(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(
        bot,
        conn,
        settings=YouTubeInfoSettings(
            api_key=SecretStr("k"),
            max_reply_len=60,
        ),
    )
    long_title = "A" * 200
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_api_payload(title=long_title)),
    )

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )

    text = conn.sent[0][1]
    assert _visible_len(text) <= 60
    assert text.count("\x02") == 2  # opening + closing bold preserved


async def test_http_error_is_swallowed(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(500, json={"error": "boom"}),
    )

    await module.handle_message(
        _event("https://youtu.be/dQw4w9WgXcQ")
    )

    assert conn.sent == []


async def test_non_youtube_url_ignored_by_trigger(bot: VibeBot) -> None:
    conn = _FakeConn()
    await _install_module(bot, conn)
    registry = bot.modules._registry
    assert list(registry.match(_event("https://example.com/foo"))) == []
    assert list(registry.match(_event("https://vimeo.com/123"))) == []
    assert (
        list(registry.match(_event("https://youtu.be/dQw4w9WgXcQ"))) != []
    )
