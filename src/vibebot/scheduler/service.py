"""Runtime schedule service: CRUD over the `schedules` table + APScheduler jobs.

The `schedules` SQL table is the authoritative store. APScheduler runs the
actual fires, but we re-register every job on startup from the DB, so the
firing closure can hold a live reference to the running bot / handler — no
pickled callables, no cross-process assumptions.

Handlers are registered by modules during `on_load`. A schedule references
a handler by `(repo_name, module_name, handler_name)`. If the referenced
module is not loaded when a fire occurs, the dispatcher logs and skips; the
schedule row stays `scheduled` so the next tick runs once the module comes
back.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_REMOVED,
)
from sqlalchemy import select

from vibebot.core.acl import AclService, Identity
from vibebot.core.guard import spawn_guarded
from vibebot.scheduler.jobs import SchedulerService, _build_trigger
from vibebot.storage.db import Database
from vibebot.storage.models import Schedule

if TYPE_CHECKING:
    from vibebot.core.bot import VibeBot

log = logging.getLogger(__name__)

HandlerFn = Callable[[dict[str, Any]], Awaitable[None]]
ADMIN_PERMISSION = "admin"
JOB_ID_PREFIX = "user:"
DEFAULT_PER_OWNER_MAX = 50


class ScheduleError(Exception):
    """Raised for invalid schedule operations (not found, unauthorized, invalid input)."""


@dataclass
class ScheduleDTO:
    id: str
    owner_nick: str
    owner_mask: str
    owner_network: str | None
    repo_name: str
    module_name: str
    handler_name: str
    payload: dict[str, Any]
    trigger: dict[str, Any]
    status: str
    title: str | None
    misfire_grace_seconds: int
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None

    @classmethod
    def from_row(cls, row: Schedule) -> ScheduleDTO:
        return cls(
            id=row.id,
            owner_nick=row.owner_nick,
            owner_mask=row.owner_mask,
            owner_network=row.owner_network,
            repo_name=row.repo_name,
            module_name=row.module_name,
            handler_name=row.handler_name,
            payload=_json_loads(row.payload_json),
            trigger=_json_loads(row.trigger_json),
            status=row.status,
            title=row.title,
            misfire_grace_seconds=row.misfire_grace_seconds,
            created_at=row.created_at,
            updated_at=row.updated_at,
            next_run_at=row.next_run_at,
            last_run_at=row.last_run_at,
            last_error=row.last_error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_nick": self.owner_nick,
            "owner_mask": self.owner_mask,
            "owner_network": self.owner_network,
            "repo": self.repo_name,
            "module": self.module_name,
            "handler": self.handler_name,
            "payload": self.payload,
            "trigger": self.trigger,
            "status": self.status,
            "title": self.title,
            "misfire_grace_seconds": self.misfire_grace_seconds,
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
            "next_run_at": _iso_utc(self.next_run_at),
            "last_run_at": _iso_utc(self.last_run_at),
            "last_error": self.last_error,
        }


def _iso_utc(dt: datetime | None) -> str | None:
    """Serialize a datetime as a UTC-anchored ISO string.

    SQLite strips timezone info on round-trip, so naive datetimes coming out of
    the DB are assumed to be UTC (we only ever write UTC). Output includes an
    explicit offset so JS `new Date(...)` treats it correctly in any locale.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


