"""Async SQLite storage helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vibebot.storage.models import Base

# (table, column, sql-type) tuples applied once at startup for pre-existing DBs.
# create_all() adds new tables but never alters existing ones.
_ADD_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("module_state", "last_error", "TEXT"),
    ("repos", "subdir", "VARCHAR(255)"),
    ("repos", "deps_hash", "VARCHAR(64)"),
    ("repos", "deps_installed_at", "DATETIME"),
    ("repos", "deps_last_error", "TEXT"),
)


class Database:
    """Owns the async engine + session factory for the bot's SQLite store."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        url_path = ":memory:" if str(path) == ":memory:" else path.as_posix()
        self._url = f"sqlite+aiosqlite:///{url_path}"
        self._engine: AsyncEngine = create_async_engine(self._url, future=True)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def url(self) -> str:
        return self._url

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table, column, coltype in _ADD_COLUMNS:
                await self._add_column_if_missing(conn, table, column, coltype)

    @staticmethod
    async def _add_column_if_missing(conn: Any, table: str, column: str, coltype: str) -> None:
        rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
        if any(r[1] == column for r in rows):
            return
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))

    async def close(self) -> None:
        await self._engine.dispose()
