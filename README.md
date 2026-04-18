# vibebot

Modular, multi-server IRC bot written in Python. Ships terminal, web, and REST/WebSocket API
interfaces (terminal and web are both clients of the API). Modules are fetched from git
repositories and can be loaded, unloaded, enabled, and disabled while the bot is running.

## Features

- Multi-network: connect to any number of IRC servers, each with its own nick/realname/etc.
- Modular: one module per purpose, sourced from one or more git (GitHub) repositories.
- Hot-reloadable modules without restarting the bot.
- Per-module asyncio exception guard so a buggy module does not crash the bot.
- Per-module scheduled tasks (APScheduler with SQLite job store — tasks survive restarts).
- ACLs: restrict actions by `nick!ident@host` globs.
- Storage: SQLite via `aiosqlite`.
- Interfaces:
  - REST + WebSocket API (FastAPI).
  - Web UI (HTMX — no JS build step).
  - Terminal UI (Textual).
- Packaged as a Python wheel and a Docker image.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml
# edit config.toml
vibebot serve --config config.toml
```

In another terminal:

```bash
vibebot tui            # terminal UI
# or open http://localhost:8080 in a browser
```

## Docker

```bash
docker compose up
```

## Development

```bash
pytest
ruff check .
mypy src
```

## Project layout

```
src/vibebot/
  core/       bot, network connections, ACL, event bus, module guard
  modules/    loader, registry, base class, built-in modules
  scheduler/  APScheduler wiring with persistent job store
  storage/    SQLite models and DB helpers
  api/        FastAPI app, routes, WebSocket stream, auth
  web/        HTMX templates and static assets
  tui/        Textual terminal UI
```

See `requirements.md` for the full specification.

## License

MIT
