"""Chat scrollback widget."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from rich.text import Text
from textual.widgets import RichLog

from vibebot.tui.state import Line


def _fmt_time(ts: datetime) -> str:
    return ts.astimezone().strftime("%H:%M")


def _nick_style(nick: str) -> str:
    # Stable hash-indexed color so a user keeps the same tint between lines.
    palette = ["cyan", "magenta", "yellow", "green", "red", "blue", "bright_cyan", "bright_magenta"]
    if not nick:
        return palette[0]
    return palette[sum(ord(c) for c in nick) % len(palette)]


def format_line(line: Line) -> Text:
    """Render one Line as a Rich Text row (timestamp + marker + body)."""
    text = Text()
    text.append(_fmt_time(line.ts) + " ", style="dim")
    if line.kind in ("msg", "action", "notice"):
        nick = line.nick or "?"
        marker_style = _nick_style(nick)
        if line.kind == "action":
            text.append(f"* {nick} ", style=marker_style)
            text.append(line.body)
        elif line.kind == "notice":
            text.append(f"-{nick}- ", style="italic " + marker_style)
            text.append(line.body)
        else:
            prefix = "» " if line.self_sent else "  "
            text.append(f"{prefix}<{nick}> ", style=("bold " if line.self_sent else "") + marker_style)
            text.append(line.body)
    elif line.kind == "event":
        glyph = {
            "join": "→",
            "part": "←",
            "quit": "x",
            "kick": "✕",
            "mode": "±",
            "nick": "↺",
            "topic": "≡",
        }.get(line.event or "", "•")
        text.append(f"{glyph} ", style="dim cyan")
        text.append(line.body, style="dim")
    elif line.kind == "system":
        text.append("* ", style="bold yellow")
        text.append(line.body, style="yellow")
    elif line.kind == "whois":
        text.append("◉ ", style="bold green")
        text.append(line.body, style="green")
    else:
        text.append("· ")
        text.append(line.body)
    return text


class BufferLog(RichLog):
    """RichLog wrapper that renders Line objects."""

    DEFAULT_CSS = """
    BufferLog {
        background: $surface;
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="buffer-log", wrap=True, markup=False, auto_scroll=True, max_lines=1000)

    def show_lines(self, lines: Iterable[Line]) -> None:
        """Replace all rendered rows with `lines`."""
        self.clear()
        for line in lines:
            self.write(format_line(line))

    def append_line(self, line: Line) -> None:
        self.write(format_line(line))
