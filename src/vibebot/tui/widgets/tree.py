"""Network / channel / query sidebar."""

from __future__ import annotations

from dataclasses import dataclass

from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from vibebot.tui.state import UiState, is_channel


@dataclass
class TargetRef:
    """Payload attached to every selectable tree node."""

    network: str
    target: str  # "*" for status bucket, "#chan" for a channel, "peer" for a query


class NetworkTree(Tree[TargetRef]):
    """Left rail: networks → {status, channels, queries}."""

    class Selected(Message):
        def __init__(self, ref: TargetRef) -> None:
            self.ref = ref
            super().__init__()

    def __init__(self) -> None:
        super().__init__("networks", id="network-tree")
        self.show_root = False
        self.guide_depth = 2
        self._suppress_selected = False

    def populate(self, state: UiState) -> None:
        """Rebuild the tree from UiState. Preserves active highlight."""
        self.clear()
        for name in sorted(state.networks, key=str.lower):
            net = state.networks[name]
            connected = " ●" if net.get("connected") else " ○"
            net_node = self.root.add(f"{name}{connected}", expand=True)
            status_label = self._badge("* status", state, name, "*")
            net_node.add_leaf(status_label, data=TargetRef(network=name, target="*"))
            chans, queries = state.targets_for(name)
            for ch in chans:
                net_node.add_leaf(self._badge(ch, state, name, ch), data=TargetRef(network=name, target=ch))
            if queries:
                q_head = net_node.add("queries", expand=True)
                for peer in queries:
                    q_head.add_leaf(
                        self._badge(f"@ {peer}", state, name, peer),
                        data=TargetRef(network=name, target=peer),
                    )
        self._highlight_active(state)

    def _badge(self, base: str, state: UiState, net: str, target: str) -> str:
        unread = state.unread.get((net, target), 0)
        if unread:
            return f"{base} ({unread})"
        return base

    def _highlight_active(self, state: UiState) -> None:
        target = state.active_target
        net = state.active_net
        if not (net and target):
            return
        for node in _walk(self.root):
            data = node.data
            if isinstance(data, TargetRef) and data.network == net and data.target == target:
                # select_node posts NodeSelected, which our handler translates
                # to Selected → _activate. Suppress that re-fire when the
                # highlight is purely visual bookkeeping.
                self._suppress_selected = True
                try:
                    self.select_node(node)
                    self.scroll_to_node(node)
                finally:
                    self._suppress_selected = False
                return

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if self._suppress_selected:
            return
        ref = event.node.data
        if isinstance(ref, TargetRef):
            self.post_message(self.Selected(ref))

    def first_selectable(self) -> TargetRef | None:
        for node in _walk(self.root):
            if isinstance(node.data, TargetRef) and is_channel(node.data.target):
                return node.data
        for node in _walk(self.root):
            if isinstance(node.data, TargetRef):
                return node.data
        return None

    def neighbor(self, current: tuple[str, str] | None, *, step: int) -> TargetRef | None:
        refs = [node.data for node in _walk(self.root) if isinstance(node.data, TargetRef)]
        if not refs:
            return None
        if current is None:
            return refs[0]
        try:
            idx = next(i for i, r in enumerate(refs) if (r.network, r.target) == current)
        except StopIteration:
            return refs[0]
        return refs[(idx + step) % len(refs)]


def _walk(node: TreeNode) -> list[TreeNode]:
    out: list[TreeNode] = []
    stack = list(node.children)
    while stack:
        n = stack.pop(0)
        out.append(n)
        stack.extend(n.children)
    return out
