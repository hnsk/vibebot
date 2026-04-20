"""Base class modules subclass to plug into the bot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from vibebot.core.events import Event
from vibebot.modules.decorators import (
    on_connect,
    on_ctcp,
    on_join,
    on_kick,
    on_message,
    on_mode,
    on_nick,
    on_part,
    on_quit,
    on_topic,
)
from vibebot.modules.settings import sanitize_segment
from vibebot.modules.triggers import (
    ModeMatch,
    TriggerDescriptor,
    TriggerKind,
    build_match,
    compile_excludes,
)

__all__ = [
    "Module",
    "ScheduledTask",
    "on_connect",
    "on_ctcp",
    "on_join",
    "on_kick",
    "on_message",
    "on_mode",
    "on_nick",
    "on_part",
    "on_quit",
    "on_topic",
]

if TYPE_CHECKING:
    from vibebot.core.bot import VibeBot


@dataclass
class ScheduledTask:
    """Declarative scheduled task a module exposes.

    `trigger` is a dict passed through to APScheduler's `add_job`:
        {"type": "interval", "minutes": 5}
        {"type": "cron", "hour": 7}
    """

    name: str
    func: Callable[[], Awaitable[None]]
    trigger: dict[str, Any]


class Module:
    """Subclass this in a module's entry point.

    To declare typed settings, set ``Settings = SomePydanticModel`` on the
    subclass. The loader validates stored values against it and exposes the
    validated model on ``self.settings`` before ``on_load`` runs. Fields may
    use ``pydantic.SecretStr`` for passwords/API keys (masked in the API) and
    ``pydantic.HttpUrl`` for URLs. Defaults declared on the ``Settings`` class
    are the defaults used when nothing is stored.
    """

    name: str = ""
    description: str = ""
    Settings: type[BaseModel] | None = None

    def __init__(self, bot: VibeBot, config: dict[str, Any] | None = None) -> None:
        self.bot = bot
        self.config = config or {}
        # Set by ModuleManager before on_load (so on_load can read them).
        self.settings: Any = None
        self._repo: str = ""
        self._name: str = ""
        # Drained by ModuleManager after on_load; holds settings-driven triggers
        # as (descriptor, handler) pairs.
        self._pending_triggers: list[
            tuple[TriggerDescriptor, Callable[[Event], Awaitable[None]]]
        ] = []

    @property
    def data_dir(self) -> Path:
        """Sandboxed, auto-created scratch directory unique to this module.

        Layout: ``<bot.modules_data_dir>/<repo>/<name>/``. Safe to call
        repeatedly — the directory is created on first access. Refuses to
        return a path outside the configured base directory.
        """
        base = Path(self.bot.config.bot.modules_data_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        repo = sanitize_segment(self._repo or "unknown")
        name = sanitize_segment(self._name or self.name or "unknown")
        target = (base / repo / name).resolve()
        if not target.is_relative_to(base):
            raise RuntimeError(f"module data_dir escapes sandbox: {target}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def on_load(self) -> None:
        """Called once when the module is loaded."""

    async def on_unload(self) -> None:
        """Called once when the module is unloaded."""

    def register_trigger(
        self,
        kind: TriggerKind,
        *,
        handler: Callable[[Event], Awaitable[None]],
        regex: str | None = None,
        startswith: str | None = None,
        exact: str | None = None,
        predicate: Callable[[Event], bool] | None = None,
        ctcp_type: str | None = None,
        mode_letters: Sequence[str] | None = None,
        mode_direction: Literal["+", "-", "*"] = "*",
        excludes: Sequence[str] = (),
        field: str = "message",
        case_sensitive: bool = True,
    ) -> None:
        """Register a trigger dynamically. Call from ``on_load`` when the
        trigger depends on ``self.settings``; module reload refreshes it.

        For ``kind='mode'`` this always produces a ``ModeMatch`` — pass
        ``mode_letters``/``mode_direction`` to narrow it.
        """
        if kind == "mode":
            match = ModeMatch(
                letters=frozenset(mode_letters) if mode_letters is not None else None,
                direction=mode_direction,
            )
        else:
            match = build_match(
                regex=regex,
                startswith=startswith,
                exact=exact,
                predicate=predicate,
                ctcp_type=ctcp_type,
                field=field,
                case_sensitive=case_sensitive,
                always=(
                    regex is None
                    and startswith is None
                    and exact is None
                    and predicate is None
                    and ctcp_type is None
                ),
            )
        descriptor = TriggerDescriptor(
            kind=kind,
            match=match,
            excludes=compile_excludes(excludes),
        )
        self._pending_triggers.append((descriptor, handler))

    def scheduled_tasks(self) -> list[ScheduledTask]:
        """Return scheduled tasks this module wants APScheduler to run."""
        return []

    def register_handler(
        self,
        name: str,
        func: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Register a named handler that schedules may dispatch to.

        Handlers take the schedule's stored `payload` dict. Call this from
        `on_load`; the module loader unregisters all of a module's handlers
        on unload.
        """
        self.bot.schedules.register_handler(self._repo, self._name, name, func)
