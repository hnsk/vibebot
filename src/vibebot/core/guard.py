"""Exception shield for module-supplied callbacks and tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

log = logging.getLogger("vibebot.module")


def guard_callback[T](
    module_name: str, fn: Callable[..., Awaitable[T]]
) -> Callable[..., Awaitable[T | None]]:
    """Wrap an async callback so exceptions are logged rather than bubbling up."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T | None:
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Module %r callback %s raised; suppressed.", module_name, fn.__name__)
            return None

    return wrapper


def spawn_guarded(
    module_name: str,
    coro: Awaitable[Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Schedule a coroutine as a task with a logging exception handler."""

    async def runner() -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Module %r task %r crashed; suppressed.", module_name, name)

    return asyncio.create_task(runner(), name=name or f"module:{module_name}")
