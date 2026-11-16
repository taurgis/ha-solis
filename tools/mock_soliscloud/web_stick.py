"""Emulator for a Solis stick that only serves a status web page.

Follows a stick read on 2025: collector firmware 10010106, web 2.2.0, lwIP,
HTTP/1.0, basic auth with the user ``admin``, replies padded with spaces.
``inverter.cgi`` returns ``serial;firmware;model;temperature;power;today;total;alarm;``
and ``moniter.cgi`` returns the stick details. The real stick refreshes the
values about every five minutes and serves the cached ones in between;
``refresh`` stands for that tick.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import socket
import struct

REALM = "solis"


@dataclass
class WebStickState:
    """Mutable behaviour of one emulated web-page stick."""

    host: str = "127.0.0.1"
    port: int = 0
    password: str = "123456789"
    serial: str = "110E40182280017"
    firmware: str = "0F000F"
    model: str = "E4"
    temperature: float = 38.4
    power: int = 382
    today: float = 2.8
    total: int = 21986
    alarm: str = "NO"
    mid: str = "7A122C096F409F31"
    mac: str = "B8:D6:1A:58:A7:0B"
    # ok, garbage (200 with another body), error (500), silent (accept, send nothing),
    # redirect (302), nopage (200 with a non-HTTP reply), hang (accept, never answer),
    # reset (TCP reset).
    mode: str = "ok"
    requests: list[str] = field(default_factory=list)


class WebStickEmulator:
    """Serve ``/``, ``/inverter.cgi`` and ``/moniter.cgi`` on one TCP port."""

    def __init__(self, state: WebStickState | None = None) -> None:
        """Store the scenario. ``start`` binds the socket."""
        self.state = state or WebStickState()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The bound port."""
        if self._server is not None:
            self.state.port = int(self._server.sockets[0].getsockname()[1])
        return self.state.port

    async def start(self) -> None:
        """Listen."""
        self._server = await asyncio.start_server(
            self._serve, self.state.host, self.state.port, reuse_address=True
        )
        self.state.port = self.port

    async def close(self) -> None:
        """Stop listening, as when the inverter powers the stick off."""
        if self._server is not None:
            self._server.close()
            if hasattr(self._server, "close_clients"):  # Python 3.13 and later
                self._server.close_clients()
            await self._server.wait_closed()
            self._server = None

    def refresh(self, *, power: int, temperature: float, today: float | None = None) -> None:
        """The stick polled the inverter again."""
        self.state.power = power
        self.state.temperature = temperature
        if today is not None:
            self.state.today = today

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if self.state.mode == "reset":
                self.state.requests.append("reset")
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    # Closing with a zero linger sends a TCP reset instead of a FIN.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            if self.state.mode == "hang":
                self.state.requests.append("hang")
                await reader.read()  # until the client gives up and closes
                return
            reply = self._reply(head.decode("latin-1"))
            if reply:
                writer.write(reply)
                await writer.drain()
        except (
            TimeoutError,
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _reply(self, head: str) -> bytes:
        lines = head.split("\r\n")
        path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
        self.state.requests.append(path)
        mode = self.state.mode
        if mode == "silent":
            return b""
        if mode == "nopage":
            return b"\x00\x01\x02 not http"
        if not self._authorized(lines):
            return (
                b"HTTP/1.0 401 unauthorized\r\n"
                b"Server: lwIP/1.3.1 (http://savannah.nongnu.org/projects/lwip)\r\n"
                b'WWW-authenticate: Basic realm="solis"\r\n'
                b"Content-type: text/html\r\n\r\n"
            )
        if mode == "error":
            return b"HTTP/1.0 500 Internal Server Error\r\n\r\n"
        if mode == "redirect":
            return b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.2/\r\n\r\n"
        body = self._body(path)
        if body is None:
            return b"HTTP/1.0 404 Not Found\r\n\r\n"
        return (
            b"HTTP/1.0 200 OK\r\nServer: lwIP/1.3.1\r\nContent-type: text/html\r\n\r\n"
            + body.encode("latin-1")
        )

    def _authorized(self, lines: list[str]) -> bool:
        wanted = base64.b64encode(f"admin:{self.state.password}".encode()).decode("ascii")
        return any(line.strip() == f"Authorization: Basic {wanted}" for line in lines)

    def _body(self, path: str) -> str | None:
        state = self.state
        if path.startswith("/inverter.cgi"):
            if state.mode == "garbage":
                return "<html>Not the inverter</html>"
            fields = (
                state.serial,
                state.firmware,
                state.model,
                f"{state.temperature}",
                f"{state.power}",
                f"{state.today:.6f}",
                f"{state.total}",
                state.alarm,
            )
            return "\x00" * 33 + ";".join(fields) + ";" + " " * 200
        if path.startswith("/moniter.cgi"):
            if state.mode == "garbage":
                return "<html>Not the collector</html>"
            fields = (
                "",
                "2",
                state.mid,
                "10010106",
                "Disabled",
                "null",
                "null",
                "Disabled",
                "null",
                "null",
                state.host,
                state.mac,
                "Connected",
                "null",
            )
            return "\x00" * 33 + ";".join(fields) + ";" + " " * 200
        return None
