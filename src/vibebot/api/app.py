"""FastAPI application factory for vibebot."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from vibebot.api import ws
from vibebot.api.routes import (
    acl,
    channels,
    module_settings,
    modules,
    networks,
    repos,
    send,
    settings,
)

if TYPE_CHECKING:
    from vibebot.core.bot import VibeBot

_HERE = Path(__file__).resolve().parent
_WEB = _HERE.parent / "web"


def build_app(bot: VibeBot) -> FastAPI:
    app = FastAPI(title="vibebot", version="0.1.0")
    app.state.bot = bot
    app.state.api_tokens = list(bot.config.api.tokens)
    app.state.templates = Jinja2Templates(directory=str(_WEB / "templates"))

    app.mount("/static", StaticFiles(directory=str(_WEB / "static")), name="static")

    app.include_router(networks.router)
    app.include_router(channels.router)
    app.include_router(channels.queries_router)
    app.include_router(send.router)
    app.include_router(modules.router)
    app.include_router(module_settings.router)
    app.include_router(repos.router)
    app.include_router(acl.router)
    app.include_router(settings.router)
    app.include_router(ws.router)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return app.state.templates.TemplateResponse(request, "index.html", {"bot": bot})

    return app
