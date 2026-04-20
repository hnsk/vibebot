"""Schedule CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from vibebot.api.auth import require_token
from vibebot.scheduler.service import ScheduleError

router = APIRouter(prefix="/api/schedules", tags=["schedules"], dependencies=[Depends(require_token)])


def module_task_entries(bot: Any) -> list[dict[str, Any]]:
    """Enumerate static scheduled tasks across every loaded module.

    Each entry carries enough origin info (repo, module, task_name) for the
    UI to attribute the job, plus the APScheduler job_id used by the
    module-task lifecycle endpoints below.
    """
    out: list[dict[str, Any]] = []
    for loaded in bot.modules.list_loaded():
        for jid in loaded.job_ids:
            job = bot.scheduler.get_job(jid)
            if job is None:
                continue
            next_run = getattr(job, "next_run_time", None)
            out.append(
                {
                    "repo_name": loaded.repo,
                    "module_name": loaded.name,
                    "task_name": jid.rsplit("/", 1)[-1],
                    "job_id": jid,
                    "trigger": str(job.trigger),
                    "next_run_at": next_run.isoformat() if next_run else None,
                    "paused": next_run is None,
                }
            )
    return out


def _loaded_module_job_ids(bot: Any) -> set[str]:
    ids: set[str] = set()
    for loaded in bot.modules.list_loaded():
        ids.update(loaded.job_ids)
    return ids


class CreateSchedule(BaseModel):
    owner_nick: str
    owner_mask: str
    owner_network: str | None = None
    repo: str
    module: str
    handler: str
    trigger: dict[str, Any]
    payload: dict[str, Any] = Field(default_factory=dict)
    title: str | None = None
    misfire_grace_seconds: int = 60


class UpdateSchedule(BaseModel):
    trigger: dict[str, Any] | None = None
    payload: dict[str, Any] | None = None
    title: str | None = None


@router.get("")
async def list_schedules(
    request: Request,
    owner_mask: str | None = Query(default=None),
    repo: str | None = Query(default=None),
    module: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    bot = request.app.state.bot
    items = await bot.schedules.list(owner_mask=owner_mask, repo=repo, module=module, status=status)
    return [dto.to_dict() for dto in items]


@router.get("/overview")
async def overview(request: Request) -> dict[str, list[dict[str, Any]]]:
    """Aggregate view for the Schedules page.

    Returns every module-declared static task (``module_tasks``) and every
    user-created schedule (``user_schedules``). The UI partitions these into
    "periodic jobs" vs. "upcoming tasks" by trigger type.
    """
    bot = request.app.state.bot
    module_tasks = module_task_entries(bot)
    user_items = await bot.schedules.list()
    return {
        "module_tasks": module_tasks,
        "user_schedules": [dto.to_dict() for dto in user_items],
    }


class ModuleTaskRef(BaseModel):
    job_id: str


def _resolve_module_task(bot: Any, job_id: str) -> None:
    if job_id not in _loaded_module_job_ids(bot):
        raise HTTPException(404, f"no such module task: {job_id}")


@router.post("/module-task/pause")
async def pause_module_task(body: ModuleTaskRef, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    _resolve_module_task(bot, body.job_id)
    bot.scheduler.pause_job(body.job_id)
    return _module_task_snapshot(bot, body.job_id)


@router.post("/module-task/resume")
async def resume_module_task(body: ModuleTaskRef, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    _resolve_module_task(bot, body.job_id)
    bot.scheduler.resume_job(body.job_id)
    return _module_task_snapshot(bot, body.job_id)


@router.post("/module-task/run-now")
async def run_module_task_now(body: ModuleTaskRef, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    _resolve_module_task(bot, body.job_id)
    bot.scheduler.run_job_now(body.job_id)
    return _module_task_snapshot(bot, body.job_id)


def _module_task_snapshot(bot: Any, job_id: str) -> dict[str, Any]:
    job = bot.scheduler.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"job vanished: {job_id}")
    next_run = getattr(job, "next_run_time", None)
    return {
        "job_id": job_id,
        "trigger": str(job.trigger),
        "next_run_at": next_run.isoformat() if next_run else None,
        "paused": next_run is None,
    }


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.get(schedule_id)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return dto.to_dict()


@router.post("")
async def create_schedule(body: CreateSchedule, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.create(
            owner_nick=body.owner_nick,
            owner_mask=body.owner_mask,
            owner_network=body.owner_network,
            repo=body.repo,
            module=body.module,
            handler=body.handler,
            trigger=body.trigger,
            payload=body.payload,
            title=body.title,
            misfire_grace_seconds=body.misfire_grace_seconds,
        )
    except (ScheduleError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return dto.to_dict()


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: UpdateSchedule, request: Request
) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.update(
            schedule_id,
            trigger=body.trigger,
            payload=body.payload,
            title=body.title,
        )
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return dto.to_dict()


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    request: Request,
    hard: bool = Query(default=False),
) -> dict[str, str]:
    bot = request.app.state.bot
    try:
        await bot.schedules.cancel(schedule_id, hard=hard)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"status": "ok"}


@router.post("/{schedule_id}/pause")
async def pause_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.pause(schedule_id)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return dto.to_dict()


@router.post("/{schedule_id}/resume")
async def resume_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.resume(schedule_id)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return dto.to_dict()


@router.post("/{schedule_id}/run-now")
async def run_now(schedule_id: str, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    try:
        dto = await bot.schedules.run_now(schedule_id)
    except ScheduleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return dto.to_dict()
