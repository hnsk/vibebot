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
