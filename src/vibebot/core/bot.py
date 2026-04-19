"""VibeBot orchestrator: runs IRC networks, scheduler, API, and module lifecycle."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any

from vibebot.config import Config
from vibebot.core.acl import AclService
from vibebot.core.events import EventBus
from vibebot.core.network import NetworkConnection
from vibebot.storage.db import Database

log = logging.getLogger(__name__)


class VibeBot:
    """Top-level runtime. Owns DB, event bus, network connections, modules, API, scheduler."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bus = EventBus()
        self.db = Database(config.bot.database)
        self.acl = AclService(self.db)
        self.networks: dict[str, NetworkConnection] = {}

        # Late imports to keep module import-time graph small.
        from vibebot.modules.loader import ModuleManager
        from vibebot.modules.registry import RepoRegistry
        from vibebot.scheduler.jobs import SchedulerService

        self.repos = RepoRegistry(self.db, default_repos=config.repos, modules_dir=config.bot.modules_dir)
        self.modules = ModuleManager(bot=self)
        self.scheduler = SchedulerService(database_url=self.db.url)

        self._stop = asyncio.Event()
        self._api_task: asyncio.Task[None] | None = None
        self._api_server: Any = None

    async def run(self) -> None:
        await self.db.create_all()
        await self.repos.sync_from_config()
        await self.scheduler.start()
        await self._start_networks()
        await self.modules.reload_all()
        self._api_task = asyncio.create_task(self._run_api(), name="api")
        self._install_signal_handlers()
        log.info("vibebot running (networks=%d)", len(self.networks))
        try:
            await self._stop.wait()
        finally:
            await self._shutdown()

    async def _start_networks(self) -> None:
        for net_cfg in self.config.networks:
            conn = NetworkConnection(net_cfg, self.bus)
            self.networks[net_cfg.name] = conn
            await conn.start()

    async def _run_api(self) -> None:
        import uvicorn

        from vibebot.api.app import build_app

        app = build_app(self)
        cfg = uvicorn.Config(
            app,
            host=self.config.api.host,
            port=self.config.api.port,
            log_level=self.config.bot.log_level.lower(),
            lifespan="on",
            # VibeBot owns signal handling; otherwise uvicorn cancels its own
            # lifespan task on SIGINT and starlette logs a CancelledError trace.
            install_signal_handlers=False,
        )
        server = uvicorn.Server(cfg)
        self._api_server = server
        await server.serve()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

    async def shutdown(self) -> None:
        self._stop.set()

    async def _shutdown(self) -> None:
        log.info("vibebot shutting down")
        for conn in self.networks.values():
            await conn.stop()
        await self.scheduler.stop()
        if self._api_task is not None:
            if self._api_server is not None:
                self._api_server.should_exit = True
            else:
                self._api_task.cancel()
            with suppress(BaseException):
                await self._api_task
        await self.db.close()
