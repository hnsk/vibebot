"""ACL: match nick!ident@host globs against permission tokens stored in SQLite."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from sqlalchemy import delete, select

from vibebot.storage.db import Database
from vibebot.storage.models import AclRule


@dataclass(frozen=True)
class Identity:
    nick: str
    ident: str
    host: str

    @classmethod
    def parse(cls, prefix: str) -> Identity:
        """Parse an IRC userhost like 'nick!ident@host'. Missing parts → '*'."""
        nick, rest = ([*prefix.split("!", 1), ""])[:2]
        ident, host = ([*rest.split("@", 1), ""])[:2] if rest else ("", "")
        return cls(nick=nick or "*", ident=ident or "*", host=host or "*")

    def mask(self) -> str:
        return f"{self.nick}!{self.ident}@{self.host}"


def _match_mask(mask: str, identity: Identity) -> bool:
    return fnmatch.fnmatchcase(identity.mask(), mask)


class AclService:
    """CRUD + check() for ACL rules. Rules are `(mask_glob, permission)` pairs."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_rules(self) -> list[AclRule]:
        async with self._db.session() as s:
            result = await s.execute(select(AclRule))
            return list(result.scalars())

    async def add_rule(self, mask: str, permission: str, note: str | None = None) -> AclRule:
        async with self._db.session() as s:
            rule = AclRule(mask=mask, permission=permission, note=note)
            s.add(rule)
            await s.commit()
            await s.refresh(rule)
            return rule

    async def remove_rule(self, rule_id: int) -> bool:
        async with self._db.session() as s:
            result = await s.execute(delete(AclRule).where(AclRule.id == rule_id))
            await s.commit()
            return (result.rowcount or 0) > 0

    async def check(self, identity: Identity, permission: str) -> bool:
        """True iff any rule with this permission matches the identity mask."""
        async with self._db.session() as s:
            result = await s.execute(select(AclRule).where(AclRule.permission == permission))
            for rule in result.scalars():
                if _match_mask(rule.mask, identity):
                    return True
        return False