class ScheduleService:
    """CRUD + fire routing for persisted schedules."""

    def __init__(
        self,
        bot: VibeBot,
        scheduler: SchedulerService,
        db: Database,
        acl: AclService,
        *,
        per_owner_max: int = DEFAULT_PER_OWNER_MAX,
    ) -> None:
        self._bot = bot
        self._scheduler = scheduler
        self._db = db
        self._acl = acl
        self._per_owner_max = per_owner_max
        # (repo, module, handler) -> callable
        self._handlers: dict[tuple[str, str, str], HandlerFn] = {}
        scheduler.add_listener(self._on_job_event, EVENT_JOB_MISSED | EVENT_JOB_REMOVED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    # ---------- handler registry ----------

    def register_handler(
        self,
        repo: str,
        module: str,
        handler_name: str,
        func: HandlerFn,
    ) -> None:
        key = (repo, module, handler_name)
        self._handlers[key] = func
        # Re-register any rows that reference this handler in case the job was
        # cleared when the module was last unloaded. resume_job() on an unknown
        # job is harmless (SchedulerService swallows the error).
        spawn_guarded(
            "schedules",
            self._rearm_handler(repo, module, handler_name),
            name=f"schedules:rearm:{repo}/{module}/{handler_name}",
        )

    def unregister_handlers_for(self, repo: str, module: str) -> None:
        to_drop = [key for key in self._handlers if key[0] == repo and key[1] == module]
        for key in to_drop:
            del self._handlers[key]
        # Pause APScheduler jobs for schedules referencing this module so no
        # fire lands without a handler. DB `status` stays `scheduled`; resumes
        # when the module reloads.
        spawn_guarded(
            "schedules",
            self._pause_for_module(repo, module),
            name=f"schedules:pause:{repo}/{module}",
        )

    async def _rearm_handler(self, repo: str, module: str, handler_name: str) -> None:
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(Schedule).where(
                            Schedule.repo_name == repo,
                            Schedule.module_name == module,
                            Schedule.handler_name == handler_name,
                            Schedule.status.in_(("scheduled", "paused")),
                        )
                    )
                ).scalars()
            )
        for row in rows:
            dto = ScheduleDTO.from_row(row)
            self._register_job(dto)
            if dto.status == "paused":
                self._scheduler.pause_job(_job_id(dto.id))

    async def _pause_for_module(self, repo: str, module: str) -> None:
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(Schedule.id).where(
                            Schedule.repo_name == repo,
                            Schedule.module_name == module,
                            Schedule.status == "scheduled",
                        )
                    )
                ).scalars()
            )
        for schedule_id in rows:
            self._scheduler.remove_job(_job_id(schedule_id))

    # ---------- startup rehydrate ----------

    async def rehydrate(self) -> None:
        """Re-register APScheduler jobs for every active schedule row."""
        async with self._db.session() as s:
            rows = list(
                (
                    await s.execute(
                        select(Schedule).where(Schedule.status.in_(("scheduled", "paused")))
                    )
                ).scalars()
            )
        for row in rows:
            dto = ScheduleDTO.from_row(row)
            try:
                self._register_job(dto)
            except Exception:
                log.exception("Failed to rehydrate schedule %s", dto.id)
                continue
            if dto.status == "paused":
                self._scheduler.pause_job(_job_id(dto.id))

    # ---------- CRUD ----------

    async def create(
        self,
        *,
        owner_nick: str,
        owner_mask: str,
        repo: str,
        module: str,
        handler: str,
        trigger: dict[str, Any],
        payload: dict[str, Any] | None = None,
        title: str | None = None,
        misfire_grace_seconds: int = 60,
        owner_network: str | None = None,
        requester: Identity | None = None,
    ) -> ScheduleDTO:
        _build_trigger(trigger)  # validate up-front, raises ValueError
        payload = payload or {}
        # Per-owner cap (admins bypass).
        is_admin = requester is not None and await self._acl.check(requester, ADMIN_PERMISSION)
        if not is_admin:
            async with self._db.session() as s:
                count_rows = list(
                    (
                        await s.execute(
                            select(Schedule.id).where(
                                Schedule.owner_mask == owner_mask,
                                Schedule.status.in_(("scheduled", "paused")),
                            )
                        )
                    ).scalars()
                )
            if len(count_rows) >= self._per_owner_max:
                raise ScheduleError(f"owner {owner_mask!r} exceeded schedule cap ({self._per_owner_max})")

        schedule_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        row = Schedule(
            id=schedule_id,
            owner_nick=owner_nick,
            owner_mask=owner_mask,
            owner_network=owner_network,
            repo_name=repo,
            module_name=module,
            handler_name=handler,
            payload_json=json.dumps(payload),
            trigger_json=json.dumps(trigger, default=str),
            status="scheduled",
            title=title,
            misfire_grace_seconds=misfire_grace_seconds,
            created_at=now,
            updated_at=now,
        )
        async with self._db.session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        dto = ScheduleDTO.from_row(row)
        self._register_job(dto)
        await self._sync_next_run(schedule_id)
        return await self.get(schedule_id)

    async def get(self, schedule_id: str) -> ScheduleDTO:
        async with self._db.session() as s:
            row = (
                await s.execute(select(Schedule).where(Schedule.id == schedule_id))
            ).scalar_one_or_none()
        if row is None:
            raise ScheduleError(f"schedule {schedule_id!r} not found")
        return ScheduleDTO.from_row(row)

    async def list(
        self,
        *,
        owner_mask: str | None = None,
        repo: str | None = None,
        module: str | None = None,
        status: str | None = None,
    ) -> list[ScheduleDTO]:
        async with self._db.session() as s:
            stmt = select(Schedule)
            if owner_mask is not None:
                stmt = stmt.where(Schedule.owner_mask == owner_mask)
            if repo is not None:
                stmt = stmt.where(Schedule.repo_name == repo)
            if module is not None:
                stmt = stmt.where(Schedule.module_name == module)
            if status is not None:
                stmt = stmt.where(Schedule.status == status)
            rows = list((await s.execute(stmt)).scalars())
        return [ScheduleDTO.from_row(r) for r in rows]

    async def update(
        self,
        schedule_id: str,
        *,
        trigger: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        title: str | None = None,
        requester: Identity | None = None,
    ) -> ScheduleDTO:
        dto = await self.get(schedule_id)
        await self._authorize(dto, requester)
        if trigger is not None:
            _build_trigger(trigger)
        async with self._db.session() as s:
            row = (await s.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
            if trigger is not None:
                row.trigger_json = json.dumps(trigger, default=str)
            if payload is not None:
                row.payload_json = json.dumps(payload)
            if title is not None:
                row.title = title
            row.updated_at = datetime.now(UTC)
            await s.commit()
        if trigger is not None:
            try:
                self._scheduler.reschedule_job(_job_id(schedule_id), trigger)
            except Exception:
                # Job may be missing (e.g. handler unloaded); re-register fresh.
                self._register_job(await self.get(schedule_id))
        await self._sync_next_run(schedule_id)
        return await self.get(schedule_id)

    async def cancel(
        self,
        schedule_id: str,
        *,
        requester: Identity | None = None,
        hard: bool = False,
    ) -> None:
        dto = await self.get(schedule_id)
        await self._authorize(dto, requester)
        self._scheduler.remove_job(_job_id(schedule_id))
        async with self._db.session() as s:
            if hard:
                row = (
                    await s.execute(select(Schedule).where(Schedule.id == schedule_id))
                ).scalar_one()
                await s.delete(row)
            else:
                row = (
                    await s.execute(select(Schedule).where(Schedule.id == schedule_id))
                ).scalar_one()
                row.status = "cancelled"
                row.updated_at = datetime.now(UTC)
            await s.commit()

    async def pause(self, schedule_id: str, *, requester: Identity | None = None) -> ScheduleDTO:
        dto = await self.get(schedule_id)
        await self._authorize(dto, requester)
        self._scheduler.pause_job(_job_id(schedule_id))
        return await self._set_status(schedule_id, "paused")

    async def resume(self, schedule_id: str, *, requester: Identity | None = None) -> ScheduleDTO:
        dto = await self.get(schedule_id)
        await self._authorize(dto, requester)
        if self._scheduler.get_job(_job_id(schedule_id)) is None:
            self._register_job(dto)
        self._scheduler.resume_job(_job_id(schedule_id))
        return await self._set_status(schedule_id, "scheduled")

    async def run_now(self, schedule_id: str, *, requester: Identity | None = None) -> ScheduleDTO:
        dto = await self.get(schedule_id)
        await self._authorize(dto, requester)
        if self._scheduler.get_job(_job_id(schedule_id)) is None:
            self._register_job(dto)
        self._scheduler.run_job_now(_job_id(schedule_id))
        return await self.get(schedule_id)

    # ---------- internals ----------

    async def _set_status(self, schedule_id: str, status: str) -> ScheduleDTO:
        async with self._db.session() as s:
            row = (await s.execute(select(Schedule).where(Schedule.id == schedule_id))).scalar_one()
            row.status = status
            row.updated_at = datetime.now(UTC)
            await s.commit()
        return await self.get(schedule_id)

    async def _sync_next_run(self, schedule_id: str) -> None:
        job = self._scheduler.get_job(_job_id(schedule_id))
        if job is None:
            return
        next_run: datetime | None = getattr(job, "next_run_time", None)
        async with self._db.session() as s:
            row = (
                await s.execute(select(Schedule).where(Schedule.id == schedule_id))
            ).scalar_one_or_none()
            if row is None:
                return
            row.next_run_at = next_run
            await s.commit()

    async def _authorize(self, dto: ScheduleDTO, requester: Identity | None) -> None:
        if requester is None:
            return  # trusted in-process / API-token caller
        if await self._acl.check(requester, ADMIN_PERMISSION):
            return
        import fnmatch

        if fnmatch.fnmatchcase(requester.mask(), dto.owner_mask):
            return
        raise ScheduleError("not authorized for this schedule")

    def _register_job(self, dto: ScheduleDTO) -> None:
        schedule_id = dto.id

        async def _fire() -> None:
            await self._dispatch(schedule_id)

        self._scheduler.add_job(
            _fire,
            trigger=dto.trigger,
            job_id=_job_id(schedule_id),
            misfire_grace_time=dto.misfire_grace_seconds,
            jobstore="memory",
        )

    async def _dispatch(self, schedule_id: str) -> None:
        try:
            dto = await self.get(schedule_id)
        except ScheduleError:
            log.warning("dispatch: schedule %s missing; removing job", schedule_id)
            self._scheduler.remove_job(_job_id(schedule_id))
            return
        handler = self._handlers.get((dto.repo_name, dto.module_name, dto.handler_name))
        if handler is None:
            log.warning(
                "dispatch: handler %s/%s#%s not registered; skipping fire for %s",
                dto.repo_name,
                dto.module_name,
                dto.handler_name,
                schedule_id,
            )
            return
        now = datetime.now(UTC)
        error: str | None = None
        try:
            await handler(dto.payload)
        except Exception as exc:
            log.exception("schedule %s handler raised", schedule_id)
            error = repr(exc)
        async with self._db.session() as s:
            row = (
                await s.execute(select(Schedule).where(Schedule.id == schedule_id))
            ).scalar_one_or_none()
            if row is None:
                return
            row.last_run_at = now
            row.last_error = error
            # One-shot (`date`) triggers: APScheduler removes the job after fire.
            # Mark `completed` so it shows up accurately in listings.
            trigger = _json_loads(row.trigger_json)
            if trigger.get("type") == "date":
                row.status = "error" if error else "completed"
            elif error:
                # Recurring jobs stay scheduled; just record the error.
                pass
            row.updated_at = now
            await s.commit()
        await self._sync_next_run(schedule_id)

    def _on_job_event(self, event: Any) -> None:
        """APScheduler listener — updates DB for misses / removals we didn't cause."""
        job_id: str = getattr(event, "job_id", "")
        if not job_id.startswith(JOB_ID_PREFIX):
            return
        schedule_id = job_id[len(JOB_ID_PREFIX):]
        code = event.code
        if code == EVENT_JOB_MISSED:
            spawn_guarded(
                "schedules",
                self._handle_missed(schedule_id),
                name=f"schedules:missed:{schedule_id}",
            )

    async def _handle_missed(self, schedule_id: str) -> None:
        async with self._db.session() as s:
            row = (
                await s.execute(select(Schedule).where(Schedule.id == schedule_id))
            ).scalar_one_or_none()
            if row is None:
                return
            trigger = _json_loads(row.trigger_json)
            if trigger.get("type") == "date":
                row.status = "missed"
                row.updated_at = datetime.now(UTC)
                await s.commit()
                self._scheduler.remove_job(_job_id(schedule_id))
            else:
                # Recurring: just log; job continues.
                log.warning("schedule %s missed a fire (grace exceeded)", schedule_id)


def _job_id(schedule_id: str) -> str:
    return f"{JOB_ID_PREFIX}{schedule_id}"
