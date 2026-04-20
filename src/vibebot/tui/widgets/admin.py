"""Read-only admin panel — Networks / Modules / Repos tables."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, TabbedContent, TabPane


class AdminTables(Container):
    """Three DataTables behind tabs — carried over from the original TUI stub."""

    DEFAULT_CSS = """
    AdminTables DataTable { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="admin-networks"):
            with TabPane("Networks", id="admin-networks"):
                yield DataTable(id="admin-networks-table")
            with TabPane("Modules", id="admin-modules"):
                yield DataTable(id="admin-modules-table")
            with TabPane("Repos", id="admin-repos"):
                yield DataTable(id="admin-repos-table")

    def on_mount(self) -> None:
        for tid, columns in (
            ("admin-networks-table", ("name", "host:port", "tls", "connected", "channels")),
            ("admin-modules-table", ("repo", "name", "enabled", "description")),
            ("admin-repos-table", ("name", "url", "branch", "enabled")),
        ):
            self.query_one(f"#{tid}", DataTable).add_columns(*columns)

    def fill_networks(self, rows: list[dict]) -> None:
        table = self.query_one("#admin-networks-table", DataTable)
        table.clear()
        for n in rows:
            table.add_row(
                n.get("name", ""),
                f"{n.get('host', '')}:{n.get('port', '')}",
                str(n.get("tls", "")),
                str(n.get("connected", "")),
                ",".join(n.get("channels", []) or []),
            )

    def fill_modules(self, rows: list[dict]) -> None:
        table = self.query_one("#admin-modules-table", DataTable)
        table.clear()
        for m in rows:
            table.add_row(
                m.get("repo", ""),
                m.get("name", ""),
                str(m.get("enabled", "")),
                m.get("description", "") or "",
            )

    def fill_repos(self, rows: list[dict]) -> None:
        table = self.query_one("#admin-repos-table", DataTable)
        table.clear()
        for r in rows:
            table.add_row(
                r.get("name", ""),
                r.get("url", ""),
                r.get("branch", ""),
                str(r.get("enabled", "")),
            )
