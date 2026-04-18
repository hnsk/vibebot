"""ACL rule CRUD."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from vibebot.api.auth import require_token

router = APIRouter(prefix="/api/acl", tags=["acl"], dependencies=[Depends(require_token)])


class RuleBody(BaseModel):
    mask: str
    permission: str
    note: str | None = None


@router.get("")
async def list_rules(request: Request) -> list[dict]:
    acl = request.app.state.bot.acl
    rules = await acl.list_rules()
    return [{"id": r.id, "mask": r.mask, "permission": r.permission, "note": r.note} for r in rules]


@router.post("")
async def add_rule(body: RuleBody, request: Request) -> dict:
    acl = request.app.state.bot.acl
    rule = await acl.add_rule(body.mask, body.permission, body.note)
    return {"id": rule.id, "status": "ok"}


@router.delete("/{rule_id}")
async def remove_rule(rule_id: int, request: Request) -> dict:
    acl = request.app.state.bot.acl
    removed = await acl.remove_rule(rule_id)
    if not removed:
        raise HTTPException(404, "Unknown rule")
    return {"status": "ok"}
