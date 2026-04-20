"""Per-module settings endpoints.

Modules declare a Pydantic ``Settings`` class on the Module subclass; this
router exposes the schema, current values (with secrets masked), and a PUT
for partial updates. Writes persist only — the running module keeps its old
settings until an operator explicitly reloads via /api/modules/reload.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from vibebot.api.auth import require_token
from vibebot.modules.settings import (
    ModuleSettingsError,
    dump_for_storage,
    mask_secrets,
    merge_and_validate,
    schema_with_secret_flags,
)

router = APIRouter(
    prefix="/api/modules", tags=["module-settings"], dependencies=[Depends(require_token)]
)


def _get_settings_cls(request: Request, repo: str, name: str) -> type | None:
    bot = request.app.state.bot
    loaded = next(
        (m for m in bot.modules.list_loaded() if m.repo == repo and m.name == name), None
    )
    if loaded is None:
        raise HTTPException(404, f"Module {repo}/{name} not loaded")
    return type(loaded.instance).Settings


@router.get("/{repo}/{name}/settings/schema")
async def get_schema(repo: str, name: str, request: Request) -> dict[str, Any]:
    cls = _get_settings_cls(request, repo, name)
    if cls is None:
        return {"type": "object", "properties": {}, "declared": False}
    schema = schema_with_secret_flags(cls)
    schema["declared"] = True
    return schema


@router.get("/{repo}/{name}/settings")
async def get_settings(repo: str, name: str, request: Request) -> dict[str, Any]:
    bot = request.app.state.bot
    cls = _get_settings_cls(request, repo, name)
    stored = await bot.modules.get_stored_settings(repo, name)
    if cls is None:
        # Module has no typed Settings — return the raw dict with no masking
        # context to lean on; this is a legacy shape we don't promise to mask.
        return {"declared": False, "values": stored}
    try:
        model = cls(**stored)
    except ValidationError as exc:
        raise HTTPException(422, f"Stored settings fail validation: {exc.errors()}") from exc
    return {"declared": True, "values": mask_secrets(model)}


@router.put("/{repo}/{name}/settings")
async def update_settings(
    repo: str, name: str, patch: dict[str, Any], request: Request
) -> dict[str, Any]:
    bot = request.app.state.bot
    cls = _get_settings_cls(request, repo, name)
    if cls is None:
        raise HTTPException(400, f"Module {repo}/{name} does not declare typed Settings")
    stored = await bot.modules.get_stored_settings(repo, name)
    try:
        model = merge_and_validate(cls, stored, patch)
    except (ValidationError, ModuleSettingsError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await bot.modules.save_settings(repo, name, dump_for_storage(model))
    return {
        "declared": True,
        "values": mask_secrets(model),
        "reload_required": True,
    }
