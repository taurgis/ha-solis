"""LAN emulators for the Solis dongle lab.

One process serves every stick the audit drives from Home Assistant:

- Solarman V5 on TCP 8899 and UDP 48899
- S2-WL-ST Modbus TCP on 502, with a port-80 page that names the stick
- A Waveshare-style gateway on 502, with a port-80 page that names the gateway
- An unnamed port-502 device, so auto-detect asks which device it is
- Modbus RTU over TCP on the Waveshare transparent port 4196
- An S5-WiFi-ST whose port 502 can be closed after setup

Each device has its own address on the loopback interface. POST
http://127.0.0.1:8871/__control changes one device. The body is JSON.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import struct
import sys
import types

ROOT = Path(__file__).resolve().parents[2]


def _load_solarman() -> types.ModuleType:
    """Import the V5 codec without loading the Home Assistant package init."""
    parent = types.ModuleType("custom_components")
    parent.__path__ = [str(ROOT / "custom_components")]
    sys.modules["custom_components"] = parent
    package = types.ModuleType("custom_components.solis")
    package.__path__ = [str(ROOT / "custom_components" / "solis")]
    package.__package__ = "custom_components.solis"
    sys.modules["custom_components.solis"] = package
    from custom_components.solis import solarman

    return solarman


_SOLARMAN = _load_solarman()
build_response = _SOLARMAN.build_response
crc16 = _SOLARMAN.crc16
logger_serial_of = _SOLARMAN.logger_serial_of
modbus_from_request = _SOLARMAN.modbus_from_request
sequence_of = _SOLARMAN.sequence_of

CONTROL_PORT = 8871
LAB_NET = "192.168.250."


def _words(serial: str, power: int) -> dict[int, int]:
    """Old 3000-range string map. Lifetime energy is 21970 kWh."""
    text = serial.encode("ascii").ljust(8, b"\x00")[:8]
    words = {
        3000: 15,
        3001: 15,
        3002: 0,
        3003: 1,
        3004: 0,
        3005: power & 0xFFFF,
        3008: 0,
        3009: 21970,
        3011: 30,
        3014: 59,
        3015: 0,
        3035: 2349,
        3041: 272,
        3042: 4999,
        3043: 1,
    }
    for offset in range(4):
        words[3060 + offset] = int.from_bytes(text[offset * 2 : offset * 2 + 2], "big")
    return words


@dataclass
class Device:
    """One emulated logger and the fault the lab last asked for."""

    name: str
    host: str
    tcp_port: int
    framing: str
    serial_text: str
    power: int
    logger_serial: int = 0
    mac: str = ""
    udp_port: int = 0
    http_port: int = 0
    page: str = ""
    night: bool = False
    mode: str = "ok"
    registers: dict[int, int] = field(default_factory=dict)
    tcp: asyncio.Server | None = None
    http: asyncio.Server | None = None

    def __post_init__(self) -> None:
        if not self.registers:
            self.registers = _words(self.serial_text, self.power)


def _devices() -> dict[str, Device]:
    return {
        "solarman": Device(
            "solarman",
            f"{LAB_NET}10",
            8899,
            "v5",
            "SOLIS001",
            500,
            logger_serial=1902137671,
            mac="bc54f9f57b5c",
            udp_port=48899,
        ),
        "s2": Device(
            "s2",
            f"{LAB_NET}11",
            502,
            "mbap",
            "S2LOCAL1",
            640,
            http_port=80,
            page="Solis S2-WL-ST logger LAN settings",
        ),
        "gateway": Device(
            "gateway",
            f"{LAB_NET}12",
            502,
            "mbap",
            "GWLOCAL1",
            700,
            http_port=80,
            page="Waveshare RS485 TO ETH status",
        ),
        "blank": Device(
            "blank",
            f"{LAB_NET}16",
            502,
            "mbap",
            "BLANK001",
            550,
            http_port=80,
            page="Device status OK",
        ),
        "rtu": Device("rtu", f"{LAB_NET}13", 4196, "rtu", "RTULOCAL", 800),
        "s5": Device(
            "s5",
            f"{LAB_NET}14",
            502,
            "mbap",
            "S5LOCAL1",
            900,
            http_port=80,
            page="S5-WiFi-ST experimental",
        ),
    }


class Lab:
    """Own every device socket."""

    def __init__(self) -> None:
        self.devices = _devices()
        self._udp: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        for device in self.devices.values():
            await self.listen(device)
        await self._listen_udp()

    async def listen(self, device: Device) -> None:
        await self._close_device(device)
        if device.night or device.mode == "closed":
            return
        device.tcp = await asyncio.start_server(
            lambda reader, writer, item=device: self._tcp(item, reader, writer),
            device.host,
            device.tcp_port,
        )
        if device.http_port and device.page:
            device.http = await asyncio.start_server(
                lambda reader, writer, item=device: self._http(item, reader, writer),
                device.host,
                device.http_port,
            )

    async def _listen_udp(self) -> None:
        if self._udp is not None:
            return
        loop = asyncio.get_running_loop()

        class _Udp(asyncio.DatagramProtocol):
            def __init__(self, lab: Lab) -> None:
                self.lab = lab
                self.transport: asyncio.DatagramTransport | None = None

            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport  # type: ignore[assignment]

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                text = data.decode("utf-8", errors="ignore")
                if "WIFIKIT" not in text and "HF-A11" not in text:
                    return
                device = self.lab.devices["solarman"]
                if device.night or self.transport is None:
                    return
                payload = f"{device.host},{device.mac},{device.logger_serial}".encode()
                self.transport.sendto(payload, addr)

        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _Udp(self), local_addr=("0.0.0.0", 48899)
        )
        self._udp = transport

    async def apply(self, payload: dict[str, object]) -> dict[str, object]:
        name = str(payload.get("device") or "")
        device = self.devices.get(name)
        if device is None:
            return {"ok": False, "error": f"unknown device {name}"}
        rebound = False
        if "host" in payload:
            device.host = str(payload["host"])
            rebound = True
        if "night" in payload:
            device.night = bool(payload["night"])
            rebound = True
        if "mode" in payload:
            device.mode = str(payload["mode"])
            rebound = device.mode == "closed" or rebound
        registers = payload.get("registers")
        if isinstance(registers, dict):
            for address, value in registers.items():
                device.registers[int(address)] = int(value) & 0xFFFF
        if "power" in payload:
            device.registers[3005] = int(payload["power"]) & 0xFFFF
        if rebound or device.mode == "closed":
            await self.listen(device)
        return {"ok": True, "device": name, "host": device.host, "mode": device.mode}

    async def _close_device(self, device: Device) -> None:
        for server in (device.tcp, device.http):
            if server is None:
                continue
            server.close()
            await server.wait_closed()
        device.tcp = None
        device.http = None

    async def _tcp(
        self, device: Device, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if device.mode == "reset":
                await _reset(reader, writer)
                return
            if device.mode == "silent":
                await asyncio.sleep(30)
                return
            if device.framing == "v5":
                await self._v5(device, reader, writer)
            elif device.framing == "rtu":
                await self._rtu(device, reader, writer)
            else:
                await self._mbap(device, reader, writer)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def _v5(
        self, device: Device, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        buffer = b""
        while True:
            frame, buffer = _pop_v5(buffer)
            if frame is None:
                chunk = await asyncio.wait_for(reader.read(512), timeout=3)
                if not chunk:
                    return
                buffer += chunk
                continue
            if logger_serial_of(frame) != device.logger_serial:
                return
            modbus = modbus_from_request(frame)
            reply = _rtu_pdu(device, modbus)
            writer.write(build_response(device.logger_serial, sequence_of(frame), reply))
            await writer.drain()

    async def _mbap(
        self, device: Device, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            header = await asyncio.wait_for(reader.readexactly(6), timeout=3)
            length = int.from_bytes(header[4:6], "big")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=3)
            frame = header + body
            function = frame[7] if len(frame) > 7 else 4
            address = int.from_bytes(frame[8:10], "big") if len(frame) >= 12 else 0
            count = int.from_bytes(frame[10:12], "big") if len(frame) >= 12 else 1
            request = bytes((frame[6], function)) + frame[8:12]
            pdu = _pdu(device, request[0], function, address, count)
            if device.mode == "wrong_slave":
                pdu = _data_pdu(function, [device.registers.get(address, 0)])
                unit = b"\x09"
            else:
                unit = frame[6:7] or b"\x01"
            writer.write(frame[:2] + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + unit + pdu)
            await writer.drain()

    async def _rtu(
        self, device: Device, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            request = await asyncio.wait_for(reader.readexactly(8), timeout=3)
            if device.mode == "echo":
                writer.write(request)
                await writer.drain()
            reply = _rtu_pdu(device, request)
            if device.mode == "wrong_slave":
                address = int.from_bytes(request[2:4], "big")
                reply = _rtu_frame(9, request[1], [device.registers.get(address, 0)])
            writer.write(reply)
            await writer.drain()

    async def _http(
        self, device: Device, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with suppress(TimeoutError, ConnectionError, OSError):
            await asyncio.wait_for(reader.read(1024), timeout=2)
        body = device.page.encode()
        head = (
            b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
        )
        writer.write(head + body)
        await writer.drain()
        writer.close()


def _pop_v5(buffer: bytes) -> tuple[bytes | None, bytes]:
    if len(buffer) < 13 or buffer[0] != 0xA5:
        return None, buffer
    total = 11 + int.from_bytes(buffer[1:3], "little") + 2
    if len(buffer) < total:
        return None, buffer
    return buffer[:total], buffer[total:]


def _pdu(device: Device, slave: int, function: int, address: int, count: int) -> bytes:
    if device.mode == "exception":
        return bytes((function | 0x80, 3))
    if device.mode == "short":
        return _data_pdu(function, [device.registers.get(address, 0)])
    if address >= 33000 or slave not in {0, 1}:
        return bytes((function | 0x80, 2))
    if function not in {3, 4}:
        return bytes((function | 0x80, 1))
    values = [device.registers.get(address + offset, 0) & 0xFFFF for offset in range(count)]
    return _data_pdu(function, values)


def _data_pdu(function: int, values: list[int]) -> bytes:
    body = bytes((function, len(values) * 2))
    for value in values:
        body += (value & 0xFFFF).to_bytes(2, "big")
    return body


def _rtu_frame(slave: int, function: int, values: list[int]) -> bytes:
    body = bytes((slave & 0xFF, function & 0xFF, len(values) * 2))
    for value in values:
        body += (value & 0xFFFF).to_bytes(2, "big")
    return body + crc16(body).to_bytes(2, "little")


def _rtu_pdu(device: Device, request: bytes) -> bytes:
    if len(request) < 6:
        return _exception_frame(1, 4, 1)
    slave = request[0]
    function = request[1]
    address = int.from_bytes(request[2:4], "big")
    count = int.from_bytes(request[4:6], "big")
    if device.mode == "exception":
        return _exception_frame(slave, function, 3)
    if address >= 33000:
        return _exception_frame(slave, function, 2)
    if function not in {3, 4}:
        return _exception_frame(slave, function, 1)
    if device.mode == "short":
        return _rtu_frame(slave, function, [device.registers.get(address, 0)])
    values = [device.registers.get(address + offset, 0) & 0xFFFF for offset in range(count)]
    return _rtu_frame(slave, function, values)


def _exception_frame(slave: int, function: int, code: int) -> bytes:
    body = bytes((slave & 0xFF, function | 0x80, code))
    return body + crc16(body).to_bytes(2, "little")


async def _reset(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with suppress(TimeoutError, ConnectionError, OSError):
        await asyncio.wait_for(reader.read(64), timeout=0.2)
    sock = writer.get_extra_info("socket")
    if isinstance(sock, socket.socket):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


async def _control(lab: Lab, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(65536), timeout=2)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = b"200 OK"
        if head.startswith(b"GET /__health"):
            payload = {"ok": True, "devices": sorted(lab.devices)}
        elif head.startswith(b"POST /__control"):
            parsed = json.loads(body.decode() or "{}")
            payload = await lab.apply(parsed if isinstance(parsed, dict) else {})
            if not payload.get("ok"):
                status = b"404 Not Found"
        else:
            payload = {"ok": False}
            status = b"404 Not Found"
        encoded = json.dumps(payload).encode()
        writer.write(
            b"HTTP/1.0 "
            + status
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(encoded)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + encoded
        )
        await writer.drain()
    except (TimeoutError, ConnectionError, OSError, json.JSONDecodeError):
        return
    finally:
        writer.close()


async def _serve() -> None:
    lab = Lab()
    await lab.start()
    control = await asyncio.start_server(
        lambda reader, writer: _control(lab, reader, writer), "127.0.0.1", CONTROL_PORT
    )
    print(f"dongle lab control http://127.0.0.1:{CONTROL_PORT}", flush=True)
    async with control:
        await control.serve_forever()


if __name__ == "__main__":
    asyncio.run(_serve())
