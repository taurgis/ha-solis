"""Solarman V5 client and UDP discovery.

The frame layout matches pysolarmanv5 (MIT, jmccrohan) and the stick on this
LAN: start ``0xA5``, little-endian length, control ``0x4510`` / ``0x1510``,
sequence, logger serial, then a 15-byte request prefix (or 14-byte response
prefix) and a Modbus RTU frame. The trailer is ``sum(bytes[1:]) & 0xFF`` and
``0x15``. A wrong serial is silence, which is what the real stick does.

pysolarmanv5 3.0.6 is maintained and supports Python 3.13, but it depends on
the synchronous umodbus package and owns the socket. This client keeps one
connection, paces requests, and can be pointed at the lab emulator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import struct

from .errors import SolisLoggerOffline

_LOGGER = logging.getLogger(__name__)

_START = 0xA5
_END = 0x15
_REQ_CONTROL = 0x4510
_RSP_CONTROL = 0x1510
_DISCOVERY_PORT = 48899
_DISCOVERY_PROBES = (b"WIFIKIT-214028-READ", b"HF-A11ASSISTHREAD")
_HEADER = struct.Struct("<BHHHI")


@dataclass(frozen=True, slots=True)
class Stick:
    """One logger found by UDP discovery."""

    host: str
    mac: str
    serial: int


class ModbusException(Exception):
    """The inverter returned a Modbus exception code."""

    def __init__(self, code: int) -> None:
        """Store the exception code. ``2`` means the register map is wrong."""
        super().__init__(f"modbus exception {code}")
        self.code = code


def crc16(data: bytes) -> int:
    """Return the Modbus RTU CRC16 of ``data``."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def rtu_read(slave: int, address: int, count: int) -> bytes:
    """Build a function-4 read of input registers."""
    body = struct.pack(">BBHH", slave & 0xFF, 0x04, address & 0xFFFF, count & 0xFFFF)
    return body + struct.pack("<H", crc16(body))


def build_request(logger_serial: int, sequence: int, modbus: bytes) -> bytes:
    """Wrap a Modbus RTU frame in a Solarman V5 request."""
    prefix = bytes((0x02, 0x00, 0x00)) + bytes(12)
    payload = prefix + modbus
    header = _HEADER.pack(_START, len(payload), _REQ_CONTROL, sequence & 0xFFFF, logger_serial)
    body = header + payload
    return body + bytes((sum(body[1:]) & 0xFF, _END))


def build_response(logger_serial: int, sequence: int, modbus: bytes) -> bytes:
    """Wrap a Modbus RTU frame in a Solarman V5 response."""
    prefix = bytes((0x02, 0x01)) + bytes(12)
    payload = prefix + modbus
    header = _HEADER.pack(_START, len(payload), _RSP_CONTROL, sequence & 0xFFFF, logger_serial)
    body = header + payload
    return body + bytes((sum(body[1:]) & 0xFF, _END))


def modbus_from_request(frame: bytes) -> bytes:
    """Return the Modbus RTU request. It starts after the 15-byte V5 prefix."""
    if len(frame) < 28 or frame[0] != _START:
        raise ValueError("not a solarman request")
    payload_len = int.from_bytes(frame[1:3], "little")
    end = 11 + payload_len
    if len(frame) < end:
        raise ValueError("truncated solarman request")
    return frame[26:end]


def modbus_from_frame(frame: bytes) -> bytes:
    """Return the Modbus RTU bytes inside one V5 frame.

    Responses start the Modbus PDU at byte 25 and end before the trailer.
    """
    if len(frame) < 13 or frame[0] != _START or frame[-1] != _END:
        raise ValueError("not a solarman frame")
    payload_len = int.from_bytes(frame[1:3], "little")
    expected = 11 + payload_len + 2
    if len(frame) < expected:
        raise ValueError("truncated solarman frame")
    frame = frame[:expected]
    if (sum(frame[1:-2]) & 0xFF) != frame[-2]:
        raise ValueError("solarman checksum mismatch")
    if payload_len < 14:
        raise ValueError("solarman payload has no modbus frame")
    return frame[25:-2]


def logger_serial_of(frame: bytes) -> int:
    """Return the logger serial carried in a V5 header."""
    if len(frame) < 11:
        raise ValueError("header too short")
    return int.from_bytes(frame[7:11], "little")


def sequence_of(frame: bytes) -> int:
    """Return the sequence field."""
    return int.from_bytes(frame[5:7], "little")


