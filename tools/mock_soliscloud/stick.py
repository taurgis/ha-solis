"""Solarman V5 stick emulator.

IGEN does not publish a runnable LSW-3 or LSE-3 firmware image. This emulator
follows the V5 frame used by pysolarmanv5 and the frames captured from a real
stick: serial 1902137671, discovery ``ip,mac,serial``, function-4 registers in
the 3000 range, and Modbus exception 2 for the 33000 hybrid range.

Night is a powered-off logger: TCP 8899 and UDP 48899 both stop. The next
morning it can listen on a different address.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import socket
import struct
import time

from aiohttp import web

from custom_components.solis.solarman import (
    build_response,
    crc16,
    logger_serial_of,
    modbus_from_request,
    sequence_of,
)

SERIAL = 1902137671
MAC = "bc54f9f57b5c"
INVERTER_SERIAL = b"SOLIS001"


def captured_registers() -> dict[int, int]:
    """Return the string-inverter registers read from the real stick."""
    words = {
        3000: 15,
        3001: 15,
        3002: 0,
        3003: 1,
        3004: 0,
        3005: 0,
        3006: 0,
        3007: 0,
        3008: 0,
        3009: 21970,
        3010: 0,
        3011: 30,
        3012: 0,
        3013: 169,
        3014: 0,
        3015: 59,
        3016: 0,
        3017: 2690,
        3018: 0,
        3019: 2717,
        3021: 2784,
        3022: 13,
        3031: 3623,
        3035: 2349,
        3038: 4,
        3041: 272,
        3042: 4999,
        3043: 3,
        3048: 4,
        3049: 10000,
    }
    for offset in range(4):
        pair = INVERTER_SERIAL[offset * 2 : offset * 2 + 2]
        words[3060 + offset] = int.from_bytes(pair, "big")
    return words


@dataclass
class StickState:
    """Mutable behaviour of one emulated logger."""

    host: str = "127.0.0.1"
    serial: int = SERIAL
    mac: str = MAC
    tcp_port: int = 8899
    udp_port: int = 48899
    night: bool = False
    slow: float = 0.0
    garbage: bool = False
    reset_after: int = 0
    locked: bool = False
    protocol: str = "v5"
    lock_above: int | None = None
    registers: dict[int, int] = field(default_factory=captured_registers)
    connections: list[float] = field(default_factory=list)
    kind: str = "solarman"
    functions: tuple[int, ...] = ()
    max_clients: int = 0
    port_closed: bool = False
    silent: bool = False
    close_disables_port: bool = False


class StickEmulator:
    """Serve V5 on TCP and the IGEN discovery reply on UDP."""

    def __init__(self, state: StickState | None = None) -> None:
        """Store the scenario. ``start`` binds the sockets."""
        self.state = state or StickState()
        self._tcp: asyncio.Server | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._control: web.AppRunner | None = None
        self._open_clients = 0

    async def start(self, control_port: int | None = None) -> None:
        """Listen, unless the stick is in night mode."""
        await self._listen()
        if control_port is not None:
            await self._serve_control(control_port)

    async def _listen(self) -> None:
        if not self.state.port_closed:
            self._tcp = await asyncio.start_server(
                self._client, self.state.host, self.state.tcp_port
            )
        if self.state.night or self.state.kind == "s2":
            return
        loop = asyncio.get_running_loop()

        class _Udp(asyncio.DatagramProtocol):
            def __init__(self, emulator: StickEmulator) -> None:
                self._emulator = emulator
                self.transport: asyncio.DatagramTransport | None = None

            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport  # type: ignore[assignment]

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                if self._emulator.state.night or self.transport is None:
                    return
                text = data.decode("utf-8", errors="ignore")
                if "WIFIKIT" not in text and "HF-A11" not in text:
                    return
                state = self._emulator.state
                payload = f"{state.host},{state.mac},{state.serial}".encode()
                self.transport.sendto(payload, addr)

        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _Udp(self),
            local_addr=("0.0.0.0", self.state.udp_port),
        )
        self._udp = transport

    async def apply(self, changes: dict[str, object]) -> None:
        """Apply a lab control payload and rebind when the address or night changes."""
        rebound = False
        keys = (
            "night",
            "slow",
            "garbage",
            "reset_after",
            "host",
            "locked",
            "protocol",
            "lock_above",
            "kind",
            "max_clients",
            "port_closed",
            "silent",
            "close_disables_port",
            "tcp_port",
        )
        for key in keys:
            if key in changes:
                if key in {"host", "night", "kind", "port_closed", "tcp_port"}:
                    rebound = True
                setattr(self.state, key, changes[key])
        functions = changes.get("functions")
        if isinstance(functions, list):
            self.state.functions = tuple(int(item) for item in functions)
            rebound = True
        registers = changes.get("registers")
        if isinstance(registers, dict):
            for address, value in registers.items():
                self.state.registers[int(address)] = int(value) & 0xFFFF
        if rebound:
            await self._close_sockets()
            await self._listen()

    async def close(self) -> None:
        """Stop every socket."""
        await self._close_sockets()
        if self._control is not None:
            await self._control.cleanup()

    async def _close_sockets(self) -> None:
        if self._tcp is not None:
            self._tcp.close()
            await self._tcp.wait_closed()
            self._tcp = None
        if self._udp is not None:
            self._udp.close()
            self._udp = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._open_clients += 1
        now = time.monotonic()
        recent = [stamp for stamp in self.state.connections if now - stamp < 2]
        recent.append(now)
        self.state.connections = recent
        if self.state.night:
            self._open_clients -= 1
            writer.close()
            return
        if self.state.max_clients and self._open_clients > self.state.max_clients:
            self._open_clients -= 1
            await _reset(reader, writer)
            return
        if self.state.locked:
            self._open_clients -= 1
            await _reset(reader, writer)
            return
        if self.state.reset_after and len(recent) > self.state.reset_after:
            self._open_clients -= 1
            await _reset(reader, writer)
            return
        try:
            if self.state.silent:
                while True:
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout=1)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return
            first = await asyncio.wait_for(reader.read(1), timeout=3)
            if not first:
                return
            if first == b"\xa5":
                if self.state.protocol == "mbtcp" or self.state.kind == "s2":
                    return
                await self._serve_v5(reader, writer, first)
                return
            if self.state.protocol == "v5" and self.state.kind != "s2":
                return
            await self._serve_mbap(reader, writer, first)
        except (TimeoutError, ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
            return
        finally:
            self._open_clients = max(0, self._open_clients - 1)
            transport = writer.transport
            if transport is not None and not transport.is_closing():
                writer.close()
            if self.state.close_disables_port and self._tcp is not None:
                self.state.port_closed = True
                self._tcp.close()
                await self._tcp.wait_closed()
                self._tcp = None

    async def _serve_v5(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: bytes
    ) -> None:
        buffer = first
        while True:
            frame, buffer = _pop_v5(buffer)
            if frame is None:
                chunk = await asyncio.wait_for(reader.read(512), timeout=3)
                if not chunk:
                    return
                buffer += chunk
                continue
            if logger_serial_of(frame) != self.state.serial:
                await asyncio.sleep(1)
                return
            if self.state.slow:
                await asyncio.sleep(self.state.slow)
            if self.state.garbage:
                writer.write(b"\x00\x01garbage")
                await writer.drain()
                return
            modbus = modbus_from_request(frame)
            if len(modbus) >= 6 and _should_lock(self.state, int.from_bytes(modbus[2:4], "big")):
                self.state.locked = True
                await _reset(reader, writer)
                return
            reply = _modbus_reply(self.state, modbus)
            writer.write(build_response(self.state.serial, sequence_of(frame), reply))
            await writer.drain()

    async def _serve_mbap(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, first: bytes
    ) -> None:
        if self.state.slow:
            await asyncio.sleep(self.state.slow)
        pending = first
        while True:
            if not pending:
                pending = await asyncio.wait_for(reader.read(1), timeout=3)
                if not pending:
                    return
            header = pending + await asyncio.wait_for(reader.readexactly(6), timeout=3)
            length = int.from_bytes(header[4:6], "big")
            body = await asyncio.wait_for(reader.readexactly(max(length - 1, 0)), timeout=3)
            frame = header + body
            pending = b""
            if len(frame) >= 12 and _should_lock(self.state, int.from_bytes(frame[8:10], "big")):
                self.state.locked = True
                await _reset(reader, writer)
                return
            writer.write(_mbap_reply(self.state, frame))
            await writer.drain()

    async def _serve_control(self, port: int) -> None:
        app = web.Application()

        async def _control(request: web.Request) -> web.Response:
            payload = await request.json()
            await self.apply(payload if isinstance(payload, dict) else {})
            return web.json_response(
                {"host": self.state.host, "night": self.state.night, "serial": self.state.serial}
            )

        app.router.add_post("/__control", _control)
        self._control = web.AppRunner(app)
        await self._control.setup()
        await web.TCPSite(self._control, "127.0.0.1", port).start()


def _pop_v5(buffer: bytes) -> tuple[bytes | None, bytes]:
    if len(buffer) < 13 or buffer[0] != 0xA5:
        return None, buffer
    total = 11 + int.from_bytes(buffer[1:3], "little") + 2
    if len(buffer) < total:
        return None, buffer
    return buffer[:total], buffer[total:]


def _should_lock(state: StickState, address: int) -> bool:
    return state.lock_above is not None and address >= state.lock_above


async def _reset(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Accept the TCP session, then reset it. That is the locked logger."""
    with suppress(TimeoutError, ConnectionError, OSError):
        await asyncio.wait_for(reader.read(64), timeout=0.2)
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    buffer = b""
    while True:
        chunk = await asyncio.wait_for(reader.read(512), timeout=3)
        if not chunk:
            raise ConnectionError("client closed")
        buffer += chunk
        if len(buffer) >= 13 and buffer[0] == 0xA5:
            total = 11 + int.from_bytes(buffer[1:3], "little") + 2
            if len(buffer) >= total:
                return buffer[:total]


