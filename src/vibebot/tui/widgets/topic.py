"""Topic bar above the chat buffer."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class TopicBar(Static):
    DEFAULT_CSS = """
    TopicBar {
        height: auto;
        padding: 0 1;
        color: $text;
        background: $panel;
    }
    """

    def __init__(self) -> None:
        super().__init__("", id="topic-bar")

    def show(self, net: str | None, target: str | None, topic_info: dict | None) -> None:
        if not net or not target or target == "*" or not topic_info or not topic_info.get("topic"):
            self.update("")
            self.display = False
            return
        self.display = True
        text = Text()
        text.append(f"{net}/{target}", style="bold")
        text.append("  ")
        text.append(topic_info.get("topic") or "")
        by = topic_info.get("by")
        if by:
            text.append(f"  (by {by})", style="dim")
        self.update(text)
