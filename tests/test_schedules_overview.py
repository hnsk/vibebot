"""Top-level `/api/schedules/overview` + module-task lifecycle endpoints."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.modules.base import Module, ScheduledTask
from vibebot.modules.loader import LoadedModule

HEADERS = {"Authorization": "Bearer t0ken"}


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
        ]


def _inject_fake_module(bot: VibeBot, repo: str, name: str) -> LoadedModule:
    inst = _FakeModule(bot)
    inst._repo = repo
    inst._name = name
    job_ids: list[str] = []
    for task in inst.scheduled_tasks():
        jid = f"{repo}/{name}/{task.name}"
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


async def test_overview_lists_module_tasks_and_user_schedules(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="fake",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
        title="recurring",
    )
    r = client.get("/api/schedules/overview", headers=HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "module_tasks" in data and "user_schedules" in data
    assert len(data["module_tasks"]) == 1
    entry = data["module_tasks"][0]
    assert entry["repo_name"] == "testrepo"
    assert entry["module_name"] == "fake"
    assert entry["task_name"] == "heartbeat"
    assert entry["job_id"] == "testrepo/fake/heartbeat"
    assert entry["trigger"]
    assert entry["next_run_at"]
    assert entry["paused"] is False
    assert len(data["user_schedules"]) == 1
    user = data["user_schedules"][0]
    assert user["title"] == "recurring"
    # The UI relies on these keys for the origin chip + edit dialog sub.
    assert user["repo"] == "testrepo"
    assert user["module"] == "fake"
    assert user["handler"] == "ding"


async def test_overview_requires_token(client: TestClient) -> None:
    r = client.get("/api/schedules/overview")
    assert r.status_code == 401


async def test_module_task_pause_resume_and_run_now(client: TestClient) -> None:
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    job_id = "testrepo/fake/heartbeat"
    r = client.post("/api/schedules/module-task/pause", headers=HEADERS, json={"job_id": job_id})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is True
    r = client.post("/api/schedules/module-task/resume", headers=HEADERS, json={"job_id": job_id})
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is False
    r = client.post("/api/schedules/module-task/run-now", headers=HEADERS, json={"job_id": job_id})
    assert r.status_code == 200, r.text


def test_module_task_unknown_job_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/schedules/module-task/pause",
        headers=HEADERS,
        json={"job_id": "does/not/exist"},
    )
    assert r.status_code == 404


async def test_module_task_refuses_user_schedule_job(client: TestClient) -> None:
    """The endpoint must only act on loaded-module job_ids.

    User schedules live under APScheduler job ids of the form ``sched:<uuid>``
    in the memory jobstore, and must not be pokable via this surface.
    """
    bot: VibeBot = client.bot  # type: ignore[attr-defined]
    _inject_fake_module(bot, "testrepo", "fake")
    dto = await bot.schedules.create(
        owner_nick="alice",
        owner_mask="alice!*@*",
        repo="testrepo",
        module="fake",
        handler="ding",
        trigger={"type": "interval", "seconds": 300},
    )
    user_job_id = f"sched:{dto.id}"
    r = client.post("/api/schedules/module-task/pause", headers=HEADERS, json={"job_id": user_job_id})
    assert r.status_code == 404
