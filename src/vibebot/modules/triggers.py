"""Trigger model + global registry for module dispatch.

Modules declare triggers (via decorators on handler methods, or dynamically in
``on_load`` via ``Module.register_trigger``). The ``ModuleManager`` owns one
``TriggerRegistry`` and, on every event, asks it for the triggers whose match
spec accepts the event payload. Only matching handlers are invoked — modules
no longer see every message.

This module is intentionally import-light (stdlib + typing only) so
``decorators.py`` and ``base.py`` can pull it without creating cycles.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from vibebot.core.events import Event


TriggerKind = Literal[
    "message",
    "mode",
    "topic",
    "ctcp",
    "join",
    "part",
    "kick",
    "nick",
    "connect",
    "quit",
]

DISPATCH_KINDS: tuple[TriggerKind, ...] = (
    "message",
    "mode",
    "topic",
    "ctcp",
    "join",
    "part",
    "kick",
    "nick",
    "connect",
    "quit",
)


# -------------------- match specs --------------------


class TriggerMatch:
    """Base class for match specs. Subclasses implement ``matches``."""

    def matches(self, event: Event) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - for logs
        return self.__class__.__name__


@dataclass(frozen=True, slots=True)
class RegexMatch(TriggerMatch):
    pattern: re.Pattern[str]
    field: str = "message"

    def matches(self, event: Event) -> bool:
        value = event.get(self.field, "")
        if not isinstance(value, str):
            return False
        return self.pattern.search(value) is not None

    def describe(self) -> str:
        return f"regex={self.pattern.pattern!r}"


@dataclass(frozen=True, slots=True)
class StartswithMatch(TriggerMatch):
    prefix: str
    field: str = "message"
    case_sensitive: bool = True

    def matches(self, event: Event) -> bool:
        value = event.get(self.field, "")
        if not isinstance(value, str):
            return False
        if self.case_sensitive:
            return value.startswith(self.prefix)
        return value.lower().startswith(self.prefix.lower())

    def describe(self) -> str:
        return f"startswith={self.prefix!r}"


@dataclass(frozen=True, slots=True)
class ExactMatch(TriggerMatch):
    text: str
    field: str = "message"
    case_sensitive: bool = True
    strip: bool = True

    def matches(self, event: Event) -> bool:
        value = event.get(self.field, "")
        if not isinstance(value, str):
            return False
        candidate = value.strip() if self.strip else value
        if self.case_sensitive:
            return candidate == self.text
        return candidate.lower() == self.text.lower()

    def describe(self) -> str:
        return f"exact={self.text!r}"


@dataclass(frozen=True, slots=True)
class PredicateMatch(TriggerMatch):
    predicate: Callable[[Event], bool]

    def matches(self, event: Event) -> bool:
        try:
            return bool(self.predicate(event))
        except Exception:
            return False

    def describe(self) -> str:
        name = getattr(self.predicate, "__name__", repr(self.predicate))
        return f"predicate={name}"


@dataclass(frozen=True, slots=True)
class AlwaysMatch(TriggerMatch):
    def matches(self, event: Event) -> bool:
        return True

    def describe(self) -> str:
        return "always"


@dataclass(frozen=True, slots=True)
class CTCPMatch(TriggerMatch):
    type: str

    def matches(self, event: Event) -> bool:
        value = event.get("ctcp_type", "")
        if not isinstance(value, str):
            return False
        return value.upper() == self.type.upper()

    def describe(self) -> str:
        return f"ctcp={self.type!r}"


@dataclass(frozen=True, slots=True)
class ModeMatch(TriggerMatch):
    """Match `mode` events by letters and direction.

    Event payload must carry ``modes_parsed``: ``list[tuple[direction, letter, arg|None]]``.
    """

    letters: frozenset[str] | None = None
    direction: Literal["+", "-", "*"] = "*"

    def matches(self, event: Event) -> bool:
        parsed = event.get("modes_parsed", [])
        if not isinstance(parsed, list):
            return False
        for item in parsed:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            direction, letter = item[0], item[1]
            if self.direction != "*" and direction != self.direction:
                continue
            if self.letters is not None and letter not in self.letters:
                continue
            return True
        return False

    def describe(self) -> str:
        letters = "".join(sorted(self.letters)) if self.letters else "*"
        return f"mode {self.direction}{letters}"


# -------------------- descriptor (decorator output) --------------------


@dataclass(frozen=True, slots=True)
class TriggerDescriptor:
    """Unbound trigger declared by a decorator.

    Stored on the decorated function as ``_vb_triggers: list[TriggerDescriptor]``.
    The loader binds these to a module instance at load time.
    """

    kind: TriggerKind
    match: TriggerMatch
    excludes: tuple[re.Pattern[str], ...] = ()


# -------------------- runtime trigger --------------------


@dataclass(slots=True)
class Trigger:
    kind: TriggerKind
    match: TriggerMatch
    excludes: tuple[re.Pattern[str], ...]
    handler: Callable[[Event], Awaitable[None]]
    repo: str
    name: str
    source: Literal["decorator", "dynamic"]

    def matches(self, event: Event) -> bool:
        if not self.match.matches(event):
            return False
        if self.excludes:
            text = event.get("message", "")
            if isinstance(text, str):
                for pattern in self.excludes:
                    if pattern.search(text):
                        return False
        return True


# -------------------- registry --------------------


class TriggerRegistry:
    """Per-ModuleManager registry. Linear scan per kind — trigger counts are small."""

    def __init__(self) -> None:
        self._by_kind: dict[str, list[Trigger]] = {}
        self._enabled: dict[tuple[str, str], bool] = {}

    def register(self, trigger: Trigger) -> None:
        self._by_kind.setdefault(trigger.kind, []).append(trigger)
        self._enabled.setdefault((trigger.repo, trigger.name), True)

    def remove_for_module(self, repo: str, name: str) -> int:
        removed = 0
        for kind, triggers in self._by_kind.items():
            keep = [t for t in triggers if not (t.repo == repo and t.name == name)]
            removed += len(triggers) - len(keep)
            self._by_kind[kind] = keep
        self._enabled.pop((repo, name), None)
        return removed

    def set_enabled(self, repo: str, name: str, enabled: bool) -> None:
        self._enabled[(repo, name)] = enabled

    def is_enabled(self, repo: str, name: str) -> bool:
        return self._enabled.get((repo, name), True)

    def match(self, event: Event) -> Iterator[Trigger]:
        triggers = self._by_kind.get(event.kind, ())
        for trig in triggers:
            if not self._enabled.get((trig.repo, trig.name), True):
                continue
            if trig.matches(event):
                yield trig

    def triggers_for_module(self, repo: str, name: str) -> list[Trigger]:
        out: list[Trigger] = []
        for triggers in self._by_kind.values():
            out.extend(t for t in triggers if t.repo == repo and t.name == name)
        return out

    def total(self) -> int:
        return sum(len(v) for v in self._by_kind.values())


# -------------------- helpers --------------------


def compile_excludes(excludes: Sequence[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in excludes)


def build_match(
    *,
    regex: str | None = None,
    startswith: str | None = None,
    exact: str | None = None,
    predicate: Callable[[Event], bool] | None = None,
    ctcp_type: str | None = None,
    mode_letters: Sequence[str] | None = None,
    mode_direction: Literal["+", "-", "*"] = "*",
    always: bool = False,
    field: str = "message",
    case_sensitive: bool = True,
) -> TriggerMatch:
    """Build a TriggerMatch from the decorator/register_trigger kwargs.

    Exactly one of regex/startswith/exact/predicate/ctcp_type/mode/always must
    be set, except for kinds where always is the natural default (``always=True``).
    """
    specified: list[str] = []
    if regex is not None:
        specified.append("regex")
    if startswith is not None:
        specified.append("startswith")
    if exact is not None:
        specified.append("exact")
    if predicate is not None:
        specified.append("predicate")
    if ctcp_type is not None:
        specified.append("ctcp_type")
    if mode_letters is not None:
        specified.append("mode_letters")
    if always:
        specified.append("always")
    if len(specified) > 1:
        raise TypeError(
            f"trigger accepts at most one match spec, got: {', '.join(specified)}"
        )

    if regex is not None:
        flags = 0 if case_sensitive else re.IGNORECASE
        return RegexMatch(pattern=re.compile(regex, flags), field=field)
    if startswith is not None:
        return StartswithMatch(
            prefix=startswith, field=field, case_sensitive=case_sensitive
        )
    if exact is not None:
        return ExactMatch(text=exact, field=field, case_sensitive=case_sensitive)
    if predicate is not None:
        return PredicateMatch(predicate=predicate)
    if ctcp_type is not None:
        return CTCPMatch(type=ctcp_type)
    if mode_letters is not None or mode_direction != "*":
        letters_set = (
            frozenset(mode_letters) if mode_letters is not None else None
        )
        return ModeMatch(letters=letters_set, direction=mode_direction)
    return AlwaysMatch()


__all__ = [
    "DISPATCH_KINDS",
    "AlwaysMatch",
    "CTCPMatch",
    "ExactMatch",
    "ModeMatch",
    "PredicateMatch",
    "RegexMatch",
    "StartswithMatch",
    "Trigger",
    "TriggerDescriptor",
    "TriggerKind",
    "TriggerMatch",
    "TriggerRegistry",
    "build_match",
    "compile_excludes",
]
