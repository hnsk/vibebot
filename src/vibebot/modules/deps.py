"""Per-repo `requirements.txt` installer for custom module repos."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from vibebot.storage.db import Database
from vibebot.storage.models import Repo

if TYPE_CHECKING:
    from vibebot.modules.registry import RepoRegistry

log = logging.getLogger(__name__)

_STDERR_TAIL = 4096
_STDOUT_TAIL = 4096


@dataclass
class InstallResult:
    ok: bool
    skipped: bool
    reason: str
    requirements_path: str | None
    stdout: str
    stderr: str
    duration: float
    returncode: int | None


class RepoDepsInstaller:
    """Installs `<repo>/requirements.txt` into the running interpreter via pip.

    Shared-env install by design: two repos pinning different versions of the
    same package cannot both be active in a single Python process. Last writer
    wins. See plan doc for trade-off discussion.
    """

    def __init__(self, repos: RepoRegistry, db: Database, *, timeout_s: float = 600.0) -> None:
        self._repos = repos
        self._db = db
        self._timeout_s = timeout_s
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, repo_name: str) -> asyncio.Lock:
        lock = self._locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[repo_name] = lock
        return lock

    def resolve_requirements_path(self, repo: Repo) -> Path | None:
        """Return path to requirements.txt: subdir first, then repo root."""
        base = self._repos.path_for(repo.name)
        if repo.subdir:
            candidate = self._repos.module_root_for(repo) / "requirements.txt"
            if candidate.is_file():
                return candidate
        root_candidate = base / "requirements.txt"
        if root_candidate.is_file():
            return root_candidate
        return None

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    async def status(self, repo_name: str) -> dict[str, object]:
        """Summary for UI: path, current hash, stored hash, install state."""
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == repo_name))).scalar_one_or_none()
        if repo is None:
            return {"exists": False}
        path = self.resolve_requirements_path(repo)
        current_hash = self._hash_file(path) if path else None
        if path is None:
            state = "missing"
        elif repo.deps_last_error:
            state = "error"
        elif repo.deps_hash is None:
            state = "stale"
        elif repo.deps_hash == current_hash:
            state = "clean"
        else:
            state = "stale"
        return {
            "exists": True,
            "path": str(path) if path else None,
            "current_hash": current_hash,
            "installed_hash": repo.deps_hash,
            "installed_at": repo.deps_installed_at.isoformat() if repo.deps_installed_at else None,
            "last_error": repo.deps_last_error,
            "state": state,
        }

    async def ensure_installed(self, repo_name: str, *, force: bool = False) -> InstallResult:
        async with self._lock_for(repo_name):
            return await self._install_locked(repo_name, force=force)

    async def _install_locked(self, repo_name: str, *, force: bool) -> InstallResult:
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == repo_name))).scalar_one_or_none()
        if repo is None:
            return InstallResult(
                ok=False, skipped=True, reason="unknown repo",
                requirements_path=None, stdout="", stderr="", duration=0.0, returncode=None,
            )
        path = self.resolve_requirements_path(repo)
        if path is None:
            return InstallResult(
                ok=True, skipped=True, reason="no requirements.txt",
                requirements_path=None, stdout="", stderr="", duration=0.0, returncode=None,
            )
        current_hash = self._hash_file(path)
        if not force and repo.deps_hash == current_hash and repo.deps_last_error is None:
            return InstallResult(
                ok=True, skipped=True, reason="up-to-date",
                requirements_path=str(path), stdout="", stderr="", duration=0.0, returncode=None,
            )

        log.info("Installing requirements for %s from %s", repo_name, path)
        t0 = time.monotonic()
        cmd = [sys.executable, "-m", "pip", "install", "-r", str(path)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            duration = time.monotonic() - t0
            await self._persist_error(repo_name, f"spawn failed: {exc}")
            return InstallResult(
                ok=False, skipped=False, reason="spawn failed",
                requirements_path=str(path), stdout="", stderr=str(exc),
                duration=duration, returncode=None,
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            duration = time.monotonic() - t0
            msg = f"pip timed out after {self._timeout_s:.0f}s"
            await self._persist_error(repo_name, msg)
            return InstallResult(
                ok=False, skipped=False, reason="timeout",
                requirements_path=str(path), stdout="", stderr=msg,
                duration=duration, returncode=None,
            )

        duration = time.monotonic() - t0
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode

        if rc == 0:
            importlib.invalidate_caches()
            await self._persist_success(repo_name, current_hash)
            log.info("Installed requirements for %s (%.1fs)", repo_name, duration)
            return InstallResult(
                ok=True, skipped=False, reason="installed",
                requirements_path=str(path),
                stdout=stdout[-_STDOUT_TAIL:], stderr=stderr[-_STDERR_TAIL:],
                duration=duration, returncode=rc,
            )

        err_tail = stderr[-_STDERR_TAIL:] or stdout[-_STDERR_TAIL:]
        await self._persist_error(repo_name, err_tail)
        log.warning("pip install failed for %s (rc=%s)", repo_name, rc)
        return InstallResult(
            ok=False, skipped=False, reason=f"pip rc={rc}",
            requirements_path=str(path),
            stdout=stdout[-_STDOUT_TAIL:], stderr=err_tail,
            duration=duration, returncode=rc,
        )

    async def _persist_success(self, repo_name: str, deps_hash: str) -> None:
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == repo_name))).scalar_one_or_none()
            if repo is None:
                return
            repo.deps_hash = deps_hash
            repo.deps_installed_at = datetime.now(UTC)
            repo.deps_last_error = None
            await s.commit()

    async def _persist_error(self, repo_name: str, error: str) -> None:
        async with self._db.session() as s:
            repo = (await s.execute(select(Repo).where(Repo.name == repo_name))).scalar_one_or_none()
            if repo is None:
                return
            repo.deps_last_error = error
            await s.commit()
