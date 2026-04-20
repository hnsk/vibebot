"""ScheduleService + HTTP API tests — no live IRC, in-memory scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.acl import Identity
from vibebot.core.bot import VibeBot
from vibebot.scheduler.service import ScheduleError


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
async def running_bot(tmp_path: Path):
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    await bot.scheduler.start()
    await bot.schedules.rehydrate()
    try:
        yield bot
    finally:
        await bot.scheduler.stop()
        await bot.db.close()


async def test_create_list_get_roundtrip(running_bot: VibeBot) -> None:
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 60},
        payload={"x": 1},
        title="demo",
    )
    assert dto.id
    assert dto.status == "scheduled"
    listed = await running_bot.schedules.list(owner_mask="alice!*@*")
    assert len(listed) == 1
    assert listed[0].title == "demo"
    got = await running_bot.schedules.get(dto.id)
    assert got.payload == {"x": 1}


async def test_handler_fires_with_payload(running_bot: VibeBot) -> None:
    received: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def handler(payload: dict[str, Any]) -> None:
        received.append(payload)
        done.set()

    running_bot.schedules.register_handler("demo", "m", "fire", handler)
    # A `date` trigger with a past run_date fires immediately.
    await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "date", "run_date": datetime.now(UTC) + timedelta(milliseconds=50)},
        payload={"hello": "world"},
    )
    await asyncio.wait_for(done.wait(), timeout=5.0)
    assert received == [{"hello": "world"}]


async def test_date_trigger_marks_completed(running_bot: VibeBot) -> None:
    done = asyncio.Event()

    async def handler(_payload: dict[str, Any]) -> None:
        done.set()

    running_bot.schedules.register_handler("demo", "m", "fire", handler)
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "date", "run_date": datetime.now(UTC) + timedelta(milliseconds=50)},
    )
    await asyncio.wait_for(done.wait(), timeout=5.0)
    # Allow the post-dispatch DB update to settle.
    for _ in range(20):
        got = await running_bot.schedules.get(dto.id)
        if got.status == "completed":
            break
        await asyncio.sleep(0.05)
    assert got.status == "completed"


async def test_cancel_owner_vs_stranger(running_bot: VibeBot) -> None:
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!user@host.example",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 60},
    )
    stranger = Identity.parse("bob!user@evil.example")
    with pytest.raises(ScheduleError):
        await running_bot.schedules.cancel(dto.id, requester=stranger)
    owner = Identity.parse("alice!user@host.example")
    await running_bot.schedules.cancel(dto.id, requester=owner)
    got = await running_bot.schedules.get(dto.id)
    assert got.status == "cancelled"


async def test_admin_can_cancel_anyones(running_bot: VibeBot) -> None:
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 60},
    )
    await running_bot.acl.add_rule("op!*@*", "admin")
    admin = Identity.parse("op!user@host")
    await running_bot.schedules.cancel(dto.id, requester=admin)
    got = await running_bot.schedules.get(dto.id)
    assert got.status == "cancelled"


async def test_pause_resume(running_bot: VibeBot) -> None:
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 60},
    )
    paused = await running_bot.schedules.pause(dto.id)
    assert paused.status == "paused"
    resumed = await running_bot.schedules.resume(dto.id)
    assert resumed.status == "scheduled"


async def test_update_reschedules(running_bot: VibeBot) -> None:
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 600},
    )
    updated = await running_bot.schedules.update(
        dto.id, trigger={"type": "interval", "seconds": 10}
    )
    assert updated.trigger["seconds"] == 10


async def test_invalid_trigger_rejected(running_bot: VibeBot) -> None:
    with pytest.raises(ValueError):
        await running_bot.schedules.create(
            owner_nick="alice",
            owner_mask="alice!*@*",
            repo="demo",
            module="m",
            handler="fire",
            trigger={"type": "bogus"},
        )


async def test_missing_handler_is_skipped(running_bot: VibeBot) -> None:
    # No register_handler call — dispatcher should log+skip, not crash.
    dto = await running_bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="ghost",
        trigger={"type": "date", "run_date": datetime.now(UTC) + timedelta(milliseconds=50)},
    )
    await asyncio.sleep(0.5)
    got = await running_bot.schedules.get(dto.id)
    # Handler missing → dispatcher returns without setting `completed`; the
    # `date` job is already gone from APScheduler. Status remains `scheduled`.
    assert got.status in {"scheduled", "missed"}


async def test_rehydrate_restores_jobs(tmp_path: Path) -> None:
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    await bot.scheduler.start()
    dto = await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="demo",
        module="m",
        handler="fire",
        trigger={"type": "interval", "seconds": 3600},
    )
    await bot.scheduler.stop()
    await bot.db.close()

    # Fresh bot on the same DB.
    bot2 = _make_bot(tmp_path)
    await bot2.db.create_all()
    await bot2.scheduler.start()
    await bot2.schedules.rehydrate()
    got = await bot2.schedules.get(dto.id)
    assert got.status == "scheduled"
    assert bot2.scheduler.get_job(f"user:{dto.id}") is not None
    await bot2.scheduler.stop()
    await bot2.db.close()


# -------- HTTP API --------


@pytest.fixture()
def api_bot(tmp_path: Path) -> VibeBot:
    return _make_bot(tmp_path)


@pytest.fixture()
async def client(api_bot: VibeBot):
    await api_bot.db.create_all()
    await api_bot.scheduler.start()
    await api_bot.schedules.rehydrate()
    app = build_app(api_bot)
    with TestClient(app) as c:
        yield c
    await api_bot.scheduler.stop()
    await api_bot.db.close()


HEADERS = {"Authorization": "Bearer t0ken"}


async def test_api_create_list_delete(client: TestClient) -> None:
    body = {
        "owner_nick": "alice",
        "owner_mask": "alice!*@*",
        "repo": "demo",
        "module": "m",
        "handler": "fire",
        "trigger": {"type": "interval", "seconds": 30},
        "payload": {"k": 1},
        "title": "api-test",
    }
    r = client.post("/api/schedules", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    created = r.json()
    sid = created["id"]
    listed = client.get("/api/schedules", headers=HEADERS).json()
    assert any(item["id"] == sid for item in listed)
    got = client.get(f"/api/schedules/{sid}", headers=HEADERS).json()
    assert got["title"] == "api-test"
    dele = client.delete(f"/api/schedules/{sid}", headers=HEADERS)
    assert dele.status_code == 200
    after = client.get(f"/api/schedules/{sid}", headers=HEADERS).json()
    assert after["status"] == "cancelled"


async def test_api_patch(client: TestClient) -> None:
    body = {
        "owner_nick": "alice",
        "owner_mask": "alice!*@*",
        "repo": "demo",
        "module": "m",
        "handler": "fire",
        "trigger": {"type": "interval", "seconds": 600},
    }
    sid = client.post("/api/schedules", json=body, headers=HEADERS).json()["id"]
    r = client.patch(
        f"/api/schedules/{sid}",
        json={"trigger": {"type": "interval", "seconds": 15}, "title": "renamed"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["trigger"]["seconds"] == 15
    assert r.json()["title"] == "renamed"


async def test_api_pause_resume(client: TestClient) -> None:
    body = {
        "owner_nick": "alice",
        "owner_mask": "alice!*@*",
        "repo": "demo",
        "module": "m",
        "handler": "fire",
        "trigger": {"type": "interval", "seconds": 600},
    }
    sid = client.post("/api/schedules", json=body, headers=HEADERS).json()["id"]
    assert client.post(f"/api/schedules/{sid}/pause", headers=HEADERS).json()["status"] == "paused"
    assert client.post(f"/api/schedules/{sid}/resume", headers=HEADERS).json()["status"] == "scheduled"


async def test_api_invalid_trigger_400(client: TestClient) -> None:
    body = {
        "owner_nick": "alice",
        "owner_mask": "alice!*@*",
        "repo": "demo",
        "module": "m",
        "handler": "fire",
        "trigger": {"type": "bogus"},
    }
    r = client.post("/api/schedules", json=body, headers=HEADERS)
    assert r.status_code == 400


async def test_api_not_found_404(client: TestClient) -> None:
    r = client.get("/api/schedules/does-not-exist", headers=HEADERS)
    assert r.status_code == 404


async def test_api_requires_token(client: TestClient) -> None:
    r = client.get("/api/schedules")
    assert r.status_code == 401
