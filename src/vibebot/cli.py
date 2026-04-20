"""vibebot command-line entry point."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import click

from vibebot import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="vibebot")
def main() -> None:
    """vibebot: modular multi-server IRC bot."""


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Path to config.toml.",
)
def serve(config_path: Path) -> None:
    """Run the bot (IRC networks + API + scheduler)."""
    from vibebot.config import load_config
    from vibebot.core.bot import VibeBot

    config = load_config(config_path)
    logging.basicConfig(level=config.bot.log_level)

    bot = VibeBot(config, config_path=config_path)
    asyncio.run(bot.run())


@main.command()
@click.option("--api-url", default="http://127.0.0.1:8080", show_default=True)
@click.option("--token", envvar="VIBEBOT_TOKEN", required=True, help="API token.")
def tui(api_url: str, token: str) -> None:
    """Launch the terminal UI."""
    from vibebot.tui.app import run_tui

    run_tui(api_url=api_url, token=token)


if __name__ == "__main__":
    main()
