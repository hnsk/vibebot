"""ACL rule matching tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibebot.core.acl import AclService, Identity
from vibebot.storage.db import Database


@pytest.fixture()
async def db(tmp_path: Path):
    d = Database(tmp_path / "test.db")
    await d.create_all()
    try:
        yield d
    finally:
        await d.close()


def test_identity_parse_full():
    ident = Identity.parse("alice!~al@host.example.com")
    assert ident.nick == "alice"
    assert ident.ident == "~al"
    assert ident.host == "host.example.com"
    assert ident.mask() == "alice!~al@host.example.com"


def test_identity_parse_minimal():
    ident = Identity.parse("bob")
    assert ident.nick == "bob"
    assert ident.ident == "*"
    assert ident.host == "*"


async def test_check_matches_glob(db: Database):
    acl = AclService(db)
    await acl.add_rule("admin!*@*.example.com", "admin")
    assert await acl.check(Identity.parse("admin!foo@bar.example.com"), "admin") is True
    assert await acl.check(Identity.parse("admin!foo@evil.net"), "admin") is False
    assert await acl.check(Identity.parse("alice!foo@bar.example.com"), "admin") is False


async def test_check_wrong_permission(db: Database):
    acl = AclService(db)
    await acl.add_rule("*!*@*", "moderate")
    assert await acl.check(Identity.parse("x!y@z"), "admin") is False
    assert await acl.check(Identity.parse("x!y@z"), "moderate") is True


async def test_remove_rule(db: Database):
    acl = AclService(db)
    rule = await acl.add_rule("alice!*@*", "admin")
    assert await acl.remove_rule(rule.id) is True
    assert await acl.check(Identity.parse("alice!x@y"), "admin") is False
