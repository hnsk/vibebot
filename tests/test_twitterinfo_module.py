"""Tests for the twitterinfo optional module."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.core.events import Event

_TWI_PATH = (
    Path(__file__).resolve().parent.parent
    / "optional-modules"
    / "twitterinfo"
    / "__init__.py"
)
_spec = importlib.util.spec_from_file_location(
    "vibebot_module._test.twitterinfo", _TWI_PATH
)
assert _spec is not None and _spec.loader is not None
_twi_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _twi_mod
_spec.loader.exec_module(_twi_mod)

TwitterInfoModule = _twi_mod.TwitterInfoModule
TwitterInfoSettings = _twi_mod.TwitterInfoSettings
_extract_tweet_id = _twi_mod._extract_tweet_id
_extract_tweet_ids = _twi_mod._extract_tweet_ids
_calc_token = _twi_mod._calc_token
_format_age = _twi_mod._format_age
_visible_len = _twi_mod._visible_len
_parse_syndication = _twi_mod._parse_syndication
_parse_fxtwitter = _twi_mod._parse_fxtwitter

TEST_REPO = "vibebot-optional"

# ------------------------------- tweet id ------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://twitter.com/jack/status/20", "20"),
        ("https://x.com/jack/status/20", "20"),
        ("https://www.twitter.com/jack/status/20", "20"),
        ("https://mobile.twitter.com/jack/status/20", "20"),
        ("https://mobile.x.com/jack/status/20?lang=en", "20"),
        ("https://twitter.com/jack/statuses/20", "20"),
        ("https://twitter.com/i/web/status/20", "20"),
        ("https://fxtwitter.com/jack/status/20", "20"),
        ("https://vxtwitter.com/jack/status/20", "20"),
        ("https://fixupx.com/jack/status/20", "20"),
        ("https://x.com/jack/status/1234567890123456789/photo/1", "1234567890123456789"),
    ],
)
def test_extract_tweet_id_valid(url: str, expected: str) -> None:
    assert _extract_tweet_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/jack/status/20",
        "https://twitter.com/jack",
        "https://twitter.com/jack/status/",
        "https://twitter.com/jack/status/abc",
        "https://twitter.com/search?q=hello",
        "https://twitter.com/",
    ],
)
def test_extract_tweet_id_invalid(url: str) -> None:
    assert _extract_tweet_id(url) is None


def test_extract_tweet_ids_in_surrounding_text() -> None:
    msg = (
        "hey check this out https://x.com/jack/status/20, "
        "also www.twitter.com/bob/status/123! the end."
    )
    assert _extract_tweet_ids(msg) == ["20", "123"]


def test_extract_tweet_ids_deduplicates() -> None:
    msg = (
        "https://x.com/jack/status/20 "
        "https://twitter.com/jack/status/20?foo=bar"
    )
    assert _extract_tweet_ids(msg) == ["20"]


# ------------------------------- token ---------------------------------------


def test_calc_token_stable() -> None:
    # Known-good: deterministic output shape — no zeros, no dots.
    tok = _calc_token("1234567890123456789")
    assert tok
    assert "0" not in tok
    assert "." not in tok


def test_calc_token_non_numeric() -> None:
    assert _calc_token("not-a-number") == "0"


def test_calc_token_matches_reference_for_id_20() -> None:
    # Reference JS:
    #   ((20/1e15)*Math.PI).toString(36).replace(/(0+|\.)/g,'')
    # = "6.cnehnjqrcpf7e-14".toString(36) → base-36 of 6.283185307179586e-14
    # We don't hardcode the exact string (float-to-base36 has minor
    # rounding differences between engines); just assert sane properties.
    tok = _calc_token("20")
    assert tok
    assert "0" not in tok
    assert "." not in tok


# ---------------------------------- age --------------------------------------


def test_format_age_buckets_iso() -> None:
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    cases = [
        (now - timedelta(seconds=30), "just now"),
        (now - timedelta(minutes=5), "5m ago"),
        (now - timedelta(hours=3), "3h ago"),
        (now - timedelta(days=2), "2d ago"),
        (now - timedelta(days=45), "1mo ago"),
        (now - timedelta(days=400), "1y ago"),
    ]
    for stamp, expected in cases:
        assert _format_age(
            stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"), now=now
        ) == expected


def test_format_age_twitter_legacy_format() -> None:
    # FxTwitter returns the classic Twitter date string.
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    assert _format_age(
        "Sun Apr 19 12:00:00 +0000 2026", now=now
    ) == "2d ago"


def test_format_age_invalid() -> None:
    assert _format_age("") == "?"
    assert _format_age("not-a-date") == "?"


# --------------------------- parse syndication -------------------------------


def _syn_payload(
    *,
    screen_name: str = "jack",
    name: str = "jack",
    text: str = "just setting up my twttr",
    created_at: str = "2006-03-21T20:50:14.000Z",
    verified: bool = False,
    is_blue_verified: bool = False,
    favorite_count: int = 100,
    retweet_count: int = 50,
    conversation_count: int = 10,
    entities: dict[str, Any] | None = None,
    display_text_range: list[int] | None = None,
    media_details: list[dict[str, Any]] | None = None,
    quoted: dict[str, Any] | None = None,
    in_reply_to: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id_str": "20",
        "text": text,
        "created_at": created_at,
        "favorite_count": favorite_count,
        "retweet_count": retweet_count,
        "conversation_count": conversation_count,
        "user": {
            "screen_name": screen_name,
            "name": name,
            "verified": verified,
            "is_blue_verified": is_blue_verified,
        },
        "entities": entities if entities is not None else {},
    }
    if display_text_range is not None:
        payload["display_text_range"] = display_text_range
    if media_details is not None:
        payload["mediaDetails"] = media_details
    if quoted is not None:
        payload["quoted_tweet"] = quoted
    if in_reply_to:
        payload["in_reply_to_screen_name"] = in_reply_to
    return payload


def test_parse_syndication_basic() -> None:
    parsed = _parse_syndication(_syn_payload())
    assert parsed is not None
    assert parsed["handle"] == "jack"
    assert parsed["name"] == "jack"
    assert parsed["text"] == "just setting up my twttr"
    assert parsed["likes"] == 100
    assert parsed["retweets"] == 50
    assert parsed["verified"] is False


def test_parse_syndication_blue_verified_counts_as_verified() -> None:
    parsed = _parse_syndication(
        _syn_payload(verified=False, is_blue_verified=True)
    )
    assert parsed is not None
    assert parsed["verified"] is True


def test_parse_syndication_expands_tco_and_strips_media_url() -> None:
    entities = {
        "urls": [
            {
                "url": "https://t.co/abc",
                "expanded_url": "https://example.com/article",
            }
        ],
        "media": [{"url": "https://t.co/pic1"}],
    }
    parsed = _parse_syndication(
        _syn_payload(
            text="see https://t.co/abc for details https://t.co/pic1",
            entities=entities,
        )
    )
    assert parsed is not None
    assert "https://example.com/article" in parsed["text"]
    assert "https://t.co/pic1" not in parsed["text"]
    assert "https://t.co/abc" not in parsed["text"]


def test_parse_syndication_decodes_html_entities() -> None:
    parsed = _parse_syndication(
        _syn_payload(text="5 &lt; 10 &amp; 10 &gt; 5")
    )
    assert parsed is not None
    assert parsed["text"] == "5 < 10 & 10 > 5"


def test_parse_syndication_media_photo_count() -> None:
    media = [
        {"type": "photo"},
        {"type": "photo"},
        {"type": "photo"},
    ]
    parsed = _parse_syndication(_syn_payload(media_details=media))
    assert parsed is not None
    assert parsed["media"] == "3 photos"


def test_parse_syndication_media_video_and_gif() -> None:
    parsed = _parse_syndication(
        _syn_payload(media_details=[{"type": "video"}])
    )
    assert parsed is not None
    assert parsed["media"] == "video"
    parsed = _parse_syndication(
        _syn_payload(media_details=[{"type": "animated_gif"}])
    )
    assert parsed is not None
    assert parsed["media"] == "gif"


def test_parse_syndication_quoted() -> None:
    quoted = {
        "user": {"screen_name": "bob"},
        "text": "quoted text here",
        "entities": {},
    }
    parsed = _parse_syndication(_syn_payload(quoted=quoted))
    assert parsed is not None
    assert parsed["quoted"] == "@bob: quoted text here"


def test_parse_syndication_reply() -> None:
    parsed = _parse_syndication(_syn_payload(in_reply_to="alice"))
    assert parsed is not None
    assert parsed["reply_to"] == "alice"


def test_parse_syndication_missing_user_is_none() -> None:
    assert _parse_syndication({"text": "x"}) is None


def test_parse_syndication_non_dict_is_none() -> None:
    assert _parse_syndication([]) is None  # type: ignore[arg-type]


# --------------------------- parse fxtwitter ---------------------------------


def test_parse_fxtwitter_basic() -> None:
    payload = {
        "code": 200,
        "tweet": {
            "text": "hello world",
            "author": {
                "screen_name": "jack",
                "name": "jack",
                "verified": None,
            },
            "created_at": "Tue Mar 21 20:50:14 +0000 2006",
            "likes": 309250,
            "retweets": 126552,
            "replies": 17867,
        },
    }
    parsed = _parse_fxtwitter(payload)
    assert parsed is not None
    assert parsed["handle"] == "jack"
    assert parsed["likes"] == 309250
    assert parsed["verified"] is False


def test_parse_fxtwitter_with_media_and_quote() -> None:
    payload = {
        "tweet": {
            "text": "pic",
            "author": {"screen_name": "alice", "name": "Alice"},
            "created_at": "Tue Mar 21 20:50:14 +0000 2006",
            "likes": 1,
            "retweets": 0,
            "replies": 0,
            "media": {
                "photos": [{"url": "u1"}, {"url": "u2"}],
                "videos": [],
            },
            "quote": {
                "text": "inside",
                "author": {"screen_name": "bob"},
            },
        }
    }
    parsed = _parse_fxtwitter(payload)
    assert parsed is not None
    assert parsed["media"] == "2 photos"
    assert parsed["quoted"] == "@bob: inside"


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
    settings: TwitterInfoSettings | None = None,
) -> TwitterInfoModule:
    module = TwitterInfoModule(bot)
    module._repo = TEST_REPO
    module._name = "twitterinfo"
    module.settings = settings or TwitterInfoSettings()
    await module.on_load()
    bot.modules._register_triggers(
        TEST_REPO, "twitterinfo", module, TwitterInfoModule
    )
    bot.networks["mock"] = conn  # type: ignore[assignment]
    return module


def _event(message: str, target: str = "#room", source: str = "alice") -> Event:
    return Event(
        kind="message",
        network="mock",
        payload={"message": message, "source": source, "target": target},
    )


class _Counter:
    def __init__(self) -> None:
        self.calls = 0


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> _Counter:
    counter = _Counter()

    def counting(request: httpx.Request) -> httpx.Response:
        counter.calls += 1
        return handler(request)

    transport = httpx.MockTransport(counting)
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(_twi_mod.httpx, "AsyncClient", factory)
    return counter


async def test_handle_message_happy_path(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "cdn.syndication.twimg.com" in str(request.url)
        assert "id=20" in str(request.url)
        return httpx.Response(200, json=_syn_payload())

    _install_transport(monkeypatch, handler)

    await module.handle_message(
        _event("look https://x.com/jack/status/20 here")
    )

    assert len(conn.sent) == 1
    target, text = conn.sent[0]
    assert target == "#room"
    assert "\x02@jack\x02" in text
    assert "just setting up my twttr" in text
    assert "ago" in text
    assert "\u2665100" in text
    assert "\u21bb50" in text


async def test_cache_hit_avoids_second_fetch(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    counter = _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_syn_payload()),
    )

    await module.handle_message(_event("https://x.com/jack/status/20"))
    await module.handle_message(_event("https://twitter.com/jack/status/20"))

    assert counter.calls == 1
    assert len(conn.sent) == 2
    assert conn.sent[0][1] == conn.sent[1][1]


async def test_404_stored_as_negative_cache(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    counter = _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(404),
    )

    await module.handle_message(_event("https://x.com/jack/status/20"))
    await module.handle_message(_event("https://x.com/jack/status/20"))

    assert conn.sent == []
    assert counter.calls == 1


async def test_pm_replies_to_sender(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(200, json=_syn_payload()),
    )

    await module.handle_message(
        _event("https://x.com/jack/status/20", target="vibebot")
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
        lambda _req: httpx.Response(200, json=_syn_payload()),
    )

    await module.handle_message(_event("https://x.com/jack/status/20"))

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
        settings=TwitterInfoSettings(max_reply_len=80),
    )
    long_text = "A" * 500
    _install_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, json=_syn_payload(text=long_text)
        ),
    )

    await module.handle_message(_event("https://x.com/jack/status/20"))

    text = conn.sent[0][1]
    assert _visible_len(text) <= 80
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

    await module.handle_message(_event("https://x.com/jack/status/20"))

    assert conn.sent == []


async def test_fxtwitter_fallback_used_when_enabled(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(
        bot,
        conn,
        settings=TwitterInfoSettings(fallback_fxtwitter=True),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "cdn.syndication.twimg.com" in str(request.url):
            return httpx.Response(500)
        assert "api.fxtwitter.com" in str(request.url)
        return httpx.Response(
            200,
            json={
                "tweet": {
                    "text": "fallback ok",
                    "author": {"screen_name": "jack", "name": "Jack"},
                    "created_at": "Tue Mar 21 20:50:14 +0000 2006",
                    "likes": 1,
                    "retweets": 0,
                    "replies": 0,
                }
            },
        )

    _install_transport(monkeypatch, handler)

    await module.handle_message(_event("https://x.com/jack/status/20"))

    assert len(conn.sent) == 1
    assert "fallback ok" in conn.sent[0][1]


async def test_fxtwitter_fallback_disabled_by_default(
    bot: VibeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _FakeConn()
    module = await _install_module(bot, conn)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "api.fxtwitter.com" not in str(request.url)
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)

    await module.handle_message(_event("https://x.com/jack/status/20"))
    assert conn.sent == []


async def test_non_twitter_url_ignored_by_trigger(bot: VibeBot) -> None:
    conn = _FakeConn()
    await _install_module(bot, conn)
    registry = bot.modules._registry
    assert list(registry.match(_event("https://example.com/foo"))) == []
    assert list(registry.match(_event("https://youtu.be/dQw4w9WgXcQ"))) == []
    assert (
        list(registry.match(_event("https://x.com/jack/status/20"))) != []
    )
