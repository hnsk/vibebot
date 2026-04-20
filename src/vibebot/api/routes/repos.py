"""Module repository CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/repos", tags=["repos"], dependencies=[Depends(require_token)])


class RepoBody(BaseModel):
    name: str
    url: str
    branch: str = "main"
    subdir: str | None = None
    enabled: bool = True


class RepoPatch(BaseModel):
    url: str | None = None
    branch: str | None = None
    subdir: str | None = None
    enabled: bool | None = None
    clear_subdir: bool = False


@router.get("")
async def list_repos(request: Request) -> list[dict]:
    bot = request.app.state.bot
    repos = await bot.repos.list_repos()
    return [
        {
            "name": r.name,
            "url": r.url,
            "branch": r.branch,
            "subdir": r.subdir,
            "enabled": r.enabled,
            "last_pulled_at": r.last_pulled_at.isoformat() if r.last_pulled_at else None,
        }
        for r in repos
    ]


@router.post("")
async def add_repo(body: RepoBody, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.repos.add_repo(
            body.name, body.url, body.branch, body.enabled, subdir=body.subdir
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok"}


@router.patch("/{name}")
async def patch_repo(name: str, body: RepoPatch, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        repo = await bot.repos.update_repo(
            name,
            url=body.url,
            branch=body.branch,
            subdir=body.subdir,
            enabled=body.enabled,
            clear_subdir=body.clear_subdir,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if repo is None:
        raise HTTPException(404, f"Unknown repo {name!r}")
    return {"status": "ok"}


@router.delete("/{name}")
async def remove_repo(name: str, request: Request) -> dict:
    bot = request.app.state.bot
    removed = await bot.repos.remove_repo(name)
    if not removed:
        raise HTTPException(404, f"Unknown repo {name!r}")
    return {"status": "ok"}


@router.post("/{name}/pull")
async def pull_repo(name: str, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        path = await bot.repos.clone_or_pull(name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", "path": str(path)}
