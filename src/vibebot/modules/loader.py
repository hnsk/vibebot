"""Module loader: discover, import, enable/disable, hot-reload bot modules."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy import select

from vibebot.core.events import Event
from vibebot.core.guard import guard_callback, spawn_guarded
from vibebot.modules.base import Module
from vibebot.modules.triggers import (
    DISPATCH_KINDS,
    Trigger,
    TriggerRegistry,
)
from vibebot.storage.models import ModuleState

if TYPE_CHECKING:
    from vibebot.core.bot import VibeBot

log = logging.getLogger(__name__)

BUILTIN_PACKAGE = "vibebot.modules.builtin"
BUILTIN_REPO = "__builtin__"


@dataclass
class LoadedModule:
    repo: str
    name: str
    instance: Module
    python_module: ModuleType
    job_ids: list[str] = field(default_factory=list)
    enabled: bool = True


class ModuleManager:
    """Owns the set of loaded modules and their lifecycle."""

    def __init__(self, bot: VibeBot) -> None:
        self.bot = bot
        self._loaded: dict[tuple[str, str], LoadedModule] = {}
        self._registry = TriggerRegistry()
        for kind in DISPATCH_KINDS:
            bot.bus.subscribe(kind, self._dispatch)

    # ---------- lifecycle ----------

    async def reload_all(self) -> None:
        """Load built-ins, then every enabled module recorded in the DB."""
        await self._load_builtins()
        async with self.bot.db.session() as s:
            states = list((await s.execute(select(ModuleState).where(ModuleState.loaded.is_(True)))).scalars())
        for state in states:
            if state.repo_name == BUILTIN_REPO:
                continue
            try:
                await self.load(state.repo_name, state.module_name)
                if not state.enabled:
                    await self.disable(state.repo_name, state.module_name)
            except Exception:
                log.exception("Failed to auto-load module %s/%s", state.repo_name, state.module_name)

    async def _load_builtins(self) -> None:
        package = importlib.import_module(BUILTIN_PACKAGE)
        package_path = Path(package.__file__).parent if package.__file__ else None
        if package_path is None:
            return
        for entry in sorted(package_path.iterdir()):
            if entry.name.startswith("_") or not entry.name.endswith(".py"):
                continue
            module_name = entry.stem
            try:
                await self._load_python_path(BUILTIN_REPO, module_name, f"{BUILTIN_PACKAGE}.{module_name}")
            except Exception:
                log.exception("Failed to load built-in module %s", module_name)

    async def load(self, repo: str, name: str) -> LoadedModule:
        if (repo, name) in self._loaded:
            raise ValueError(f"Module {repo}/{name} already loaded")
        if repo == BUILTIN_REPO:
            qualified = f"{BUILTIN_PACKAGE}.{name}"
            return await self._load_python_path(repo, name, qualified)
        path = await self.bot.repos.clone_or_pull(repo)
        module_dir = path / name
        if not module_dir.exists():
            raise FileNotFoundError(f"No module {name!r} in repo {repo!r} at {module_dir}")
        qualified = f"vibebot_module.{repo}.{name}"
        entry = module_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(qualified, entry)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {qualified} from {entry}")
        python_module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = python_module
        spec.loader.exec_module(python_module)
        return await self._finalize_load(repo, name, python_module)

    async def _load_python_path(self, repo: str, name: str, qualified: str) -> LoadedModule:
        python_module = importlib.import_module(qualified)
        python_module = importlib.reload(python_module)
        return await self._finalize_load(repo, name, python_module)

    async def _finalize_load(self, repo: str, name: str, python_module: ModuleType) -> LoadedModule:
        cls = _find_module_class(python_module)
        if cls is None:
            raise ImportError(f"Module {repo}/{name} has no Module subclass")
        stored = await self._load_config(repo, name)
        instance = cls(self.bot, config=stored)
        instance.name = instance.name or name
        instance._repo = repo
        instance._name = name

        settings_error: str | None = None
        if cls.Settings is not None:
            try:
                instance.settings = cls.Settings(**stored)
            except ValidationError as exc:
                settings_error = _format_validation_error(exc)
                log.error("Module %s/%s invalid settings: %s", repo, name, settings_error)
            else:
                if not stored:
                    # Persist resolved defaults so the UI has concrete values to edit.
                    from vibebot.modules.settings import dump_for_storage
                    defaults = dump_for_storage(instance.settings)
                    await self._upsert_state(
                        repo, name, loaded=True, enabled=True, config_json=defaults
                    )

        if settings_error is not None:
            loaded = LoadedModule(
                repo=repo, name=name, instance=instance, python_module=python_module, enabled=False
            )
            self._loaded[(repo, name)] = loaded
            await self._upsert_state(
                repo, name, loaded=True, enabled=False, last_error=settings_error
            )
            log.info("Loaded module %s/%s (disabled: invalid settings)", repo, name)
            return loaded

        try:
            await instance.on_load()
        except Exception:
            log.exception("Module %s/%s on_load failed", repo, name)
        loaded = LoadedModule(repo=repo, name=name, instance=instance, python_module=python_module)
        loaded.job_ids = await self._register_scheduled(repo, name, instance)
        self._register_triggers(repo, name, instance, cls)
        self._loaded[(repo, name)] = loaded
        await self._upsert_state(repo, name, loaded=True, enabled=True, last_error=None)
        log.info("Loaded module %s/%s", repo, name)
        return loaded

    def _register_triggers(
        self, repo: str, name: str, instance: Module, cls: type[Module]
    ) -> None:
        """Collect decorator-declared + pending triggers and register them."""
        registered = 0
        seen: set[int] = set()
        for klass in cls.__mro__:
            if klass is object:
                continue
            for attr_name, attr in klass.__dict__.items():
                descriptors = getattr(attr, "_vb_triggers", None)
                if not descriptors:
                    continue
                if id(attr) in seen:
                    continue
                seen.add(id(attr))
                bound = getattr(instance, attr_name)
                for desc in descriptors:
                    trig = Trigger(
                        kind=desc.kind,
                        match=desc.match,
                        excludes=desc.excludes,
                        handler=bound,
                        repo=repo,
                        name=name,
                        source="decorator",
                    )
                    self._registry.register(trig)
                    registered += 1
                    log.info(
                        "module %s/%s trigger %s %s",
                        repo, name, desc.kind, desc.match.describe(),
                    )
        for desc, handler in instance._pending_triggers:
            trig = Trigger(
                kind=desc.kind,
                match=desc.match,
                excludes=desc.excludes,
                handler=handler,
                repo=repo,
                name=name,
                source="dynamic",
            )
            self._registry.register(trig)
            registered += 1
            log.info(
                "module %s/%s trigger %s %s (dynamic)",
                repo, name, desc.kind, desc.match.describe(),
            )
        instance._pending_triggers.clear()
        if registered == 0:
            log.warning(
                "module %s/%s registered zero triggers — it will never be invoked",
                repo, name,
            )

    async def unload(self, repo: str, name: str) -> None:
        key = (repo, name)
        loaded = self._loaded.pop(key, None)
        if loaded is None:
            return
        # Clear triggers FIRST so an in-flight event cannot dispatch mid-teardown.
        self._registry.remove_for_module(repo, name)
        for job_id in loaded.job_ids:
            self.bot.scheduler.remove_job(job_id)
        self.bot.schedules.unregister_handlers_for(repo, name)
        try:
            await loaded.instance.on_unload()
        except Exception:
            log.exception("Module %s/%s on_unload failed", repo, name)
        sys.modules.pop(loaded.python_module.__name__, None)
        await self._upsert_state(repo, name, loaded=False, enabled=False)
        log.info("Unloaded module %s/%s", repo, name)

    async def reload(self, repo: str, name: str) -> LoadedModule:
        if (repo, name) in self._loaded:
            await self.unload(repo, name)
        return await self.load(repo, name)

    async def enable(self, repo: str, name: str) -> None:
        loaded = self._loaded.get((repo, name))
        if loaded is None:
            raise ValueError(f"Module {repo}/{name} not loaded")
        loaded.enabled = True
        self._registry.set_enabled(repo, name, True)
        for job_id in loaded.job_ids:
            self.bot.scheduler.resume_job(job_id)
        await self._upsert_state(repo, name, loaded=True, enabled=True)

    async def disable(self, repo: str, name: str) -> None:
        loaded = self._loaded.get((repo, name))
        if loaded is None:
            raise ValueError(f"Module {repo}/{name} not loaded")
        loaded.enabled = False
        self._registry.set_enabled(repo, name, False)
        for job_id in loaded.job_ids:
            self.bot.scheduler.pause_job(job_id)
        await self._upsert_state(repo, name, loaded=True, enabled=False)

    # ---------- introspection ----------

    def list_loaded(self) -> list[LoadedModule]:
        return list(self._loaded.values())

    # ---------- bus bridge ----------

    async def _dispatch(self, event: Event) -> None:
        # Skip pydle-synthesized outbound echoes — otherwise a module that
        # replies to its own trigger loops forever.
        if event.kind == "message":
            own = self.bot._own_nick_of(event.network)
            if own and event.get("source") == own:
                return
        for trig in self._registry.match(event):
            handler = guard_callback(trig.name, trig.handler)
            spawn_guarded(
                trig.name,
                handler(event),
                name=f"{trig.repo}/{trig.name}:{trig.kind}",
            )

    # ---------- persistence ----------

    async def _load_config(self, repo: str, name: str) -> dict:
        async with self.bot.db.session() as s:
            state = (
                await s.execute(
                    select(ModuleState).where(
                        ModuleState.repo_name == repo, ModuleState.module_name == name
                    )
                )
            ).scalar_one_or_none()
            if state is None or state.config_json is None:
                return {}
            try:
                return json.loads(state.config_json)
            except json.JSONDecodeError:
                return {}

    _UNSET = object()

    async def _upsert_state(
        self,
        repo: str,
        name: str,
        *,
        loaded: bool,
        enabled: bool,
        config_json: Any = _UNSET,
        last_error: Any = _UNSET,
    ) -> None:
        async with self.bot.db.session() as s:
            state = (
                await s.execute(
                    select(ModuleState).where(
                        ModuleState.repo_name == repo, ModuleState.module_name == name
                    )
                )
            ).scalar_one_or_none()
            serialized = self._UNSET if config_json is self._UNSET else (
                None if config_json is None else json.dumps(config_json)
            )
            if state is None:
                row = ModuleState(
                    repo_name=repo, module_name=name, loaded=loaded, enabled=enabled
                )
                if serialized is not self._UNSET:
                    row.config_json = serialized
                if last_error is not self._UNSET:
                    row.last_error = last_error
                s.add(row)
            else:
                state.loaded = loaded
                state.enabled = enabled
                if serialized is not self._UNSET:
                    state.config_json = serialized
                if last_error is not self._UNSET:
                    state.last_error = last_error
            await s.commit()

    async def save_settings(self, repo: str, name: str, values: dict[str, Any]) -> None:
        """Persist a validated settings dict. Preserves loaded/enabled flags and
        does not trigger reload — operator must call reload to pick up changes.
        """
        async with self.bot.db.session() as s:
            state = (
                await s.execute(
                    select(ModuleState).where(
                        ModuleState.repo_name == repo, ModuleState.module_name == name
                    )
                )
            ).scalar_one_or_none()
            serialized = json.dumps(values)
            if state is None:
                s.add(
                    ModuleState(
                        repo_name=repo,
                        module_name=name,
                        loaded=False,
                        enabled=False,
                        config_json=serialized,
                    )
                )
            else:
                state.config_json = serialized
                state.last_error = None
            await s.commit()

    async def get_stored_settings(self, repo: str, name: str) -> dict[str, Any]:
        """Read the raw stored settings dict for a module (empty if unset)."""
        return await self._load_config(repo, name)

    async def _register_scheduled(self, repo: str, name: str, instance: Module) -> list[str]:
        job_ids: list[str] = []
        for task in instance.scheduled_tasks():
            job_id = f"{repo}/{name}/{task.name}"
            wrapped = guard_callback(name, task.func)
            self.bot.scheduler.add_job(wrapped, trigger=task.trigger, job_id=job_id)
            job_ids.append(job_id)
        return job_ids


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def _find_module_class(python_module: ModuleType) -> type[Module] | None:
    for _, obj in inspect.getmembers(python_module):
        if (
            inspect.isclass(obj)
            and issubclass(obj, Module)
            and obj is not Module
            and obj.__module__ == python_module.__name__
        ):
            return obj
    return None
