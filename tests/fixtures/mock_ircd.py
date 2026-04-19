"""Minimal asyncio IRC server for unit/integration tests.

Not a real ircd. Parses incoming lines into IRC messages, records them for
assertions, and lets tests script responses. Covers paths Ergo can't
easily reproduce (RFC1459-only servers that ignore CAP, QuakeNet Q-bot).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class Message:
    command: str
    params: list[str] = field(default_factory=list)
    trailing: str | None = None
    raw: str = ""


def parse_line(line: str) -> Message:
    raw = line
    if line.startswith(":"):
        _, _, line = line.partition(" ")
    trailing: str | None = None
    if " :" in line:
        head, _, trailing = line.partition(" :")
        line = head
    parts = line.split()
    if not parts:
        return Message(command="", raw=raw)
    cmd, params = parts[0].upper(), parts[1:]
    if trailing is not None:
        params.append(trailing)
    return Message(command=cmd, params=params, trailing=trailing, raw=raw)


@dataclass
class ClientSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    received: list[Message] = field(default_factory=list)
    nick: str = ""
    user_received: bool = False
    registered: bool = False


class MockIrcd:
    """Configurable in-process IRC server."""

    def __init__(
        self,
        *,
        server_name: str = "mock.irc",
        ignore_cap: bool = False,
        autoreply_q: bool = False,
        advertise_sasl: bool = False,
    ) -> None:
        self.server_name = server_name
        self.ignore_cap = ignore_cap
        self.autoreply_q = autoreply_q
        self.advertise_sasl = advertise_sasl
        self._server: asyncio.base_events.Server | None = None
        self.sessions: list[ClientSession] = []
        self.port: int = 0
        self._line_hook: Callable[[ClientSession, Message], Awaitable[None]] | None = None

    def on_line(self, hook: Callable[[ClientSession, Message], Awaitable[None]]) -> None:
        self._line_hook = hook

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = await asyncio.start_server(self._handle, host, port)
        assert self._server.sockets is not None
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _send(self, sess: ClientSession, line: str) -> None:
        sess.writer.write((line + "\r\n").encode("utf-8"))
        await sess.writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sess = ClientSession(reader=reader, writer=writer)
        self.sessions.append(sess)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                msg = parse_line(line)
                sess.received.append(msg)
                await self._dispatch(sess, msg)
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            with _suppress_errors():
                writer.close()
                await writer.wait_closed()

    async def _dispatch(self, sess: ClientSession, msg: Message) -> None:
        cmd = msg.command
        if cmd == "CAP":
            await self._handle_cap(sess, msg)
        elif cmd == "NICK" and msg.params:
            sess.nick = msg.params[0]
            await self._maybe_welcome(sess)
        elif cmd == "USER":
            sess.user_received = True
            await self._maybe_welcome(sess)
        elif cmd == "AUTHENTICATE":
            await self._handle_authenticate(sess, msg)
        elif cmd == "PRIVMSG" and self.autoreply_q and msg.params:
            target = msg.params[0]
            if target.upper().startswith("Q@"):
                await self._send(sess, f":{target} NOTICE {sess.nick} :AUTH OK")
                await self._send(
                    sess,
                    f":{self.server_name} 396 {sess.nick} {sess.nick}.users.quakenet.org :is now your visible host",
                )
        if self._line_hook is not None:
            await self._line_hook(sess, msg)

    async def _handle_cap(self, sess: ClientSession, msg: Message) -> None:
        if self.ignore_cap:
            # Simulate legacy server: ignore CAP entirely (client should never have sent it in rfc1459 mode).
            return
        sub = msg.params[0].upper() if msg.params else ""
        if sub == "LS":
            caps = "sasl" if self.advertise_sasl else ""
            await self._send(sess, f":{self.server_name} CAP * LS :{caps}")
        elif sub == "REQ":
            wanted = msg.params[1] if len(msg.params) > 1 else ""
            await self._send(sess, f":{self.server_name} CAP * ACK :{wanted}")
        elif sub == "END":
            pass

    async def _handle_authenticate(self, sess: ClientSession, msg: Message) -> None:
        arg = msg.params[0] if msg.params else ""
        if arg.upper() == "PLAIN":
            # Advance to challenge phase.
            await self._send(sess, "AUTHENTICATE +")
        elif arg.upper() == "EXTERNAL":
            await self._send(sess, "AUTHENTICATE +")
        else:
            # Assume this is the response payload.
            await self._send(
                sess,
                f":{self.server_name} 903 {sess.nick or '*'} :SASL authentication successful",
            )

    async def _maybe_welcome(self, sess: ClientSession) -> None:
        if sess.registered or not sess.nick or not sess.user_received:
            return
        sess.registered = True
        await self._send(sess, f":{self.server_name} 001 {sess.nick} :Welcome")
        await self._send(sess, f":{self.server_name} 002 {sess.nick} :Your host is {self.server_name}")
        await self._send(sess, f":{self.server_name} 003 {sess.nick} :Server created")
        await self._send(sess, f":{self.server_name} 004 {sess.nick} {self.server_name} mock o o")
        await self._send(sess, f":{self.server_name} 375 {sess.nick} :- {self.server_name} MOTD -")
        await self._send(sess, f":{self.server_name} 376 {sess.nick} :End of MOTD")


class _suppress_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, Exception)
