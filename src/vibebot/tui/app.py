"""Textual TUI — chat-first client of the bot's REST + WebSocket API."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import ClassVar

import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, TabbedContent, TabPane

from vibebot.tui.api import ApiClient
from vibebot.tui.commands import (
    CommandContext,
    CommandError,
    command_summary,
    dispatch,
    literal_message,
    parse_slash,
)
from vibebot.tui.state import (
    Line,
    UiState,
    buf_key,
    is_channel,
)
from vibebot.tui.widgets import (
    AdminTables,
    BufferLog,
    Composer,
    NetworkTree,
    RosterList,
    TopicBar,
)
from vibebot.tui.widgets.tree import TargetRef
from vibebot.tui.ws import WsFeed

log = logging.getLogger(__name__)


class VibebotTui(App):
    """Chat-first TUI with an admin pane for networks/modules/repos."""

    CSS = """
    Screen { layout: vertical; }
    #chat-row { height: 1fr; }
    NetworkTree {
        width: 28;
        border-right: vkey $accent-darken-1;
    }
    #buffer-col { width: 1fr; }
    #status-line { dock: bottom; height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS: ClassVar[list] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+n", "next_buffer", "Next"),
        Binding("ctrl+p", "prev_buffer", "Prev"),
        Binding("ctrl+r", "reload_buffer", "Reload"),
        Binding("escape", "close_query", "Close query", priority=True),
    ]

    def __init__(
        self,
        api_url: str,
        token: str,
        *,
        api: ApiClient | None = None,
        ws: WsFeed | None = None,
    ) -> None:
        super().__init__()
        self._api = api or ApiClient(api_url, token)
        self._ws = ws or WsFeed(api_url, token, on_status=self._set_ws_status)
        self._state = UiState()
        self._tree: NetworkTree | None = None
        self._buffer: BufferLog | None = None
        self._roster: RosterList | None = None
        self._topic: TopicBar | None = None
        self._composer: Composer | None = None
        self._admin: AdminTables | None = None
        self._ws_task: asyncio.Task | None = None
        self._admin_task: asyncio.Task | None = None
        self._ws_status = "offline"
        # Strong refs to fire-and-forget tasks scheduled from sync handlers,
        # so the event loop doesn't GC them mid-run (RUF006).
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="chat"):
            with TabPane("Chat", id="chat"), Horizontal(id="chat-row"):
                yield NetworkTree()
                with Vertical(id="buffer-col"):
                    yield TopicBar()
                    yield BufferLog()
                    yield Composer()
                yield RosterList()
            with TabPane("Admin", id="admin"):
                yield AdminTables()
        yield Footer()

    async def on_mount(self) -> None:
        self._tree = self.query_one(NetworkTree)
        self._buffer = self.query_one(BufferLog)
        self._roster = self.query_one(RosterList)
        self._topic = self.query_one(TopicBar)
        self._composer = self.query_one(Composer)
        self._admin = self.query_one(AdminTables)
        self._topic.display = False
        await self._hydrate_networks()
        self._ws.start()
        self._ws_task = asyncio.create_task(self._drain_events(), name="tui-drain")
        self._admin_task = asyncio.create_task(self._poll_admin(), name="tui-admin-poll")
        default = self._tree.first_selectable() if self._tree else None
        if default is not None:
            await self._activate(default)

    async def on_unmount(self) -> None:
        for task in (self._ws_task, self._admin_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._ws.stop()
        await self._api.close()

    # ------------------------------------------------------------------
    # hydration
    # ------------------------------------------------------------------
    async def _hydrate_networks(self) -> None:
        try:
            nets = await self._api.networks()
        except httpx.HTTPError as exc:
            self._system_line(f"networks: {exc}")
            return
        self._state.networks = {n["name"]: n for n in nets}
        for n in nets:
            if n.get("nickname"):
                self._state.own_nicks[n["name"]] = n["nickname"]
            self._state.declared.setdefault(n["name"], set()).update(n.get("channels", []) or [])
        await asyncio.gather(
            *(self._hydrate_channels(n["name"]) for n in nets if n.get("connected")),
            return_exceptions=True,
        )
        await asyncio.gather(
            *(self._hydrate_queries(n["name"]) for n in nets),
            return_exceptions=True,
        )
        self._rebuild_tree()

    async def _hydrate_channels(self, net: str) -> None:
        try:
            chans = await self._api.channels(net)
        except httpx.HTTPError:
            return
        for c in chans:
            name = c.get("name")
            if not name:
                continue
            self._state.declared.setdefault(net, set()).add(name)
            if c.get("topic"):
                self._state.topics[buf_key(net, name)] = {
                    "topic": c.get("topic"),
                    "by": c.get("by"),
                    "set_at": c.get("set_at"),
                }
            users = c.get("users")
            if isinstance(users, list):
                self._state.rosters[buf_key(net, name)] = users

    async def _hydrate_queries(self, net: str) -> None:
        try:
            peers = await self._api.queries(net)
        except httpx.HTTPError:
            return
        for q in peers:
            peer = q.get("peer")
            if peer:
                # Touch the buffer so the sidebar surfaces it.
                self._state.buffer(net, peer)

    async def _hydrate_history(self, net: str, target: str) -> None:
        key = buf_key(net, target)
        if target == "*" or key in self._state.hydrated:
            return
        self._state.hydrated.add(key)
        try:
            if is_channel(target):
                rows = await self._api.history(net, target)
            else:
                rows = await self._api.query_history(net, target)
        except httpx.HTTPError:
            self._state.hydrated.discard(key)
            return
        if not isinstance(rows, list) or not rows:
            return
        buf = self._state.buffer(net, target)
        buf.clear()
        for row in rows:
            buf.append(_row_to_line(row))

    # ------------------------------------------------------------------
    # event drain
    # ------------------------------------------------------------------
    async def _drain_events(self) -> None:
        while True:
            event = await self._ws.events.get()
            changed = self._state.apply_event(event)
            # Re-render active buffer if it was touched; always refresh tree badges.
            active_key = (
                (self._state.active_net, self._state.active_target)
                if self._state.active_net and self._state.active_target
                else None
            )
            if active_key and active_key in changed:
                self._render_active_buffer()
            # Roster refresh is gated on the event's channel matching the active
            # target, not on buffer changes: `names`/`roster` update memberships
            # without appending a line, so `changed` would be empty for them.
            if (
                event.kind in {"join", "part", "quit", "kick", "mode", "nick", "names", "roster"}
                and self._state.active_net == event.network
                and self._state.active_target
                and is_channel(self._state.active_target)
                and self._roster_event_matches_active(event)
            ):
                await self._refresh_roster(self._state.active_net, self._state.active_target)
            if event.kind == "topic" and self._state.active_net == event.network and self._state.active_target == event.payload.get("channel"):
                self._render_topic()
            if event.kind in {"connect", "host_hidden"}:
                try:
                    nets = await self._api.networks()
                    self._state.networks = {n["name"]: n for n in nets}
                except httpx.HTTPError:
                    pass
            self._rebuild_tree()

    # ------------------------------------------------------------------
    # admin polling
    # ------------------------------------------------------------------
    async def _poll_admin(self) -> None:
        while True:
            if self._current_tab_id() == "admin":
                try:
                    nets, mods, repos = await asyncio.gather(
                        self._api.networks(),
                        self._api.modules(),
                        self._api.repos(),
                    )
                    if self._admin is not None:
                        self._admin.fill_networks(nets)
                        self._admin.fill_modules(mods)
                        self._admin.fill_repos(repos)
                except httpx.HTTPError as exc:
                    log.debug("admin poll failed: %s", exc)
            await asyncio.sleep(5.0)

    def _current_tab_id(self) -> str | None:
        try:
            tabs = self.query_one(TabbedContent)
        except Exception:
            return None
        return tabs.active

    # ------------------------------------------------------------------
    # selection / rendering
    # ------------------------------------------------------------------
    async def _activate(self, ref: TargetRef) -> None:
        self._state.active_net = ref.network
        self._state.active_target = ref.target
        self._state.clear_unread(ref.network, ref.target)
        await self._hydrate_history(ref.network, ref.target)
        self._render_active_buffer()
        await self._refresh_roster(ref.network, ref.target)
        self._render_topic()
        if self._composer is not None:
            self._composer.configure_for(ref.network, ref.target)
            self._composer.focus()
        self._rebuild_tree()

    def _render_active_buffer(self) -> None:
        if self._buffer is None or self._state.active_net is None or self._state.active_target is None:
            return
        buf = self._state.buffer(self._state.active_net, self._state.active_target)
        self._buffer.show_lines(buf)

    def _render_topic(self) -> None:
        if self._topic is None:
            return
        net = self._state.active_net
        target = self._state.active_target
        info = self._state.topics.get(buf_key(net or "", target or ""))
        self._topic.show(net, target, info)

    def _roster_event_matches_active(self, event) -> bool:  # type: ignore[no-untyped-def]
        """True when a membership event should refresh the active roster.

        `quit`/`nick` carry no channel but can remove/rename anyone in the
        active channel — refresh unconditionally. Channel-scoped events only
        refresh when their channel is the active target.
        """
        if event.kind in {"quit", "nick"}:
            return True
        ch = event.payload.get("channel")
        return ch == self._state.active_target

    async def _refresh_roster(self, net: str, target: str) -> None:
        if self._roster is None:
            return
        if not net or not target or not is_channel(target):
            self._roster.render_users([])
            return
        try:
            users = await self._api.users(net, target)
        except httpx.HTTPError:
            users = self._state.rosters.get(buf_key(net, target), [])
        else:
            if isinstance(users, list):
                self._state.rosters[buf_key(net, target)] = users
        self._roster.render_users(users if isinstance(users, list) else [])

    def _rebuild_tree(self) -> None:
        if self._tree is not None:
            self._tree.populate(self._state)

    def _system_line(self, message: str) -> None:
        net = self._state.active_net
        target = self._state.active_target
        if not (net and target):
            return
        line = Line(kind="system", body=message)
        self._state.buffer(net, target).append(line)
        if self._buffer is not None:
            self._buffer.append_line(line)

    def _set_ws_status(self, status: str) -> None:
        self._ws_status = status
        self.call_from_thread(self._title_ws) if False else None
        # Textual won't let us update title from a non-message thread; update
        # the app sub-title the next time the UI ticks.

    def _title_ws(self) -> None:
        self.sub_title = f"ws: {self._ws_status}"

    # ------------------------------------------------------------------
    # widget message handlers
    # ------------------------------------------------------------------
    async def on_network_tree_selected(self, event: NetworkTree.Selected) -> None:
        # Highlighting the active node programmatically re-fires NodeSelected;
        # skip the no-op re-activate to keep the cycle finite.
        if self._state.active_net == event.ref.network and self._state.active_target == event.ref.target:
            return
        await self._activate(event.ref)

    async def on_input_submitted(self, event: Composer.Submitted) -> None:
        if event.input.id != "composer-input":
            return
        value = event.value
        event.input.value = ""
        if value:
            await self._handle_submission(value)

    async def _handle_submission(self, value: str) -> None:
        net = self._state.active_net
        target = self._state.active_target
        if not (net and target):
            return
        parsed = parse_slash(value)
        if parsed is None:
            # Plain text → strip literal escape, then send as message.
            body = literal_message(value)
            if target == "*":
                self._system_line("status buffer is slash-commands only")
                return
            try:
                await self._api.send(net, target, body)
            except httpx.HTTPError as exc:
                self._system_line(f"send: {exc}")
                return
            own = self._state.own_nicks.get(net) or "(me)"
            line = Line(kind="msg", nick=own, body=body, self_sent=True)
            self._state.buffer(net, target).append(line)
            self._state.record_pending_echo(net, target, "msg", body)
            if self._buffer is not None:
                self._buffer.append_line(line)
            return
        # Slash command
        if parsed.name == "help":
            self._print_help()
            return
        ctx = CommandContext(
            api=self._api,
            state=self._state,
            cmd=parsed,
            on_open_query=self._open_query,
            on_close_query=self._closed_query,
        )
        try:
            await dispatch(ctx)
        except CommandError as exc:
            self._system_line(str(exc))
        except httpx.HTTPError as exc:
            self._system_line(f"/{parsed.name}: {exc}")

    def _open_query(self, net: str, peer: str) -> None:
        self._state.buffer(net, peer)  # ensure exists
        self._spawn(self._activate(TargetRef(network=net, target=peer)))

    def _closed_query(self, net: str, peer: str) -> None:
        key = buf_key(net, peer)
        self._state.buffers.pop(key, None)
        self._state.hydrated.discard(key)
        self._state.pending_echoes.pop(key, None)
        self._state.unread.pop(key, None)
        if self._state.active_net == net and self._state.active_target == peer:
            self._state.active_target = "*"
            self._spawn(self._activate(TargetRef(network=net, target="*")))
        self._rebuild_tree()

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _print_help(self) -> None:
        if self._buffer is None:
            return
        for name, desc in command_summary():
            self._buffer.append_line(Line(kind="system", body=f"{name:<32} {desc}"))

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    async def action_refresh(self) -> None:
        await self._hydrate_networks()
        if self._state.active_net and self._state.active_target:
            await self._refresh_roster(self._state.active_net, self._state.active_target)
            self._render_topic()

    async def action_reload_buffer(self) -> None:
        net = self._state.active_net
        target = self._state.active_target
        if not (net and target):
            return
        self._state.hydrated.discard(buf_key(net, target))
        await self._hydrate_history(net, target)
        self._render_active_buffer()

    async def action_next_buffer(self) -> None:
        if self._tree is None:
            return
        cur = (self._state.active_net, self._state.active_target) if self._state.active_net and self._state.active_target else None
        ref = self._tree.neighbor(cur, step=1)
        if ref is not None:
            await self._activate(ref)

    async def action_prev_buffer(self) -> None:
        if self._tree is None:
            return
        cur = (self._state.active_net, self._state.active_target) if self._state.active_net and self._state.active_target else None
        ref = self._tree.neighbor(cur, step=-1)
        if ref is not None:
            await self._activate(ref)

    async def action_close_query(self) -> None:
        net = self._state.active_net
        target = self._state.active_target
        if not (net and target) or is_channel(target) or target == "*":
            return
        try:
            await self._api.close_query(net, target)
        except httpx.HTTPError as exc:
            self._system_line(f"/close: {exc}")
            return
        self._closed_query(net, target)


def _row_to_line(row: dict) -> Line:
    """Translate a server history row into a Line for rendering."""
    from datetime import datetime

    ts_raw = row.get("ts")
    if isinstance(ts_raw, str):
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            ts = datetime.now()
    else:
        ts = datetime.now()
    kind = row.get("kind") or "msg"
    return Line(
        kind=kind,
        ts=ts,
        nick=row.get("nick") or "",
        body=row.get("body") or "",
        event=row.get("event"),
        extras={k: v for k, v in row.items() if k not in {"ts", "kind", "nick", "body", "event"}},
        self_sent=bool(row.get("self")),
    )


def run_tui(api_url: str, token: str) -> None:
    VibebotTui(api_url=api_url, token=token).run()