def _modbus_reply(state: StickState, request: bytes) -> bytes:
    if len(request) < 6 or request[1] != 0x04:
        return _exception(request[:1] or b"\x01", 0x04, 1)
    slave = request[0]
    address, count = _addr(request)
    if slave != 1 or address >= 33000:
        return _exception(bytes((slave,)), 0x04, 2)
    values = [state.registers.get(address + offset, 0) & 0xFFFF for offset in range(count)]
    body = bytes((slave, 0x04, len(values) * 2))
    for value in values:
        body += value.to_bytes(2, "big")
    return body + crc16(body).to_bytes(2, "little")


def _addr(request: bytes) -> tuple[int, int]:
    return int.from_bytes(request[2:4], "big"), int.from_bytes(request[4:6], "big")


def _allowed_functions(state: StickState) -> tuple[int, ...]:
    if state.functions:
        return state.functions
    if state.kind == "s2":
        return (3, 4)
    return (4,)


def _mbap_reply(state: StickState, frame: bytes) -> bytes:
    unit = frame[6:7] or b"\x01"
    function = frame[7] if len(frame) > 7 else 0x04
    address = int.from_bytes(frame[8:10], "big") if len(frame) >= 12 else 0
    count = int.from_bytes(frame[10:12], "big") if len(frame) >= 12 else 0
    allowed = _allowed_functions(state)
    if function not in allowed:
        pdu = bytes((function | 0x80, 1))
    elif unit != b"\x01" or address >= 33000:
        pdu = bytes((function | 0x80, 2))
    else:
        values = [state.registers.get(address + offset, 0) & 0xFFFF for offset in range(count)]
        pdu = bytes((function, len(values) * 2))
        for value in values:
            pdu += value.to_bytes(2, "big")
    return frame[:2] + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + unit + pdu


def _exception(slave: bytes, function: int, code: int) -> bytes:
    body = bytes((slave[0], function | 0x80, code))
    return body + crc16(body).to_bytes(2, "little")


def main() -> None:
    """Serve the stick until the process is stopped. ``POST /__control`` changes it."""
    import argparse

    parser = argparse.ArgumentParser(description="Solarman V5 or S2-WL-ST stick emulator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--control-port", type=int, default=8877)
    parser.add_argument("--kind", choices=("solarman", "s2"), default="solarman")
    parser.add_argument("--tcp-port", type=int, default=0)
    args = parser.parse_args()

    async def _serve() -> None:
        port = args.tcp_port or (502 if args.kind == "s2" else 8899)
        emulator = StickEmulator(StickState(host=args.host, kind=args.kind, tcp_port=port))
        await emulator.start(control_port=args.control_port)
        print(
            f"stick emulator host={args.host} control=http://127.0.0.1:{args.control_port}",
            flush=True,
        )
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
