"""ScheduleService + HTTP API tests — no live IRC, in-memory scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.acl import Identity
from vibebot.core.bot import VibeBot
from vibebot.scheduler.service import PAST_RETENTION_MAX_ROWS, ScheduleError
from vibebot.storage.models import Schedule


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


async def _insert_schedule_row(
    bot: VibeBot,
    *,
    sid: str,
    status: str,
    updated_at: datetime,
    trigger: str = '{"type":"date","run_date":"2000-01-01T00:00:00+00:00"}',
) -> None:
    async with bot.db.session() as s:
        s.add(
            Schedule(
                id=sid,
                owner_nick="seed",
                owner_mask="seed!*@*",
                owner_network=None,
                repo_name="demo",
                module_name="m",
                handler_name="fire",
                payload_json="{}",
                trigger_json=trigger,
                status=status,
                title=None,
                misfire_grace_seconds=60,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
        await s.commit()


async def _count_schedules(bot: VibeBot, status: str | None = None) -> int:
    async with bot.db.session() as s:
        stmt = select(Schedule)
        if status is not None:
            stmt = stmt.where(Schedule.status == status)
        return len(list((await s.execute(stmt)).scalars()))


async def test_prune_drops_rows_older_than_24h(running_bot: VibeBot) -> None:
    now = datetime.now(UTC)
    await _insert_schedule_row(
        running_bot, sid="stale", status="completed", updated_at=now - timedelta(hours=25)
    )
    await _insert_schedule_row(
        running_bot, sid="fresh", status="completed", updated_at=now - timedelta(hours=1)
    )
    # Active rows must survive even if old (they never become "past").
    await _insert_schedule_row(
        running_bot,
        sid="active-old",
        status="scheduled",
        updated_at=now - timedelta(hours=48),
        trigger='{"type":"interval","seconds":60}',
    )

    await running_bot.schedules._prune_past_schedules()

    async with running_bot.db.session() as s:
        remaining = {
            row.id
            for row in (await s.execute(select(Schedule))).scalars()
        }
    assert "stale" not in remaining
    assert "fresh" in remaining
    assert "active-old" in remaining


async def test_prune_caps_past_rows_at_max(running_bot: VibeBot) -> None:
    now = datetime.now(UTC)
    extra = 5
    total = PAST_RETENTION_MAX_ROWS + extra
    for i in range(total):
        await _insert_schedule_row(
            running_bot,
            sid=f"past-{i:03d}",
            status="completed",
            # All within 24h; only the row-cap rule should prune them.
            # `i=0` is the oldest, `i=total-1` the newest.
            updated_at=now - timedelta(minutes=total - i),
        )

    await running_bot.schedules._prune_past_schedules()

    async with running_bot.db.session() as s:
        remaining = sorted(
            row.id
            for row in (await s.execute(select(Schedule))).scalars()
        )
    assert len(remaining) == PAST_RETENTION_MAX_ROWS
    # The `extra` oldest rows should be gone.
    for i in range(extra):
        assert f"past-{i:03d}" not in remaining
    assert f"past-{extra:03d}" in remaining


async def test_dispatch_triggers_prune(running_bot: VibeBot) -> None:
    now = datetime.now(UTC)
    await _insert_schedule_row(
        running_bot, sid="stale", status="completed", updated_at=now - timedelta(hours=25)
    )

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
    # Wait for the post-dispatch prune to settle.
    ids: set[str] = set()
    for _ in range(100):
        await asyncio.sleep(0.05)
        async with running_bot.db.session() as s:
            ids = {
                row.id
                for row in (await s.execute(select(Schedule))).scalars()
            }
        if "stale" not in ids:
            break
    assert "stale" not in ids
    assert dto.id in ids


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
