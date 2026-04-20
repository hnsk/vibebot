"""Schedule CRUD endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from vibebot.api.auth import require_token
from vibebot.scheduler.service import ScheduleError

router = APIRouter(prefix="/api/schedules", tags=["schedules"], dependencies=[Depends(require_token)])


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
