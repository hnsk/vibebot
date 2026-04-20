"""Runtime settings CRUD — drives the Settings tab in the web UI.

All mutations go through `bot.settings` (`SettingsService`), which serializes
writes and publishes `settings_changed` events on the event bus.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from vibebot.api.auth import require_token
from vibebot.config import NetworkConfig, ServerConfig
from vibebot.core.settings import SettingsError

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_token)])


class NetworkPatch(BaseModel):
    nick: str | None = None
    username: str | None = None
    realname: str | None = None
    hostname: str | None = None
    protocol: str | None = None
    auth: dict[str, Any] | None = None
    reconnect: bool = False


class ServerBody(BaseModel):
    host: str
    port: int = 6697
    tls: bool = True
    tls_verify: bool = True
    is_default: bool = False


class AddServerBody(ServerBody):
    index: int | None = Field(default=None, description="Insertion index; append if omitted")


class ChannelBody(BaseModel):
    channel: str


@router.get("")
async def get_settings(request: Request) -> dict[str, Any]:
    return request.app.state.bot.settings.snapshot()


@router.get("/networks")
async def list_networks(request: Request) -> list[dict[str, Any]]:
    return request.app.state.bot.settings.snapshot()["networks"]


@router.get("/networks/{name}")
async def get_network(name: str, request: Request) -> dict[str, Any]:
    try:
        return request.app.state.bot.settings._network_snapshot(name)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/networks", status_code=201)
async def create_network(body: NetworkConfig, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.add_network(body)
    except SettingsError as exc:
        raise HTTPException(409, str(exc)) from exc
    return svc._network_snapshot(body.name)


@router.patch("/networks/{name}")
async def patch_network(name: str, body: NetworkPatch, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.update_network(
            name,
            nick=body.nick,
            username=body.username,
            realname=body.realname,
            hostname=body.hostname,
            protocol=body.protocol,
            auth=body.auth,
            reconnect=body.reconnect,
        )
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return svc._network_snapshot(name)


@router.delete("/networks/{name}", status_code=204)
async def delete_network(name: str, request: Request) -> None:
    svc = request.app.state.bot.settings
    try:
        await svc.remove_network(name)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/networks/{name}/servers", status_code=201)
async def add_server(name: str, body: AddServerBody, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    server = ServerConfig(
        host=body.host, port=body.port, tls=body.tls,
        tls_verify=body.tls_verify, is_default=body.is_default,
    )
    try:
        await svc.add_server(name, server, body.index)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return svc._network_snapshot(name)


@router.patch("/networks/{name}/servers/{index}")
async def update_server(name: str, index: int, body: ServerBody, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    server = ServerConfig(**body.model_dump())
    try:
        await svc.update_server(name, index, server)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return svc._network_snapshot(name)


@router.delete("/networks/{name}/servers/{index}", status_code=204)
async def delete_server(name: str, index: int, request: Request) -> None:
    svc = request.app.state.bot.settings
    try:
        await svc.remove_server(name, index)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/networks/{name}/servers/{index}/default")
async def mark_default(name: str, index: int, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.set_default_server(name, index)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return svc._network_snapshot(name)


@router.post("/networks/{name}/channels", status_code=201)
async def add_channel(name: str, body: ChannelBody, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.add_channel(name, body.channel)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return svc._network_snapshot(name)


@router.delete("/networks/{name}/channels/{channel:path}", status_code=204)
async def delete_channel(name: str, channel: str, request: Request) -> None:
    svc = request.app.state.bot.settings
    try:
        await svc.remove_channel(name, channel)
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/networks/{name}/connect")
async def connect(name: str, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.connect(name)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return svc._network_snapshot(name)


@router.post("/networks/{name}/disconnect")
async def disconnect(name: str, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.disconnect(name)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return svc._network_snapshot(name)


@router.post("/networks/{name}/reconnect")
async def reconnect(name: str, request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.reconnect(name)
    except SettingsError as exc:
        raise HTTPException(404, str(exc)) from exc
    return svc._network_snapshot(name)


@router.post("/save")
async def save(request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.save_to_disk()
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "ok", "path": svc._config_path and str(svc._config_path)}


@router.post("/reload")
async def reload(request: Request) -> dict[str, Any]:
    svc = request.app.state.bot.settings
    try:
        await svc.reload_from_disk()
    except SettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return svc.snapshot()
