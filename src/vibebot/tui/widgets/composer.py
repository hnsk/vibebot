"""Message/command input row."""

from __future__ import annotations

from textual.widgets import Input


class Composer(Input):
    """A single-line Input. The app handles `Input.Submitted` directly."""

    DEFAULT_CSS = """
    Composer {
        dock: bottom;
        border: tall $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__(placeholder="select a channel…", id="composer-input")
        self.disabled = True

    def configure_for(self, net: str | None, target: str | None) -> None:
        if not (net and target):
            self.disabled = True
            self.placeholder = "select a channel…"
            return
        self.disabled = False
        self.placeholder = "message (or /help for commands)" if target == "*" else f"message {target}"
