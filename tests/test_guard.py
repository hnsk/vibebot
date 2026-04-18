"""Module exception-guard tests."""

from __future__ import annotations

import asyncio
import contextlib

from vibebot.core.guard import guard_callback, spawn_guarded


async def test_guard_callback_suppresses_exception():
    async def boom(_):
        raise RuntimeError("boom")

    wrapped = guard_callback("mod", boom)
    result = await wrapped(None)
    assert result is None


async def test_guard_callback_passes_result():
    async def ok(x):
        return x + 1

    wrapped = guard_callback("mod", ok)
    assert await wrapped(41) == 42


async def test_spawn_guarded_does_not_propagate():
    async def boom():
        raise RuntimeError("nope")

    task = spawn_guarded("mod", boom(), name="t")
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert task.exception() is None


async def test_spawn_guarded_cancel_propagates():
    started = asyncio.Event()

    async def slow():
        started.set()
        await asyncio.sleep(10)

    task = spawn_guarded("mod", slow(), name="t")
    await started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
