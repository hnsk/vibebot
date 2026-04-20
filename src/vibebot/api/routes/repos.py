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
    out: list[dict] = []
    for r in repos:
        deps = await bot.deps.status(r.name)
        out.append({
            "name": r.name,
            "url": r.url,
            "branch": r.branch,
            "subdir": r.subdir,
            "enabled": r.enabled,
            "last_pulled_at": r.last_pulled_at.isoformat() if r.last_pulled_at else None,
            "deps": deps,
        })
    return out


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


@router.get("/{name}/requirements")
async def get_requirements(name: str, request: Request) -> dict:
    bot = request.app.state.bot
    repo = await bot.repos.get_repo(name)
    if repo is None:
        raise HTTPException(404, f"Unknown repo {name!r}")
    status = await bot.deps.status(name)
    content: str | None = None
    if status.get("path"):
        try:
            from pathlib import Path as _Path
            content = _Path(status["path"]).read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(500, f"cannot read requirements: {exc}") from exc
    return {**status, "content": content}


@router.post("/{name}/install-requirements")
async def install_requirements(name: str, request: Request, force: bool = False) -> dict:
    bot = request.app.state.bot
    repo = await bot.repos.get_repo(name)
    if repo is None:
        raise HTTPException(404, f"Unknown repo {name!r}")
    result = await bot.deps.ensure_installed(name, force=force)
    return {
        "ok": result.ok,
        "skipped": result.skipped,
        "reason": result.reason,
        "requirements_path": result.requirements_path,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration": result.duration,
        "returncode": result.returncode,
    }
