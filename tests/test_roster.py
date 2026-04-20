"""Unit tests for vibebot.core.roster.ChannelRoster."""

from __future__ import annotations

from vibebot.core.roster import ChannelRoster


def test_upsert_and_get_user():
    r = ChannelRoster()
    u = r.upsert_user("n", "#c", "Alice", ident="a", host="h.example")
    assert u.nick == "Alice"
    assert u.ident == "a"
    assert u.host == "h.example"
    got = r.get_user("n", "#c", "alice")  # case-insensitive lookup
    assert got is u


def test_upsert_preserves_mask_on_partial_update():
    r = ChannelRoster()
    r.upsert_user("n", "#c", "bob", ident="b", host="old.host")
    # Partial upsert (e.g. JOIN with no meta) must not clobber known mask fields.
    r.upsert_user("n", "#c", "bob")
    u = r.get_user("n", "#c", "bob")
    assert u.ident == "b"
    assert u.host == "old.host"


def test_remove_user_scoped_to_channel():
    r = ChannelRoster()
    r.upsert_user("n", "#a", "alice")
    r.upsert_user("n", "#b", "alice")
    r.remove_user("n", "#a", "alice")
    assert r.get_user("n", "#a", "alice") is None
    assert r.get_user("n", "#b", "alice") is not None


def test_remove_user_all_reports_channels():
    r = ChannelRoster()
    r.upsert_user("n", "#a", "alice")
    r.upsert_user("n", "#b", "alice")
    r.upsert_user("n", "#b", "bob")
    chans = r.remove_user_all("n", "alice")
    assert set(chans) == {"#a", "#b"}
    assert r.get_user("n", "#b", "bob") is not None
    assert r.get_user("n", "#b", "alice") is None


def test_rename_user_updates_every_channel():
    r = ChannelRoster()
    r.upsert_user("n", "#a", "alice", ident="a", host="h")
    r.upsert_user("n", "#b", "alice", ident="a", host="h")
    chans = r.rename_user("n", "alice", "alice_")
    assert set(chans) == {"#a", "#b"}
    for ch in ("#a", "#b"):
        assert r.get_user("n", ch, "alice") is None
        u = r.get_user("n", ch, "alice_")
        assert u is not None
        assert u.ident == "a"
        assert u.host == "h"


def test_rename_user_updates_own_nick():
    r = ChannelRoster()
    r.set_own_nick("n", "vibebot")
    r.upsert_user("n", "#c", "vibebot")
    r.rename_user("n", "vibebot", "vibebot_")
    assert r.own_nick("n") == "vibebot_"


def test_reset_channel_wipes_membership():
    r = ChannelRoster()
    r.upsert_user("n", "#c", "alice")
    r.upsert_user("n", "#c", "bob")
    r.reset_channel("n", "#c")
    assert r.users("n", "#c") == []


def test_drop_channel_removes_key():
    r = ChannelRoster()
    r.upsert_user("n", "#c", "alice")
    r.drop_channel("n", "#c")
    assert "#c" not in r.channels("n")


def test_channels_for_lists_shared_channels():
    r = ChannelRoster()
    r.upsert_user("n", "#a", "alice")
    r.upsert_user("n", "#b", "alice")
    r.upsert_user("n", "#b", "bob")
    assert sorted(r.channels_for("n", "alice")) == ["#a", "#b"]
    assert r.channels_for("n", "bob") == ["#b"]


class _FakeClient:
    """Minimal pydle stand-in exposing the `channels` shape the roster reads."""

    def __init__(self, channel_modes):
        self.channels = {"#c": {"modes": channel_modes}}


def test_sync_modes_from_client_mirrors_privilege_modes():
    r = ChannelRoster()
    r.upsert_user("n", "#c", "alice")
    r.upsert_user("n", "#c", "bob")
    r.upsert_user("n", "#c", "charlie")

    client = _FakeClient({"o": {"alice"}, "v": {"bob", "alice"}, "t": None})
    r.sync_modes_from_client("n", "#c", client)

    assert r.get_user("n", "#c", "alice").modes == {"o", "v"}
    assert r.get_user("n", "#c", "bob").modes == {"v"}
    assert r.get_user("n", "#c", "charlie").modes == set()


def test_sync_modes_clears_revoked_modes():
    r = ChannelRoster()
    r.upsert_user("n", "#c", "alice", modes={"o"})
    client = _FakeClient({"o": set(), "v": set()})
    r.sync_modes_from_client("n", "#c", client)
    assert r.get_user("n", "#c", "alice").modes == set()


def test_clear_network_drops_everything():
    r = ChannelRoster()
    r.set_own_nick("n", "vibebot")
    r.upsert_user("n", "#c", "alice")
    r.clear_network("n")
    assert r.channels("n") == []
    assert r.own_nick("n") is None
