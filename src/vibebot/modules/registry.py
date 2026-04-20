"""Git repository registry — clone/pull module source repositories."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from git import Repo as GitRepo  # type: ignore[import-untyped]
from sqlalchemy import delete, select

from vibebot.config import RepoConfig
from vibebot.storage.db import Database
from vibebot.storage.models import Repo

log = logging.getLogger(__name__)


def _validate_subdir(subdir: str | None) -> str | None:
    """Normalize a repo subdir to a safe relative POSIX path, or return None.

    Rejects absolute paths and any `..` segment so a repo can never escape its
    clone directory.
    """
    if subdir is None:
        return None
    s = subdir.strip().strip("/")
    if not s:
        return None
    p = PurePosixPath(s)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        raise ValueError(f"invalid subdir: {subdir!r}")
    return str(p)


class RepoRegistry:
    """Tracks module repos in the DB and mirrors them to a local directory on disk."""

    def __init__(self, db: Database, *, default_repos: list[RepoConfig], modules_dir: str) -> None:
        self._db = db
        self._default_repos = default_repos
        self._root = Path(modules_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, repo_name: str) -> Path:
        return self._root / repo_name

    def module_root_for(self, repo: Repo) -> Path:
        """Directory inside the clone where module packages live.

        `<modules_dir>/<repo.name>/<repo.subdir>` if subdir set, else the clone
        root itself.
        """
        base = self.path_for(repo.name)
        sub = _validate_subdir(repo.subdir)
        return base / sub if sub else base

    async def sync_from_config(self) -> None:
        """Ensure every repo listed in config is present in the DB (idempotent)."""
        if not self._default_repos:
            return
        async with self._db.session() as s:
            existing = {r.name for r in (await s.execute(select(Repo))).scalars()}
            for cfg in self._default_repos:
                if cfg.name in existing:
                    continue
                s.add(Repo(
                    name=cfg.name,
                    url=cfg.url,
                    branch=cfg.branch,
                    subdir=_validate_subdir(cfg.subdir),
                    enabled=cfg.enabled,
                ))
            await s.commit()

    async def list_repos(self) -> list[Repo]:
        async with self._db.session() as s:
            return list((await s.execute(select(Repo))).scalars())

    async def get_repo(self, name: str) -> Repo | None:
        async with self._db.session() as s:
            return (await s.execute(select(Repo).where(Repo.name == name))).scalar_one_or_none()

    async def add_repo(
        self,
        name: str,
        url: str,
        branch: str = "main",
        enabled: bool = True,
        subdir: str | None = None,
    ) -> Repo:
        async with self._db.session() as s:
            repo = Repo(
                name=name,
                url=url,
                branch=branch,
                subdir=_validate_subdir(subdir),
                enabled=enabled,
            )
            s.add(repo)
            await s.commit()
            await s.refresh(repo)
            return repo

    async def update_repo(
        self,
        name: str,
        *,
        url: str | None = None,
        branch: str | None = None,
        subdir: str | None = None,
        enabled: bool | None = None,
        clear_subdir: bool = False,
    ) -> Repo | None:
        """Patch an existing repo row. `clear_subdir=True` sets subdir to NULL."""
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == name))).scalar_one_or_none()
            if repo is None:
                return None
            if url is not None:
                repo.url = url
            if branch is not None:
                repo.branch = branch
            if enabled is not None:
                repo.enabled = enabled
            if clear_subdir:
                repo.subdir = None
            elif subdir is not None:
                repo.subdir = _validate_subdir(subdir)
            await s.commit()
            await s.refresh(repo)
            return repo

    async def remove_repo(self, name: str) -> bool:
        async with self._db.session() as s:
            result = await s.execute(delete(Repo).where(Repo.name == name))
            await s.commit()
            removed = (result.rowcount or 0) > 0
        if removed:
            shutil.rmtree(self.path_for(name), ignore_errors=True)
        return removed

    async def set_enabled(self, name: str, enabled: bool) -> None:
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == name))).scalar_one_or_none()
            if repo is None:
                return
            repo.enabled = enabled
            await s.commit()

    async def clone_or_pull(self, name: str) -> Path:
        """Ensure `<modules_dir>/<name>` is an up-to-date clone. Returns the path."""
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == name))).scalar_one()
            path = self.path_for(name)
            if path.exists():
                log.info("Pulling %s", name)
                git_repo = GitRepo(str(path))
                git_repo.remotes.origin.fetch()
                git_repo.git.checkout(repo.branch)
                git_repo.remotes.origin.pull()
            else:
                log.info("Cloning %s from %s", name, repo.url)
                GitRepo.clone_from(repo.url, str(path), branch=repo.branch)
            repo.last_pulled_at = datetime.now(UTC)
            await s.commit()
            return path
