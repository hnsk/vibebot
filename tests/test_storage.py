"""Storage layer smoke test."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from vibebot.storage.db import Database
from vibebot.storage.models import Repo


@pytest.fixture()
async def db(tmp_path: Path):
    d = Database(tmp_path / "smoke.db")
    await d.create_all()
    try:
        yield d
    finally:
        await d.close()


async def test_repo_roundtrip(db: Database):
    async with db.session() as s:
        s.add(Repo(name="alpha", url="https://example.com/a.git", branch="main"))
        await s.commit()
    async with db.session() as s:
        result = await s.execute(select(Repo).where(Repo.name == "alpha"))
        repo = result.scalar_one()
        assert repo.url == "https://example.com/a.git"
        assert repo.branch == "main"
