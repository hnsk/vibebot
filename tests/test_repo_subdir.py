"""Tests for per-repo `subdir` support: validation, module_root_for, discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibebot.config import ApiConfig, BotConfig, Config, RepoConfig
from vibebot.core.bot import VibeBot
from vibebot.modules.registry import RepoRegistry, _validate_subdir
from vibebot.storage.models import Repo

MODULE_SRC = '''\
from vibebot.modules.base import Module

class M(Module):
    name = "sample"
    description = "fixture"
'''


# ---------- _validate_subdir ----------

@pytest.mark.parametrize("raw, want", [
    (None, None),
    ("", None),
    ("   ", None),
    ("/", None),
    ("modules", "modules"),
    ("mods/x", "mods/x"),
    ("/mods/", "mods"),
])
def test_validate_subdir_accepts(raw: str | None, want: str | None) -> None:
    assert _validate_subdir(raw) == want


@pytest.mark.parametrize("bad", ["..", "../x", "mods/..", "a/../b"])
def test_validate_subdir_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        _validate_subdir(bad)


# ---------- module_root_for ----------

def test_module_root_for_no_subdir(tmp_path: Path) -> None:
    from vibebot.storage.db import Database
    reg = RepoRegistry(Database(":memory:"), default_repos=[], modules_dir=str(tmp_path))
    repo = Repo(name="r", url="u", branch="main", subdir=None)
    assert reg.module_root_for(repo) == tmp_path / "r"


def test_module_root_for_with_subdir(tmp_path: Path) -> None:
    from vibebot.storage.db import Database
    reg = RepoRegistry(Database(":memory:"), default_repos=[], modules_dir=str(tmp_path))
    repo = Repo(name="r", url="u", branch="main", subdir="modules")
    assert reg.module_root_for(repo) == tmp_path / "r" / "modules"


def test_module_root_for_rejects_traversal(tmp_path: Path) -> None:
    from vibebot.storage.db import Database
    reg = RepoRegistry(Database(":memory:"), default_repos=[], modules_dir=str(tmp_path))
    repo = Repo(name="r", url="u", branch="main", subdir="../outside")
    with pytest.raises(ValueError):
        reg.module_root_for(repo)


# ---------- add_repo / update_repo / sync_from_config ----------

def _make_bot(tmp_path: Path) -> VibeBot:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "modules-data"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["tok"]),
    )
    return VibeBot(cfg)


@pytest.fixture()
async def bot(tmp_path: Path):
    b = _make_bot(tmp_path)
    await b.db.create_all()
    yield b
    await b.db.close()


async def test_add_repo_persists_subdir(bot: VibeBot) -> None:
    await bot.repos.add_repo("r1", "https://example/r.git", subdir="modules")
    repo = await bot.repos.get_repo("r1")
    assert repo is not None
    assert repo.subdir == "modules"


async def test_add_repo_rejects_bad_subdir(bot: VibeBot) -> None:
    with pytest.raises(ValueError):
        await bot.repos.add_repo("r1", "https://example/r.git", subdir="../bad")


async def test_update_repo_sets_and_clears_subdir(bot: VibeBot) -> None:
    await bot.repos.add_repo("r1", "https://example/r.git")
    await bot.repos.update_repo("r1", subdir="plugins")
    assert (await bot.repos.get_repo("r1")).subdir == "plugins"
    await bot.repos.update_repo("r1", clear_subdir=True)
    assert (await bot.repos.get_repo("r1")).subdir is None


async def test_sync_from_config_carries_subdir(tmp_path: Path) -> None:
    cfg = Config(
        bot=BotConfig(
            database=str(tmp_path / "bot.db"),
            modules_dir=str(tmp_path / "modules"),
            modules_data_dir=str(tmp_path / "modules-data"),
        ),
        api=ApiConfig(host="127.0.0.1", port=0, tokens=["tok"]),
        repos=[RepoConfig(name="main", url="https://example/main.git", subdir="modules")],
    )
    b = VibeBot(cfg)
    try:
        await b.db.create_all()
        await b.repos.sync_from_config()
        repo = await b.repos.get_repo("main")
        assert repo is not None
        assert repo.subdir == "modules"
    finally:
        await b.db.close()


# ---------- list_available under subdir ----------

async def test_list_available_discovers_modules_under_subdir(bot: VibeBot, tmp_path: Path) -> None:
    # Fake a cloned repo with modules under a "modules/" subdir.
    await bot.repos.add_repo("mono", "https://example/mono.git", subdir="modules")
    mod_root = tmp_path / "modules" / "mono" / "modules" / "sample"
    mod_root.mkdir(parents=True)
    (mod_root / "__init__.py").write_text(MODULE_SRC)

    # Also put a non-module file at the repo root that must NOT be picked up.
    (tmp_path / "modules" / "mono" / "README.md").write_text("hi")

    available = await bot.modules.list_available()
    keys = {(m["repo"], m["name"]) for m in available}
    assert ("mono", "sample") in keys
    # No spurious entries with repo at root
    assert not any(m["repo"] == "mono" and m["name"] == "README.md" for m in available)


async def test_list_available_root_layout_still_works(bot: VibeBot, tmp_path: Path) -> None:
    await bot.repos.add_repo("flat", "https://example/flat.git")  # no subdir
    mod_root = tmp_path / "modules" / "flat" / "sample"
    mod_root.mkdir(parents=True)
    (mod_root / "__init__.py").write_text(MODULE_SRC)

    available = await bot.modules.list_available()
    keys = {(m["repo"], m["name"]) for m in available}
    assert ("flat", "sample") in keys


async def test_list_available_skips_unpulled_repo(bot: VibeBot) -> None:
    # Repo row exists but no on-disk clone yet — must not error out.
    await bot.repos.add_repo("ghost", "https://example/ghost.git", subdir="modules")
    available = await bot.modules.list_available()
    assert all(m["repo"] != "ghost" for m in available)
