"""Module lifecycle endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vibebot.api.auth import require_token
from vibebot.api.routes.schedules import module_task_entries

router = APIRouter(prefix="/api/modules", tags=["modules"], dependencies=[Depends(require_token)])


class ModuleRef(BaseModel):
    repo: str
    name: str


@router.get("")
async def list_modules(request: Request) -> list[dict]:
    bot = request.app.state.bot
    loaded = list(bot.modules.list_loaded())
    # Bucket active user schedules per (repo, module) in one query.
    active = await bot.schedules.list()
    user_counts: dict[tuple[str, str], int] = {}
    for dto in active:
        if dto.status not in ("scheduled", "paused"):
            continue
        key = (dto.repo_name, dto.module_name)
        user_counts[key] = user_counts.get(key, 0) + 1
    handler_keys = set(bot.schedules._handlers.keys())  # noqa: SLF001
    out: list[dict] = []
    for m in loaded:
        scheduled_task_count = len(m.job_ids)
        user_schedule_count = user_counts.get((m.repo, m.name), 0)
        handler_count = sum(1 for k in handler_keys if k[0] == m.repo and k[1] == m.name)
        triggers = [
            {
                "kind": t.kind,
                "match": t.match.describe(),
                "source": t.source,
                "excludes": [p.pattern for p in t.excludes],
            }
            for t in bot.modules.triggers_for(m.repo, m.name)
        ]
        out.append(
            {
                "repo": m.repo,
                "name": m.name,
                "enabled": m.enabled,
                "state": "enabled" if m.enabled else "disabled",
                "description": m.instance.description,
                "scheduled_task_count": scheduled_task_count,
                "user_schedule_count": user_schedule_count,
                "handler_count": handler_count,
                "implements_schedules": (scheduled_task_count + handler_count) > 0,
                "triggers": triggers,
                "error_message": None,
            }
        )
    for avail in await bot.modules.list_available():
        err = avail.get("error_message")
        out.append(
            {
                "repo": avail["repo"],
                "name": avail["name"],
                "enabled": False,
                "state": "error" if err else "unloaded",
                "description": avail.get("description", ""),
                "scheduled_task_count": 0,
                "user_schedule_count": 0,
                "handler_count": 0,
                "implements_schedules": False,
                "triggers": [],
                "error_message": err,
            }
        )
    return out


@router.post("/load")
async def load(ref: ModuleRef, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.modules.load(ref.repo, ref.name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.post("/unload")
async def unload(ref: ModuleRef, request: Request) -> dict:
    bot = request.app.state.bot
    await bot.modules.unload(ref.repo, ref.name)
    return {"status": "ok"}


@router.post("/reload")
async def reload(ref: ModuleRef, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.modules.reload(ref.repo, ref.name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.post("/enable")
async def enable(ref: ModuleRef, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.modules.enable(ref.repo, ref.name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.post("/disable")
async def disable(ref: ModuleRef, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.modules.disable(ref.repo, ref.name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.get("/{repo}/{name}/schedules")
async def module_schedules(repo: str, name: str, request: Request) -> dict:
    """List scheduled work for a loaded module.

    Returns two lists:
      - ``module_tasks``: static tasks the module declares via
        ``Module.scheduled_tasks()``; read-only surface (lifecycle is tied to
        module load/unload — to stop them, disable the module).
      - ``user_schedules``: user-created schedules targeting this module's
        handlers; fully controllable via the ``/api/schedules`` endpoints.
    """
    bot = request.app.state.bot
    loaded = next(
        (m for m in bot.modules.list_loaded() if m.repo == repo and m.name == name),
        None,
    )
    if loaded is None:
        raise HTTPException(404, f"module {repo}/{name} not loaded")
    module_tasks = [
        {k: v for k, v in entry.items() if k not in ("repo_name", "module_name")}
        for entry in module_task_entries(bot)
        if entry["repo_name"] == repo and entry["module_name"] == name
    ]
    user_items = await bot.schedules.list(repo=repo, module=name)
    return {
        "module_tasks": module_tasks,
        "user_schedules": [dto.to_dict() for dto in user_items],
    }
