"""Base class modules subclass to plug into the bot."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibebot.core.events import Event

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
    """Subclass this in a module's entry point."""

    name: str = ""
    description: str = ""

    def __init__(self, bot: VibeBot, config: dict[str, Any] | None = None) -> None:
        self.bot = bot
        self.config = config or {}

    async def on_load(self) -> None:
        """Called once when the module is loaded."""

    async def on_unload(self) -> None:
        """Called once when the module is unloaded."""

    async def on_message(self, event: Event) -> None:
        """Channel or private message received."""

    async def on_event(self, event: Event) -> None:
        """Any other IRC event (join, part, kick, nick, connect)."""

    def scheduled_tasks(self) -> list[ScheduledTask]:
        """Return scheduled tasks this module wants APScheduler to run."""
        return []
