"""Emulator for a Solis stick that only serves a status web page.

Follows a stick read on 2025: collector firmware 10010106, web 2.2.0, lwIP,
HTTP/1.0, basic auth with the user ``admin``, replies padded with spaces.
``inverter.cgi`` returns ``serial;firmware;model;temperature;power;today;total;alarm;``
and ``moniter.cgi`` returns the stick details. The real stick refreshes the
values about every five minutes and serves the cached ones in between;
``refresh`` stands for that tick.

Other firmware it can stand in for:

- ``total_text``: a literal total field, such as the ``d`` one firmware prints.
- ``layout="status"``: an IGEN/Solarman-made stick that has no ``inverter.cgi``
  and serves ``status.html`` with ``var webdata_now_p = "...";`` variables.
  ``night_value`` (``""`` or ``"---"``) blanks the readings as at night.
- ``realm``: the name in the Basic challenge; sticks differ.
- ``join_wifi``: some firmware replaces the web password with the Wi-Fi
  password once the stick joins the network.
- ``chunked``: answer HTTP/1.1 with a chunked body.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
import socket
import struct

REALM = "solis"
LAYOUT_CGI = "cgi"
LAYOUT_STATUS = "status"


@dataclass
class WebStickState:
    """Mutable behaviour of one emulated web-page stick."""

    host: str = "127.0.0.1"
    port: int = 0
    password: str = "123456789"
    serial: str = "1031000000000001"
    firmware: str = "0F000F"
    model: str = "E4"
    temperature: float = 38.4
    power: int = 382
    today: float = 2.8
    total: int = 21986
    alarm: str = "NO"
    total_text: str | None = None
    layout: str = LAYOUT_CGI
    night_value: str | None = None
    realm: str = REALM
    chunked: bool = False
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

    def join_wifi(self, wifi_password: str) -> None:
        """The stick joined the network and now wants the Wi-Fi password."""
        self.state.password = wifi_password

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
                + f'WWW-authenticate: Basic realm="{self.state.realm}"\r\n'.encode("latin-1")
                + b"Content-type: text/html\r\n\r\n"
            )
        if mode == "error":
            return b"HTTP/1.0 500 Internal Server Error\r\n\r\n"
        if mode == "redirect":
            return b"HTTP/1.0 302 Found\r\nLocation: http://127.0.0.2/\r\n\r\n"
        body = self._body(path)
        if body is None:
            return b"HTTP/1.0 404 Not Found\r\n\r\n"
        data = body.encode("latin-1")
        if self.state.chunked:
            # Two chunks, with a chunk extension, as an HTTP/1.1 server may send.
            half = len(data) // 2
            chunks = b"".join(
                f"{len(part):x};x=1\r\n".encode("ascii") + part + b"\r\n"
                for part in (data[:half], data[half:])
            )
            return (
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-type: text/html\r\n\r\n" + chunks + b"0\r\n\r\n"
            )
        return b"HTTP/1.0 200 OK\r\nServer: lwIP/1.3.1\r\nContent-type: text/html\r\n\r\n" + data

    def _authorized(self, lines: list[str]) -> bool:
        wanted = base64.b64encode(f"admin:{self.state.password}".encode()).decode("ascii")
        return any(line.strip() == f"Authorization: Basic {wanted}" for line in lines)

    def _body(self, path: str) -> str | None:
        state = self.state
        if state.layout == LAYOUT_STATUS:
            return self._status_page() if path.startswith("/status.html") else None
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
                f"{state.total}" if state.total_text is None else state.total_text,
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

    def _status_page(self) -> str:
        """The IGEN-made status page: readings as JavaScript variables."""
        state = self.state
        if state.mode == "garbage":
            return "<html>Not the status page</html>"
        night = state.night_value
        now = f"{state.power}" if night is None else night
        today = f"{state.today:.2f}" if night is None else night
        total = f"{state.total}" if night is None else night
        alarm = "" if state.alarm == "NO" else state.alarm
        values = (
            ("webdata_sn", f"{state.serial}      "),
            ("webdata_msvn", state.firmware),
            ("webdata_ssvn", ""),
            ("webdata_pv_type", ""),
            ("webdata_rate_p", ""),
            ("webdata_now_p", now),
            ("webdata_today_e", today),
            ("webdata_total_e", total),
            ("webdata_alarm", alarm),
            ("webdata_utime", "0"),
            ("cover_mid", state.mid),
            ("cover_ver", "MW3_16U_5406_1.59"),
            ("cover_wmode", "APSTA"),
            ("cover_sta_rssi", "40%"),
            ("cover_sta_mac", state.mac),
        )
        script = "\n".join(f'var {name} = "{value}";' for name, value in values)
        return (
            "<html><head><title>Status</title>"
            '<script type="text/javascript">\n'
            + script
            + '\n</script></head><body><div id="webdata_now_p">---</div></body></html>'
        )
