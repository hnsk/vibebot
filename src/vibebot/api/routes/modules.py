"""Module lifecycle endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/modules", tags=["modules"], dependencies=[Depends(require_token)])


class ModuleRef(BaseModel):
    repo: str
    name: str


@router.get("")
async def list_modules(request: Request) -> list[dict]:
    bot = request.app.state.bot
    return [
        {"repo": m.repo, "name": m.name, "enabled": m.enabled, "description": m.instance.description}
        for m in bot.modules.list_loaded()
    ]


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
