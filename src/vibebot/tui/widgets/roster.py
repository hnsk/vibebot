"""Right-rail user list for the active channel."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

_TIERS: tuple[tuple[str, str], ...] = (
    ("~", "owners"),
    ("&", "admins"),
    ("@", "ops"),
    ("%", "halfops"),
    ("+", "voiced"),
    ("", "users"),
)


class RosterList(Static):
    """Pretty user list grouped by prefix tier — mirrors the web UI roster."""

    DEFAULT_CSS = """
    RosterList {
        width: 24;
        padding: 0 1;
        border-left: vkey $accent-darken-1;
    }
    """

    def __init__(self) -> None:
        super().__init__(id="roster-list")

    def render_users(self, users: list[dict]) -> None:
        if not users:
            self.update(Text("no users", style="dim"))
            return
        buckets: dict[str, list[dict]] = {prefix: [] for prefix, _ in _TIERS}
        for u in users:
            prefix = u.get("prefix") or ""
            bucket = prefix if prefix in buckets else ""
            buckets[bucket].append(u)
        total = len(users)
        text = Text()
        text.append(f"users ({total})\n", style="bold")
        for prefix, label in _TIERS:
            rows = buckets.get(prefix) or []
            if not rows:
                continue
            rows.sort(key=lambda r: (r.get("nick") or "").lower())
            text.append(f"\n{label} ({len(rows)})\n", style="dim")
            for u in rows:
                nick = u.get("nick") or "?"
                text.append(prefix or " ", style="cyan")
                text.append(nick + "\n")
        self.update(text)