def decode_read(modbus: bytes) -> list[int]:
    """Decode a function-4 response or raise ``ModbusException``."""
    if len(modbus) < 5:
        raise ValueError("modbus frame too short")
    function = modbus[1]
    if function & 0x80:
        raise ModbusException(modbus[2])
    if function != 0x04:
        raise ValueError(f"unexpected function {function}")
    count = modbus[2]
    data = modbus[3 : 3 + count]
    if len(data) != count:
        raise ValueError("modbus byte count does not match")
    body = modbus[: 3 + count]
    crc = int.from_bytes(modbus[3 + count : 5 + count], "little")
    if crc16(body) != crc:
        raise ValueError("modbus crc mismatch")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, count, 2)]


def format_mac(value: str) -> str:
    """Return ``bc:54:f9:f5:7b:5c`` from a colon-free or dashed MAC."""
    hexed = value.replace(":", "").replace("-", "").strip().lower()
    if len(hexed) != 12:
        return value.strip().lower()
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def parse_discovery(payload: bytes) -> Stick | None:
    """Parse ``ip,mac,serial`` from a discovery reply."""
    text = payload.decode("utf-8", errors="ignore").strip().strip("\x00")
    parts = [part.strip() for part in text.split(",")]
    if len(parts) < 3:
        return None
    try:
        serial = int(parts[2])
    except ValueError:
        return None
    return Stick(host=parts[0], mac=format_mac(parts[1]), serial=serial)


class SolarmanClient:
    """One TCP session to a logger. Requests are serialized and paced."""

    def __init__(
        self,
        host: str,
        logger_serial: int,
        *,
        port: int = 8899,
        timeout: float = 8.0,
        gap: float = 0.35,
    ) -> None:
        """Store the logger address. The socket opens on the first read."""
        self.host = host
        self.serial = int(logger_serial)
        self._port = port
        self._timeout = timeout
        self._gap = gap
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._sequence = 1
        self._sent = False

    async def close(self) -> None:
        """Close the socket if it is open."""
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                return

    async def read_input(self, slave: int, address: int, count: int) -> list[int]:
        """Read input registers. A dead socket becomes ``SolisLoggerOffline``."""
        async with self._lock:
            if self._sent and self._gap > 0:
                await asyncio.sleep(self._gap)
            self._sent = True
            await self._ensure()
            sequence = self._sequence & 0xFFFF
            self._sequence = (self._sequence + 1) & 0xFFFF
            request = build_request(self.serial, sequence, rtu_read(slave, address, count))
            writer = self._writer
            reader = self._reader
            if writer is None or reader is None:
                raise SolisLoggerOffline(f"{self.host} has no socket")
            try:
                writer.write(request)
                await writer.drain()
                frame = await self._read_frame(reader)
            except (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError) as err:
                await self.close()
                raise SolisLoggerOffline(f"{self.host} did not answer") from err
            if sequence_of(frame) != sequence:
                raise SolisLoggerOffline(f"{self.host} echoed the wrong sequence")
            return decode_read(modbus_from_frame(frame))

    async def _ensure(self) -> None:
        if self._writer is not None:
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self._port),
                timeout=self._timeout,
            )
        except (TimeoutError, ConnectionError, OSError) as err:
            raise SolisLoggerOffline(f"cannot connect to {self.host}:{self._port}") from err
        self._reader = reader
        self._writer = writer

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        buffer = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(512), timeout=self._timeout)
            if not chunk:
                raise ConnectionError("logger closed the connection")
            buffer += chunk
            if len(buffer) >= 13 and buffer[0] == _START:
                payload_len = int.from_bytes(buffer[1:3], "little")
                total = 11 + payload_len + 2
                if len(buffer) >= total:
                    return buffer[:total]


async def discover_sticks(
    *,
    timeout: float = 2.0,
    extra_targets: tuple[str, ...] = ("127.0.0.1",),
) -> list[Stick]:
    """Broadcast the IGEN discovery probes and collect ``ip,mac,serial`` replies."""
    loop = asyncio.get_running_loop()
    found: dict[int, Stick] = {}

    class _Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            stick = parse_discovery(data)
            if stick is not None:
                found[stick.serial] = stick

        def error_received(self, exc: Exception) -> None:
            _LOGGER.debug("Solarman discovery error: %s", exc)

    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            _Collector,
            local_addr=("0.0.0.0", 0),
            allow_broadcast=True,
        )
    except OSError as err:
        _LOGGER.debug("Solarman discovery socket failed: %s", err)
        return []
    try:
        targets = ("255.255.255.255", *extra_targets)
        for probe in _DISCOVERY_PROBES:
            for target in targets:
                try:
                    transport.sendto(probe, (target, _DISCOVERY_PORT))
                except OSError:
                    continue
        await asyncio.sleep(timeout)
    finally:
        transport.close()
    return list(found.values())
