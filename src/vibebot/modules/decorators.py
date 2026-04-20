"""Decorators modules use to declare triggers on handler methods.

Each decorator validates its arguments at decoration time, builds a
``TriggerDescriptor``, and appends it to ``func._vb_triggers``. The decorator
returns the function unchanged so handlers remain directly callable in tests.

Stacking is supported — applying ``@on_message`` twice on one method produces
two descriptors and, after binding, two triggers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from vibebot.modules.triggers import (
    ModeMatch,
    TriggerDescriptor,
    TriggerKind,
    build_match,
    compile_excludes,
)

if TYPE_CHECKING:
    from vibebot.core.events import Event


_Handler = Callable[..., Any]


def _attach(func: _Handler, descriptor: TriggerDescriptor) -> _Handler:
    triggers = getattr(func, "_vb_triggers", None)
    if triggers is None:
        triggers = []
        func._vb_triggers = triggers  # type: ignore[attr-defined]
    triggers.append(descriptor)
    return func


def _decorator(
    kind: TriggerKind,
    *,
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
    always: bool = False,
) -> Callable[[_Handler], _Handler]:
    match = build_match(
        regex=regex,
        startswith=startswith,
        exact=exact,
        predicate=predicate,
        ctcp_type=ctcp_type,
        mode_letters=mode_letters,
        mode_direction=mode_direction,
        field=field,
        case_sensitive=case_sensitive,
        always=always,
    )
    compiled_excludes = compile_excludes(excludes)
    descriptor = TriggerDescriptor(kind=kind, match=match, excludes=compiled_excludes)

    def wrap(func: _Handler) -> _Handler:
        return _attach(func, descriptor)

    return wrap


def on_message(
    *,
    regex: str | None = None,
    startswith: str | None = None,
    exact: str | None = None,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
    field: str = "message",
    case_sensitive: bool = True,
) -> Callable[[_Handler], _Handler]:
    """Trigger on ``message`` events. Requires one of regex/startswith/exact/predicate."""
    if regex is None and startswith is None and exact is None and predicate is None:
        raise TypeError(
            "@on_message requires one of regex=, startswith=, exact=, predicate="
        )
    return _decorator(
        "message",
        regex=regex,
        startswith=startswith,
        exact=exact,
        predicate=predicate,
        excludes=excludes,
        field=field,
        case_sensitive=case_sensitive,
    )


def on_mode(
    *,
    letters: Sequence[str] | None = None,
    direction: Literal["+", "-", "*"] = "*",
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    """Trigger on channel mode changes."""
    match = ModeMatch(
        letters=frozenset(letters) if letters is not None else None,
        direction=direction,
    )
    descriptor = TriggerDescriptor(
        kind="mode", match=match, excludes=compile_excludes(excludes)
    )

    def wrap(func: _Handler) -> _Handler:
        return _attach(func, descriptor)

    return wrap


def on_topic(
    *,
    regex: str | None = None,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    """Trigger on topic changes. Default matches any topic event."""
    if regex is not None or predicate is not None:
        return _decorator(
            "topic",
            regex=regex,
            predicate=predicate,
            excludes=excludes,
            field="topic",
        )
    return _decorator("topic", always=True, excludes=excludes)


def on_ctcp(
    *,
    type: str,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    """Trigger on a specific CTCP request type (e.g. ``VERSION``, ``ACTION``)."""
    if not type:
        raise TypeError("@on_ctcp requires type=")
    return _decorator("ctcp", ctcp_type=type, excludes=excludes)


def on_join(
    *,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    if predicate is not None:
        return _decorator("join", predicate=predicate, excludes=excludes)
    return _decorator("join", always=True, excludes=excludes)


def on_part(
    *,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    if predicate is not None:
        return _decorator("part", predicate=predicate, excludes=excludes)
    return _decorator("part", always=True, excludes=excludes)


def on_kick(
    *,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    if predicate is not None:
        return _decorator("kick", predicate=predicate, excludes=excludes)
    return _decorator("kick", always=True, excludes=excludes)


def on_nick(
    *,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    if predicate is not None:
        return _decorator("nick", predicate=predicate, excludes=excludes)
    return _decorator("nick", always=True, excludes=excludes)


def on_connect() -> Callable[[_Handler], _Handler]:
    return _decorator("connect", always=True)


def on_quit(
    *,
    predicate: Callable[[Event], bool] | None = None,
    excludes: Sequence[str] = (),
) -> Callable[[_Handler], _Handler]:
    if predicate is not None:
        return _decorator("quit", predicate=predicate, excludes=excludes)
    return _decorator("quit", always=True, excludes=excludes)


__all__ = [
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
