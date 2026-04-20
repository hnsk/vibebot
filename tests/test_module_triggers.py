"""Unit tests for the trigger model + decorators."""

from __future__ import annotations

import re

import pytest

from vibebot.core.events import Event
from vibebot.modules.decorators import (
    on_ctcp,
    on_message,
    on_mode,
    on_topic,
)
from vibebot.modules.triggers import (
    AlwaysMatch,
    CTCPMatch,
    ExactMatch,
    ModeMatch,
    PredicateMatch,
    RegexMatch,
    StartswithMatch,
    Trigger,
    TriggerDescriptor,
    TriggerRegistry,
    build_match,
    compile_excludes,
)


def _event(kind: str = "message", **payload) -> Event:
    return Event(kind=kind, network="n", payload=payload)


# -------------------- match specs --------------------


def test_regex_match_hits() -> None:
    m = RegexMatch(pattern=re.compile(r"https?://\S+"))
    assert m.matches(_event(message="see https://example.com here"))


def test_regex_match_non_string_field() -> None:
    m = RegexMatch(pattern=re.compile(r"x"), field="other")
    assert m.matches(_event(other="x")) is True
    assert m.matches(_event(other=123)) is False


def test_startswith_case_insensitive() -> None:
    m = StartswithMatch(prefix="!HELP", case_sensitive=False)
    assert m.matches(_event(message="!help me"))


def test_exact_match_strips() -> None:
    m = ExactMatch(text="!ping")
    assert m.matches(_event(message="  !ping  "))
    assert not m.matches(_event(message="!ping extra"))


def test_predicate_match_handles_exceptions() -> None:
    def raiser(event: Event) -> bool:
        raise RuntimeError("boom")

    m = PredicateMatch(predicate=raiser)
    assert m.matches(_event(message="x")) is False


def test_always_match() -> None:
    assert AlwaysMatch().matches(_event(kind="connect"))


def test_ctcp_match_case_insensitive_type() -> None:
    m = CTCPMatch(type="VERSION")
    assert m.matches(_event(kind="ctcp", ctcp_type="version"))
    assert not m.matches(_event(kind="ctcp", ctcp_type="PING"))


def test_mode_match_letters_and_direction() -> None:
    m = ModeMatch(letters=frozenset({"o"}), direction="+")
    hit = _event(kind="mode", modes_parsed=[("+", "o", "alice")])
    miss_dir = _event(kind="mode", modes_parsed=[("-", "o", "alice")])
    miss_letter = _event(kind="mode", modes_parsed=[("+", "v", "alice")])
    assert m.matches(hit)
    assert not m.matches(miss_dir)
    assert not m.matches(miss_letter)


def test_mode_match_any_direction() -> None:
    m = ModeMatch(letters=frozenset({"o"}), direction="*")
    assert m.matches(_event(kind="mode", modes_parsed=[("-", "o", "alice")]))


def test_mode_match_missing_payload() -> None:
    m = ModeMatch(letters=frozenset({"o"}), direction="+")
    assert not m.matches(_event(kind="mode"))


# -------------------- trigger + excludes --------------------


async def _noop(event: Event) -> None:
    return None


def _trigger(match, excludes=()):
    return Trigger(
        kind="message",
        match=match,
        excludes=compile_excludes(excludes),
        handler=_noop,
        repo="r",
        name="m",
        source="decorator",
    )


def test_trigger_excludes_short_circuit_positive_match() -> None:
    trig = _trigger(
        RegexMatch(pattern=re.compile(r"https?://\S+")),
        excludes=[r"youtube\.com", r"youtu\.be"],
    )
    assert trig.matches(_event(message="look at https://example.com"))
    assert not trig.matches(
        _event(message="look at https://youtube.com/watch?v=x")
    )


# -------------------- registry --------------------


def test_registry_match_only_yields_enabled() -> None:
    reg = TriggerRegistry()
    t1 = _trigger(ExactMatch(text="!a"))
    t2 = Trigger(
        kind="message",
        match=ExactMatch(text="!b"),
        excludes=(),
        handler=_noop,
        repo="r",
        name="other",
        source="decorator",
    )
    reg.register(t1)
    reg.register(t2)

    hits = list(reg.match(_event(message="!a")))
    assert hits == [t1]

    reg.set_enabled("r", "m", False)
    assert list(reg.match(_event(message="!a"))) == []

    reg.set_enabled("r", "m", True)
    assert list(reg.match(_event(message="!a"))) == [t1]


def test_registry_remove_for_module() -> None:
    reg = TriggerRegistry()
    reg.register(_trigger(ExactMatch(text="!a")))
    reg.register(_trigger(ExactMatch(text="!b")))
    assert reg.total() == 2
    removed = reg.remove_for_module("r", "m")
    assert removed == 2
    assert reg.total() == 0


# -------------------- decorators --------------------


def test_on_message_requires_match_kind() -> None:
    with pytest.raises(TypeError):
        on_message()


def test_on_message_stacks_two_descriptors() -> None:
    @on_message(exact="!a")
    @on_message(exact="!b")
    async def handler(self, event):
        return None

    descriptors = handler._vb_triggers  # type: ignore[attr-defined]
    assert len(descriptors) == 2
    texts = {d.match.text for d in descriptors}  # type: ignore[attr-defined]
    assert texts == {"!a", "!b"}


def test_on_message_rejects_multiple_match_kinds() -> None:
    with pytest.raises(TypeError):

        @on_message(regex=r"x", startswith="y")
        async def handler(self, event):
            return None


def test_on_ctcp_requires_type() -> None:
    with pytest.raises(TypeError):
        on_ctcp(type="")


def test_on_mode_default_any() -> None:
    @on_mode()
    async def handler(self, event):
        return None

    (desc,) = handler._vb_triggers  # type: ignore[attr-defined]
    assert isinstance(desc.match, ModeMatch)
    assert desc.match.direction == "*"
    assert desc.match.letters is None


def test_on_topic_default_always() -> None:
    @on_topic()
    async def handler(self, event):
        return None

    (desc,) = handler._vb_triggers  # type: ignore[attr-defined]
    assert isinstance(desc.match, AlwaysMatch)


def test_descriptor_kind_passed_through() -> None:
    @on_message(exact="!x")
    async def handler(self, event):
        return None

    (desc,) = handler._vb_triggers  # type: ignore[attr-defined]
    assert isinstance(desc, TriggerDescriptor)
    assert desc.kind == "message"


def test_build_match_rejects_multiple_specs() -> None:
    with pytest.raises(TypeError):
        build_match(regex=r"a", startswith="b")


def test_build_match_default_always() -> None:
    assert isinstance(build_match(), AlwaysMatch)
