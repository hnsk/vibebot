"""HTTP client for the TUI — thin wrapper over the bot's REST API."""

from __future__ import annotations

from typing import Any

import httpx


class ApiClient:
    """REST client used by every TUI widget.

    Methods return decoded JSON (or None for 204 responses). Errors surface as
    httpx.HTTPStatusError so callers can show the status/message verbatim.
    """

    def __init__(self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        self._base_url = base_url
        self._token = token

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token(self) -> str:
        return self._token

    async def close(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _get(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        if r.status_code == 204:
            return None
        return r.json()

    async def _post(self, path: str, json: dict | None = None) -> Any:
        r = await self._client.post(path, json=json or {})
        r.raise_for_status()
        if r.status_code == 204:
            return None
        return r.json()

    async def _post_query(self, path: str, params: dict[str, Any]) -> Any:
        r = await self._client.post(path, params=params)
        r.raise_for_status()
        if r.status_code == 204:
            return None
        return r.json()

    async def _delete(self, path: str) -> None:
        r = await self._client.delete(path)
        r.raise_for_status()

    # --- discovery -----------------------------------------------------
    async def networks(self) -> list[dict]:
        return await self._get("/api/networks")

    async def channels(self, network: str) -> list[dict]:
        return await self._get(f"/api/networks/{network}/channels")

    async def users(self, network: str, channel: str) -> list[dict]:
        return await self._get(f"/api/networks/{network}/channels/{channel}/users")

    async def topic(self, network: str, channel: str) -> dict:
        return await self._get(f"/api/networks/{network}/channels/{channel}/topic")

    async def history(self, network: str, channel: str) -> list[dict]:
        return await self._get(f"/api/networks/{network}/channels/{channel}/history")

    async def queries(self, network: str) -> list[dict]:
        return await self._get(f"/api/networks/{network}/queries")

    async def query_history(self, network: str, peer: str) -> list[dict]:
        return await self._get(f"/api/networks/{network}/queries/{peer}/history")

    async def close_query(self, network: str, peer: str) -> None:
        await self._delete(f"/api/networks/{network}/queries/{peer}")

    # --- admin ---------------------------------------------------------
    async def modules(self) -> list[dict]:
        return await self._get("/api/modules")

    async def repos(self) -> list[dict]:
        return await self._get("/api/repos")

    # --- chat ops ------------------------------------------------------
    async def send(self, network: str, target: str, message: str) -> Any:
        return await self._post(f"/api/networks/{network}/send", {"target": target, "message": message})

    async def join(self, network: str, channel: str) -> Any:
        # channel is a query parameter, not a JSON body — see routes/networks.py:30.
        return await self._post_query(f"/api/networks/{network}/join", {"channel": channel})

    async def part(self, network: str, channel: str, reason: str | None = None) -> Any:
        params: dict[str, Any] = {"channel": channel}
        if reason:
            params["reason"] = reason
        return await self._post_query(f"/api/networks/{network}/part", params)

    async def op(self, network: str, channel: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/op", {"channel": channel, "nick": nick})

    async def deop(self, network: str, channel: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/deop", {"channel": channel, "nick": nick})

    async def voice(self, network: str, channel: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/voice", {"channel": channel, "nick": nick})

    async def devoice(self, network: str, channel: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/devoice", {"channel": channel, "nick": nick})

    async def kick(self, network: str, channel: str, nick: str, reason: str | None = None) -> Any:
        return await self._post(
            f"/api/networks/{network}/kick",
            {"channel": channel, "nick": nick, "reason": reason},
        )

    async def ban(self, network: str, channel: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/ban", {"channel": channel, "nick": nick})

    async def kickban(self, network: str, channel: str, nick: str, reason: str | None = None) -> Any:
        return await self._post(
            f"/api/networks/{network}/kickban",
            {"channel": channel, "nick": nick, "reason": reason},
        )

    async def mode(self, network: str, channel: str, flags: str, args: list[str] | None = None) -> Any:
        return await self._post(
            f"/api/networks/{network}/mode",
            {"channel": channel, "flags": flags, "args": list(args or [])},
        )

    async def set_topic(self, network: str, channel: str, topic: str | None) -> Any:
        return await self._post(f"/api/networks/{network}/topic", {"channel": channel, "topic": topic})

    async def set_nick(self, network: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/nick", {"nick": nick})

    async def whois(self, network: str, nick: str) -> Any:
        return await self._post(f"/api/networks/{network}/whois", {"nick": nick})

    async def raw(self, network: str, line: str) -> Any:
        return await self._post(f"/api/networks/{network}/raw", {"line": line})
