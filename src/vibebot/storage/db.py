"""Async SQLite storage helpers."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vibebot.storage.models import Base


class Database:
    """Owns the async engine + session factory for the bot's SQLite store."""

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._url = f"sqlite+aiosqlite:///{path.as_posix()}"
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

    async def close(self) -> None:
        await self._engine.dispose()
