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
    enabled: bool = True


@router.get("")
async def list_repos(request: Request) -> list[dict]:
    bot = request.app.state.bot
    repos = await bot.repos.list_repos()
    return [
        {
            "name": r.name,
            "url": r.url,
            "branch": r.branch,
            "enabled": r.enabled,
            "last_pulled_at": r.last_pulled_at.isoformat() if r.last_pulled_at else None,
        }
        for r in repos
    ]


@router.post("")
async def add_repo(body: RepoBody, request: Request) -> dict:
    bot = request.app.state.bot
    try:
        await bot.repos.add_repo(body.name, body.url, body.branch, body.enabled)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
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
