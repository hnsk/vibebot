# vibebot

Modular, multi-server IRC bot in Python. Ships a REST + WebSocket API and two clients
on top of it: an HTMX web UI and a Textual terminal UI. Modules live in git repos and
can be loaded, unloaded, enabled, and disabled while the bot is running.

> **Heads up:** this project is 100% developed with [Claude Code](https://claude.com/claude-code) —
> it's a vibe-coded experiment. Works for me, YMMV. :)

## Features

- **Multi-network:** any number of IRC networks, each with its own nick, channels, and auth.
- **Multiple servers per network:** one marked `is_default`, the rest tried in order on failure.
- **Protocols:** `ircv3` (CAP negotiation, default) and `rfc1459` (legacy daemons).
- **Auth:** SASL (`PLAIN`, `EXTERNAL` / CERTFP, `SCRAM-SHA-256`, `SCRAM-SHA-1`), QuakeNet `Q`, generic NickServ, or anonymous.
- **Rate limiting:** per-network outgoing throttle (burst + period).
- **Modular:** one module per purpose, sourced from one or more git repositories; hot-reloadable.
- **Decorator-based triggers:** `@on_message`, `@on_ctcp`, `@on_mode`, etc. with compiled regex.
- **Per-module `Settings`:** Pydantic model exposed to the web UI for live config.
- **Per-module asyncio exception guard** so a buggy module does not crash the bot.
- **Persistent scheduled tasks** (APScheduler + SQLite job store — survives restarts).
- **ACLs:** restrict actions by `nick!ident@host` globs.
- **Storage:** SQLite via `aiosqlite` / SQLAlchemy async.
- **Per-module data dirs** at `<modules_data_dir>/<repo>/<name>/` for module-owned SQLite / caches.
- **Per-repo `requirements.txt`** auto-install (opt-in via `auto_install_requirements`).
- Packaged as a Python wheel and a Docker image.

## Interfaces

- **REST + WebSocket API** (FastAPI) — the source of truth; both clients below talk to it.
- **Web UI** (HTMX + Jinja2, no JS build step) — chat, modules, repos, ACLs, schedules.
- **Terminal UI** (Textual) — chat-first client with admin pane.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.toml config.toml
# edit config.toml: set networks, generate an api.tokens[0] value
python -c 'import secrets; print(secrets.token_urlsafe(32))'
vibebot serve --config config.toml
```

In another terminal:

```bash
export VIBEBOT_TOKEN=<token-from-config>
vibebot tui                          # terminal UI
# or open http://localhost:8080 in a browser
```

Requires Python **3.12+**.

## Configuration

Everything lives in `config.toml`. See [`config.example.toml`](config.example.toml) for the
authoritative, commented shape. Highlights:

- `[bot]` — database path, modules dir, per-module data dir, log level, `auto_install_requirements`, `pip_timeout_s`.
- `[api]` — bind host/port, bearer tokens list.
- `[[networks]]` — name, nick, channels, `protocol`, one or more `[[networks.servers]]`, optional `[networks.auth]` and `[networks.rate_limit]`.
- `[[repos]]` — module sources. Supports `url`, `branch`, `enabled`, and `subdir` (load modules from a subdirectory of the repo, used for optional modules).

Rewriting `config.toml` from the API / UI preserves comments and formatting (tomlkit).

## Modules

### Built-in (always loaded)

- `admin` — `!modules`, `!repos`, `!reload <repo> <name>` (admin ACL).
- `help` — `!help` lists loaded modules.
- `ping` — `!ping` with configurable reply (demo for `Settings`).

### Optional (first-party, under `optional-modules/`)

Load by pointing a `[[repos]]` at this repo with `subdir = "optional-modules"`:

- **remindme** — `!remindme <when> <message>`, e.g. `!remindme 5m buy milk`, `!remindme 1h30m standup`. Reminders persist across restarts.
- **urltitle** — detects URLs in messages, fetches pages, posts `og:title` (or `<title>`) back to the channel. 10-minute title cache, skips non-HTML content, regex-based ignore list (defaults to YouTube / Twitter / X).

Hot-reload after editing module code or config: `!reload <repo> <module>`.

### Adding a repo

```toml
[[repos]]
name = "my-modules"
url  = "https://github.com/you/my-modules.git"
branch = "main"
enabled = true
# subdir = "path/in/repo"   # optional
```

The bot clones on start; modules appear at `/modules` in the web UI and can be enabled from there or via the API.

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

Tests run against a local Ergo in Docker and an asyncio mock ircd — they never touch live networks.

## Project layout

```
src/vibebot/
  core/       bot, network connections, ACL, event bus, module guard
  modules/    loader, registry, base class, decorators, built-in modules
  scheduler/  APScheduler wiring with persistent job store
  storage/    SQLite models and DB helpers
  api/        FastAPI app, routes, WebSocket stream, auth
  web/        HTMX templates and static assets
  tui/        Textual terminal UI
optional-modules/
  remindme/   personal reminders
  urltitle/   auto URL title fetcher
```

## License

MIT
