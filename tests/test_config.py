"""Configuration loader tests."""

from __future__ import annotations

from pathlib import Path

from vibebot.config import load_config


def test_load_example(tmp_path: Path):
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(example)
    assert cfg.bot.database
    assert cfg.api.port == 8080
    assert cfg.networks[0].nick == "vibebot"
