"""HTTP API smoke tests using FastAPI's TestClient."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vibebot.api.app import build_app
from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot


@pytest.fixture()
def bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["t0ken"]),
        networks=[],
        repos=[RepoConfig(name="sample", url="https://example.com/x.git")],
    )
    return VibeBot(cfg)


@pytest.fixture()
async def client(bot: VibeBot):
    await bot.db.create_all()
    await bot.repos.sync_from_config()
    app = build_app(bot)
    with TestClient(app) as c:
        yield c
    await bot.db.close()


async def test_api_requires_token(client: TestClient):
    r = client.get("/api/networks")
    assert r.status_code == 401


async def test_api_lists_networks(client: TestClient):
    r = client.get("/api/networks", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200
    assert r.json() == []


async def test_api_lists_repos_from_config(client: TestClient):
    r = client.get("/api/repos", headers={"Authorization": "Bearer t0ken"})
    assert r.status_code == 200
    repos = r.json()
    assert any(repo["name"] == "sample" for repo in repos)


async def test_api_adds_acl_rule(client: TestClient):
    headers = {"Authorization": "Bearer t0ken"}
    r = client.post(
        "/api/acl",
        json={"mask": "alice!*@*", "permission": "admin", "note": "test"},
        headers=headers,
    )
    assert r.status_code == 200
    rule_id = r.json()["id"]
    listed = client.get("/api/acl", headers=headers).json()
    assert any(rule["id"] == rule_id for rule in listed)


async def test_api_deletes_acl_rule(client: TestClient):
    headers = {"Authorization": "Bearer t0ken"}
    add = client.post(
        "/api/acl",
        json={"mask": "bob!*@*", "permission": "admin"},
        headers=headers,
    )
    rule_id = add.json()["id"]
    r = client.delete(f"/api/acl/{rule_id}", headers=headers)
    assert r.status_code == 200
    missing = client.delete(f"/api/acl/{rule_id}", headers=headers)
    assert missing.status_code == 404


async def test_api_channels_unknown_network(client: TestClient):
    r = client.get(
        "/api/networks/nope/channels",
        headers={"Authorization": "Bearer t0ken"},
    )
    assert r.status_code == 404


async def test_api_history_unknown_network(client: TestClient):
    r = client.get(
        "/api/networks/nope/channels/%23x/history",
        headers={"Authorization": "Bearer t0ken"},
    )
    assert r.status_code == 404


async def test_api_history_empty_buffer(bot: VibeBot, client: TestClient):
    # With no live networks, bot.networks is empty. Inject a stub so the
    # network-existence check passes, then query an empty channel history.
    class _StubConn:
        name = "stub"
        class _c: pass
        client = _c()
    bot.networks["stub"] = _StubConn()
    r = client.get(
        "/api/networks/stub/channels/%23room/history",
        headers={"Authorization": "Bearer t0ken"},
    )
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.parametrize(
    "path,body",
    [
        ("/op",      {"channel": "#x", "nick": "alice"}),
        ("/deop",    {"channel": "#x", "nick": "alice"}),
        ("/voice",   {"channel": "#x", "nick": "alice"}),
        ("/devoice", {"channel": "#x", "nick": "alice"}),
        ("/kick",    {"channel": "#x", "nick": "alice", "reason": "bye"}),
        ("/ban",     {"channel": "#x", "nick": "alice"}),
        ("/kickban", {"channel": "#x", "nick": "alice", "reason": "bye"}),
        ("/mode",    {"channel": "#x", "flags": "+m", "args": []}),
        ("/topic",   {"channel": "#x", "topic": "hi"}),
        ("/nick",    {"nick": "newnick"}),
        ("/whois",   {"nick": "alice"}),
        ("/raw",     {"line": "PING :x"}),
    ],
)
async def test_api_ops_endpoints_unknown_network(client: TestClient, path: str, body: dict):
    r = client.post(
        f"/api/networks/nope{path}",
        json=body,
        headers={"Authorization": "Bearer t0ken"},
    )
    assert r.status_code == 404, r.text


async def test_api_ops_endpoints_require_token(client: TestClient):
    r = client.post("/api/networks/nope/op", json={"channel": "#x", "nick": "a"})
    assert r.status_code == 401


async def test_index_html_served(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "vibebot" in body
    assert 'data-view="acl"' in body
    assert 'data-view="chat"' in body
    assert 'id="network-tree"' in body
    assert 'id="token-dialog"' in body


async def test_static_app_js_served(client: TestClient):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert "vibebot_token" in r.text
