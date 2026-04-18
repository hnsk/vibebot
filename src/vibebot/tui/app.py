"""Textual TUI that drives the bot via its HTTP API."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import httpx
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )

    async def get(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, json: dict | None = None) -> Any:
        r = await self._client.post(path, json=json or {})
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._client.aclose()


class VibebotTui(App):
    CSS = """
    Screen { layout: vertical; }
    #send-row Input { width: 1fr; }
    #status { color: #8a8f98; padding: 0 1; }
    DataTable { height: 1fr; }
    """
    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, api_url: str, token: str) -> None:
        super().__init__()
        self._api = ApiClient(api_url, token)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="networks"):
            with TabPane("Networks", id="networks"):
                yield DataTable(id="networks-table")
            with TabPane("Modules", id="modules"):
                yield DataTable(id="modules-table")
            with TabPane("Repos", id="repos"):
                yield DataTable(id="repos-table")
            with TabPane("Send", id="send"), Vertical():
                with Horizontal(id="send-row"):
                    yield Input(placeholder="network", id="send-network")
                    yield Input(placeholder="#channel or nick", id="send-target")
                    yield Input(placeholder="message", id="send-message")
                    yield Button("Send", id="send-button", variant="primary")
                yield Label("", id="send-status")
        yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        for tid, columns in (
            ("networks-table", ("name", "host:port", "tls", "connected", "channels")),
            ("modules-table", ("repo", "name", "enabled", "description")),
            ("repos-table", ("name", "url", "branch", "enabled")),
        ):
            self.query_one(f"#{tid}", DataTable).add_columns(*columns)
        await self.action_refresh()
        self.set_interval(5.0, self.action_refresh)

    async def action_refresh(self) -> None:
        try:
            networks, modules, repos = await asyncio.gather(
                self._api.get("/api/networks"),
                self._api.get("/api/modules"),
                self._api.get("/api/repos"),
            )
            self._fill(
                "networks-table",
                networks,
                lambda n: (n["name"], f"{n['host']}:{n['port']}", str(n["tls"]), str(n["connected"]), ",".join(n["channels"])),
            )
            self._fill(
                "modules-table",
                modules,
                lambda m: (m["repo"], m["name"], str(m["enabled"]), m.get("description", "")),
            )
            self._fill(
                "repos-table",
                repos,
                lambda r: (r["name"], r["url"], r["branch"], str(r["enabled"])),
            )
            self.query_one("#status", Static).update("ok")
        except Exception as exc:
            self.query_one("#status", Static).update(f"error: {exc}")

    def _fill(self, table_id: str, rows: list[dict], project) -> None:
        table = self.query_one(f"#{table_id}", DataTable)
        table.clear()
        for row in rows:
            table.add_row(*project(row))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "send-button":
            return
        network = self.query_one("#send-network", Input).value.strip()
        target = self.query_one("#send-target", Input).value.strip()
        message = self.query_one("#send-message", Input).value
        if not (network and target and message):
            self.query_one("#send-status", Label).update("Fill in network/target/message.")
            return
        try:
            await self._api.post(f"/api/networks/{network}/send", {"target": target, "message": message})
            self.query_one("#send-status", Label).update("sent")
            self.query_one("#send-message", Input).value = ""
        except Exception as exc:
            self.query_one("#send-status", Label).update(f"error: {exc}")

    async def on_unmount(self) -> None:
        await self._api.close()


def run_tui(api_url: str, token: str) -> None:
    VibebotTui(api_url=api_url, token=token).run()
