"""Per-module schedules endpoint: combines module-declared tasks + user schedules."""

from __future__ import annotations

import types
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.modules.base import Module, ScheduledTask
from vibebot.modules.loader import LoadedModule


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


class _FakeModule(Module):
    name = "fake"
    description = "fake test module"

    async def _noop(self) -> None:
        return None

    def scheduled_tasks(self) -> list[ScheduledTask]:
        return [
            ScheduledTask(
                name="heartbeat",
                func=self._noop,
                trigger={"type": "interval", "seconds": 60},
            ),
            ScheduledTask(
                name="daily",
                func=self._noop,
                trigger={"type": "cron", "hour": 0},
            ),
        ]


def _inject_fake_module(bot: VibeBot, repo: str, name: str) -> LoadedModule:
    inst = _FakeModule(bot)
    inst._repo = repo
    inst._name = name
    job_ids: list[str] = []
    for task in inst.scheduled_tasks():
        jid = f"{repo}/{name}/{task.name}"
        # memory jobstore avoids the SQLAlchemyJobStore pickle requirement;
        # the endpoint reads jobs via `get_job` regardless of store.
        bot.scheduler.add_job(task.func, trigger=task.trigger, job_id=jid, jobstore="memory")
        job_ids.append(jid)
    loaded = LoadedModule(
        repo=repo,
        name=name,
        instance=inst,
        python_module=types.ModuleType(f"fake.{repo}.{name}"),
        job_ids=job_ids,
    )
    bot.modules._loaded[(repo, name)] = loaded
    return loaded


HEADERS = {"Authorization": "Bearer t0ken"}


@pytest.fixture()
async def client(tmp_path: Path):
    bot = _make_bot(tmp_path)
    await bot.db.create_all()
    await bot.scheduler.start()
    await bot.schedules.rehydrate()
    app = build_app(bot)
    with TestClient(app) as c:
        c.bot = bot  # type: ignore[attr-defined]
        yield c
    await bot.scheduler.stop()
    await bot.db.close()


def test_404_when_module_not_loaded(client: TestClient) -> None:
    r = client.get("/api/modules/nope/missing/schedules", headers=HEADERS)
    assert r.status_code == 404


async def test_happy_path_lists_tasks_and_user_schedules(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="fake",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
        title="ping-alice",
    )
    r = client.get("/api/modules/testrepo/fake/schedules", headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["module_tasks"]) == 2
    names = {t["task_name"] for t in data["module_tasks"]}
    assert names == {"heartbeat", "daily"}
    for t in data["module_tasks"]:
        assert t["job_id"] == f"testrepo/fake/{t['task_name']}"
        assert t["trigger"]
        # next_run_at is ISO-parseable for live jobs
        datetime.fromisoformat(t["next_run_at"])
    assert len(data["user_schedules"]) == 1
    assert data["user_schedules"][0]["title"] == "ping-alice"


async def test_cross_module_filter(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="fake",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
        title="mine",
    )
    await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="other",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
        title="not-mine",
    )
    r = client.get("/api/modules/testrepo/fake/schedules", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    titles = {s["title"] for s in data["user_schedules"]}
    assert titles == {"mine"}


def test_module_task_count_parity(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    loaded = _inject_fake_module(bot, "testrepo", "fake")
    r = client.get("/api/modules/testrepo/fake/schedules", headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert len(data["module_tasks"]) == len(loaded.job_ids)


def test_requires_token(client: TestClient) -> None:
    r = client.get("/api/modules/testrepo/fake/schedules")
    assert r.status_code == 401


async def test_modules_list_exposes_schedule_counts(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="fake",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
        title="ping-alice",
    )
    r = client.get("/api/modules", headers=HEADERS)
    assert r.status_code == 200, r.text
    rows = {(row["repo"], row["name"]): row for row in r.json()}
    row = rows[("testrepo", "fake")]
    assert row["scheduled_task_count"] == 2
    assert row["user_schedule_count"] == 1
    assert row["implements_schedules"] is True
