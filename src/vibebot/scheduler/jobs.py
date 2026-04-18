"""APScheduler wrapper with a SQLAlchemy job store so jobs survive restarts."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore  # type: ignore[import-untyped]
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.date import DateTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


def _build_trigger(spec: dict[str, Any]) -> Any:
    """Turn a `{"type": "interval", ...}` dict into an APScheduler trigger."""
    params = dict(spec)
    trigger_type = params.pop("type", "interval")
    if trigger_type == "interval":
        return IntervalTrigger(**params)
    if trigger_type == "cron":
        return CronTrigger(**params)
    if trigger_type == "date":
        return DateTrigger(**params)
    raise ValueError(f"Unknown trigger type: {trigger_type!r}")


class SchedulerService:
    """Thin async facade over APScheduler with a persistent SQLite job store."""

    def __init__(self, database_url: str) -> None:
        # APScheduler's SQLAlchemyJobStore uses sync SQLAlchemy; map our aiosqlite URL.
        sync_url = database_url.replace("sqlite+aiosqlite", "sqlite")
        jobstore = SQLAlchemyJobStore(url=sync_url)
        self._scheduler = AsyncIOScheduler(jobstores={"default": jobstore})

    async def start(self) -> None:
        self._scheduler.start()

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)

    def add_job(
        self,
        func: Callable[[], Awaitable[None]],
        *,
        trigger: dict[str, Any],
        job_id: str,
    ) -> None:
        self._scheduler.add_job(
            func,
            trigger=_build_trigger(trigger),
            id=job_id,
            replace_existing=True,
        )

    def remove_job(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            log.debug("remove_job(%s) failed; probably absent", job_id, exc_info=True)

    def pause_job(self, job_id: str) -> None:
        try:
            self._scheduler.pause_job(job_id)
        except Exception:
            log.debug("pause_job(%s) failed", job_id, exc_info=True)

    def resume_job(self, job_id: str) -> None:
        try:
            self._scheduler.resume_job(job_id)
        except Exception:
            log.debug("resume_job(%s) failed", job_id, exc_info=True)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [
            {
                "id": job.id,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
            }
            for job in self._scheduler.get_jobs()
        ]
