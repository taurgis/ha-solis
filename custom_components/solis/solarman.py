"""Solarman V5 client, plain Modbus TCP, and UDP discovery.

V5 framing follows pysolarmanv5 (MIT, jmccrohan): start ``0xA5``, little-endian
length, control ``0x4510`` / ``0x1510``, sequence, logger serial, a 15-byte
request prefix or 14-byte response prefix, the Modbus RTU frame, checksum
``sum(bytes[1:]) & 0xFF``, end ``0x15``. The stick echoes the low sequence
byte and uses the high byte as its own counter. The first sequence number is
random, as in pysolarmanv5. Unsolicited frames (handshake ``0x4110``, data
``0x4210``, info ``0x4310``, heartbeat ``0x4710``, report ``0x4810``) get the
time response pysolarmanv5 sends and are otherwise skipped. A V5 response
with no Modbus frame inside is the stick saying the inverter did not answer
on RS485. A wrong serial is silence.

Some DLS-L sticks answer plain Modbus TCP (MBAP, no V5 wrapper) on the same
port 8899, or on 30003. The reader tries V5 first and remembers which
framing answered. A reset after the handshake is a lock, not a reason to
open the other framing in the same breath.

pysolarmanv5 owns a synchronous socket and depends on umodbus, so it is not
imported here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import ipaddress
import logging
import random
import struct
import time
import weakref

from .const import REASON_CLOSED, REASON_TIMEOUT
from .errors import SolisLoggerBusy, SolisLoggerOffline

_LOGGER = logging.getLogger(__name__)

_START = 0xA5
_END = 0x15
_REQ_CONTROL = 0x4510
_RSP_CONTROL = 0x1510
# Low byte of every V5 control word; the high byte is the code.
_CONTROL_SUFFIX = 0x10
# Codes the stick may send on its own. pysolarmanv5 answers each with a time
# frame whose code is the request code minus 0x30.
_UNSOLICITED = frozenset({0x41, 0x42, 0x43, 0x47, 0x48})
# Modbus exceptions from a gateway: path unavailable, target did not answer.
GATEWAY_EXCEPTIONS = frozenset({0x0A, 0x0B})
_DISCOVERY_PORT = 48899
_DISCOVERY_PROBES = (b"WIFIKIT-214028-READ", b"HF-A11ASSISTHREAD")
_HEADER = struct.Struct("<BHHHI")
_MAX_PAYLOAD = 512
_MBAP = struct.Struct(">HHH")
IO_ERRORS = (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, ValueError)

_BUSY_ERRNOS = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ECONNREFUSED,
        errno.ESHUTDOWN,
    }
)
_OFFLINE_ERRNOS = frozenset(
    {
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ENETDOWN,
        errno.EHOSTDOWN,
    }
)


@dataclass(frozen=True, slots=True)
class Stick:
    """One logger found by UDP discovery."""

    host: str
    mac: str
    serial: int


class ModbusException(Exception):
    """The inverter returned a Modbus exception code."""

    def __init__(self, code: int) -> None:
        """Store the exception code. ``2`` means the register is not in the map."""
        super().__init__(f"modbus exception {code}")
        self.code = code


def no_rs485_answer(err: ModbusException) -> SolisLoggerOffline | None:
    """Exceptions 0x0A and 0x0B are a gateway whose RS485 side got no reply.

    That is the same as a timeout behind the stick (the inverter is dark or
    the slave id is wrong), not a register the inverter rejected.
    """
    if err.code not in GATEWAY_EXCEPTIONS:
        return None
    return SolisLoggerOffline(
        f"gateway returned Modbus exception {err.code}: the inverter did not answer",
        connected=True,
        reason=REASON_TIMEOUT,
    )


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


def build_time_response(logger_serial: int, frame: bytes, now: float | None = None) -> bytes:
    """Answer an unsolicited V5 frame the way pysolarmanv5 does.

    The code is the request code minus 0x30 (``0x4710`` becomes ``0x1710``),
    the sequence is the request's with the low byte plus one, and the payload
    is ``0x0100``, the Unix time and a zero offset.
    """
    code = (frame[4] - 0x30) & 0xFF
    sequence = bytes((((frame[5] + 1) & 0xFF), frame[6]))
    payload = struct.pack("<HII", 0x0100, int(time.time() if now is None else now), 0)
    header = (
        bytes((_START,))
        + struct.pack("<H", len(payload))
        + bytes((_CONTROL_SUFFIX, code))
        + sequence
        + struct.pack("<I", logger_serial & 0xFFFFFFFF)
    )
    body = header + payload
    return body + bytes((sum(body[1:]) & 0xFF, _END))


def build_mbap(transaction: int, slave: int, address: int, count: int, function: int = 4) -> bytes:
    """Build a Modbus TCP read. There is no CRC. Function 4 is the input map."""
    pdu = struct.pack(">BHH", function & 0xFF, address & 0xFFFF, count & 0xFFFF)
    header = _MBAP.pack(transaction & 0xFFFF, 0, len(pdu) + 1)
    return header + bytes((slave & 0xFF,)) + pdu


def take_frame(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Pop one V5 frame. Junk before ``0xA5`` and an illegal length are dropped."""
    while True:
        start = buffer.find(_START)
        if start < 0:
            return None, b""
        if start:
            buffer = buffer[start:]
        if len(buffer) < 3:
            return None, buffer
        payload_len = int.from_bytes(buffer[1:3], "little")
        if payload_len < 1 or payload_len > _MAX_PAYLOAD:
            buffer = buffer[1:]
            continue
        total = 11 + payload_len + 2
        if len(buffer) < total:
            return None, buffer
        if buffer[total - 1] != _END:
            buffer = buffer[1:]
            continue
        return buffer[:total], buffer[total:]


def modbus_from_request(frame: bytes) -> bytes:
    """Return the Modbus RTU request. It starts after the 15-byte V5 prefix."""
    if len(frame) < 28 or frame[0] != _START:
        raise ValueError("not a solarman request")
    payload_len = int.from_bytes(frame[1:3], "little")
    if payload_len > _MAX_PAYLOAD:
        raise ValueError("solarman request length is not plausible")
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
    if payload_len > _MAX_PAYLOAD:
        raise ValueError("solarman length is not plausible")
    expected = 11 + payload_len + 2
    if len(frame) < expected:
        raise ValueError("truncated solarman frame")
    frame = frame[:expected]
    if (sum(frame[1:-2]) & 0xFF) != frame[-2]:
        raise ValueError("solarman checksum mismatch")
    if payload_len < 14:
        raise ValueError("solarman payload has no modbus frame")
    return frame[25:-2]


def _modbus_matches(modbus: bytes, slave: int, count: int) -> bool:
    """A reply for this request: same slave, function 4, and the byte count asked for."""
    if len(modbus) < 3 or modbus[0] != (slave & 0xFF):
        return False
    if modbus[1] == 0x84:
        return True
    return modbus[1] == 0x04 and modbus[2] == count * 2


def logger_serial_of(frame: bytes) -> int:
    """Return the logger serial carried in a V5 header."""
    if len(frame) < 11:
        raise ValueError("header too short")
    return int.from_bytes(frame[7:11], "little")


def sequence_of(frame: bytes) -> int:
    """Return the sequence field."""
    return int.from_bytes(frame[5:7], "little")


def sequence_matches(frame: bytes, sequence: int) -> bool:
    """Return whether the stick echoed our sequence.

    pysolarmanv5 compares byte 5 only. The high byte is the stick's own
    counter and changes on every reply, including the captured frame
    ``b1 17``.
    """
    return len(frame) > 5 and frame[5] == (sequence & 0xFF)


def decode_read(modbus: bytes) -> list[int]:
    """Decode a function-4 response or raise ``ModbusException``."""
    if len(modbus) < 5:
        raise ValueError("modbus frame too short")
    function = modbus[1]
    if function & 0x80:
        body = modbus[:3]
        crc = int.from_bytes(modbus[3:5], "little")
        if crc16(body) != crc:
            raise ValueError("modbus crc mismatch")
        raise ModbusException(modbus[2])
    if function != 0x04:
        raise ValueError(f"unexpected function {function}")
    count = modbus[2]
    data = modbus[3 : 3 + count]
    if len(data) != count or count % 2:
        raise ValueError("modbus byte count does not match")
    body = modbus[: 3 + count]
    crc = int.from_bytes(modbus[3 + count : 5 + count], "little")
    if crc16(body) != crc:
        raise ValueError("modbus crc mismatch")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, count, 2)]


def decode_mbap(frame: bytes) -> list[int]:
    """Decode a Modbus TCP function-4 response."""
    if len(frame) < 9:
        raise ValueError("mbap frame too short")
    _transaction, protocol, length = _MBAP.unpack(frame[:6])
    if protocol != 0 or length < 3 or 6 + length > len(frame):
        raise ValueError("mbap header is not a read response")
    pdu = frame[7 : 6 + length]
    function = pdu[0]
    if function & 0x80:
        if len(pdu) < 2:
            raise ValueError("mbap exception is truncated")
        raise ModbusException(pdu[1])
    if function not in (0x03, 0x04) or len(pdu) < 2:
        raise ValueError(f"unexpected function {function}")
    count = pdu[1]
    data = pdu[2 : 2 + count]
    if len(data) != count or count % 2:
        raise ValueError("mbap byte count does not match")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, count, 2)]


def format_mac(value: str) -> str:
    """Return ``bc:54:f9:f5:7b:5c`` from a colon-free or dashed MAC."""
    hexed = value.replace(":", "").replace("-", "").strip().lower()
    if len(hexed) != 12:
        return value.strip().lower()
    return ":".join(hexed[i : i + 2] for i in range(0, 12, 2))


def _is_lan_address(host: str) -> bool:
    """A discovery reply may name a private or loopback address, never a public one."""
    address = ipaddress.ip_address(host)
    return (address.is_private or address.is_loopback) and not (
        address.is_multicast or address.is_unspecified
    )


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
    try:
        host = str(ipaddress.ip_address(parts[0]))
    except ValueError:
        return None
    return Stick(host=host, mac=format_mac(parts[1]), serial=serial)


def _is_refused(err: BaseException) -> bool:
    if isinstance(err, ConnectionRefusedError):
        return True
    number = getattr(err, "errno", None)
    if number == errno.ECONNREFUSED:
        return True
    cause = err.__cause__
    return cause is not None and cause is not err and _is_refused(cause)


def _is_busy(err: BaseException) -> bool:
    """A reset after the handshake. A refused port is not this: the host may have moved."""
    if isinstance(err, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    number = getattr(err, "errno", None)
    if isinstance(number, int) and number in _BUSY_ERRNOS and number != errno.ECONNREFUSED:
        return True
    cause = err.__cause__
    return cause is not None and cause is not err and _is_busy(cause)


def _is_unreachable(err: BaseException) -> bool:
    number = getattr(err, "errno", None)
    if isinstance(number, int) and number in _OFFLINE_ERRNOS:
        return True
    cause = err.__cause__
    return cause is not None and cause is not err and _is_unreachable(cause)


_GATES: dict[tuple[str, int], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_DEPTH: dict[tuple[str, int], tuple[asyncio.Task[object] | None, int]] = {}


class _HostSlot:
    """One stick, one socket. The task that owns the slot may enter it again."""

    def __init__(self, host: str, port: int) -> None:
        self._key = (host, port)
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> _HostSlot:
        task = asyncio.current_task()
        holder, depth = _DEPTH.get(self._key, (None, 0))
        if task is not None and holder is task:
            _DEPTH[self._key] = (task, depth + 1)
            return self
        loop = asyncio.get_running_loop()
        current = _GATES.get(self._key)
        if current is None or current[0] is not loop:
            lock = asyncio.Lock()
            _GATES[self._key] = (loop, lock)
            _DEPTH.pop(self._key, None)
        else:
            lock = current[1]
        await lock.acquire()
        _DEPTH[self._key] = (task, 1)
        self._lock = lock
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        _holder, depth = _DEPTH.get(self._key, (None, 0))
        if depth > 1:
            _DEPTH[self._key] = (_holder, depth - 1)
            return
        _DEPTH.pop(self._key, None)
        if self._lock is not None:
            self._lock.release()
            self._lock = None


def host_gate(host: str, port: int) -> _HostSlot:
    """Serialize every client that talks to this host and port."""
    return _HostSlot(host, port)


def classify_io(err: BaseException, *, connected: bool) -> SolisLoggerOffline | SolisLoggerBusy:
    """Turn a socket error into a lock or an asleep logger."""
    if isinstance(err, SolisLoggerBusy):
        return err
    if isinstance(err, SolisLoggerOffline):
        return err
    if isinstance(err, TimeoutError) or _is_unreachable(err):
        return SolisLoggerOffline("logger did not answer", connected=connected, reason="timeout")
    if _is_refused(err):
        return SolisLoggerOffline(
            "logger refused the port", connected=False, refused=True, reason="refused"
        )
    if _is_busy(err):
        return SolisLoggerBusy("logger reset or refused the session")
    if isinstance(err, (ConnectionError, OSError, asyncio.IncompleteReadError)):
        return SolisLoggerOffline(
            "logger closed the connection", connected=connected, reason="closed"
        )
    if isinstance(err, ValueError):
        return SolisLoggerOffline(
            "logger sent a frame that is not Modbus", connected=connected, reason="garbage"
        )
    raise err


class _PacedClient:
    """One TCP session. Requests on this object are serialized and spaced."""

    def __init__(self, host: str, *, port: int, timeout: float, gap: float) -> None:
        self.host = host
        self._port = port
        self._timeout = timeout
        self._gap = gap
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._sent = False
        _LIVE.add(self)

    @property
    def port(self) -> int:
        """TCP port this client connects to."""
        return self._port

    @property
    def connected(self) -> bool:
        """True while a socket from an earlier read is open."""
        return self._writer is not None

    async def close(self) -> None:
        """Close the socket if it is open."""
        writer = self._writer
        self._reader = None
        self._writer = None
        self._sent = False
        if writer is None:
            return
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=min(self._timeout, 2.0))
        except (ConnectionError, OSError, TimeoutError, RuntimeError):
            return

    async def _pace(self) -> None:
        if self._sent and self._gap > 0:
            await asyncio.sleep(self._gap)
        self._sent = True

    async def _ensure(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._writer is not None and self._reader is not None:
            return self._reader, self._writer
        await _release_other_sockets(self)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self._port),
                timeout=self._timeout,
            )
        except (TimeoutError, ConnectionError, OSError) as err:
            raise classify_io(err, connected=False) from err
        self._reader = reader
        self._writer = writer
        return reader, writer

    async def _send(self, writer: asyncio.StreamWriter, payload: bytes) -> None:
        try:
            writer.write(payload)
            # A peer that never reads must not leave this task parked on drain.
            await asyncio.wait_for(writer.drain(), timeout=self._timeout)
        except (TimeoutError, ConnectionError, OSError) as err:
            raise classify_io(err, connected=True) from err


_LIVE: weakref.WeakSet[_PacedClient] = weakref.WeakSet()


async def _release_other_sockets(client: _PacedClient) -> None:
    """Close idle clients on this stick before opening another socket."""
    others = [
        item
        for item in list(_LIVE)
        if item is not client and item.host == client.host and item.port == client.port
    ]
    for item in others:
        await item.close()


class SolarmanClient(_PacedClient):
    """One V5 session. Unsolicited frames are answered and skipped. One request is in flight."""

    def __init__(
        self,
        host: str,
        logger_serial: int,
        *,
        port: int = 8899,
        timeout: float = 8.0,
        gap: float = 1.0,
    ) -> None:
        """Store the logger address. The socket opens on the first read."""
        super().__init__(host, port=port, timeout=timeout, gap=gap)
        self.serial = int(logger_serial)
        # pysolarmanv5 starts at a random byte, so a stick that still holds the
        # previous session's last sequence does not take a stale reply as ours.
        self._sequence = random.randrange(0x01, 0xFF)
        self._buffer = b""

    async def close(self) -> None:
        """Close the socket and drop any partial frame."""
        self._buffer = b""
        await super().close()

    async def read_input(self, slave: int, address: int, count: int) -> list[int]:
        """Read input registers. A reset becomes ``SolisLoggerBusy``.

        A socket kept from an earlier poll that fails on its first exchange
        (the stick dropped an idle session) is reopened once before the
        failure counts.
        """
        async with host_gate(self.host, self._port), self._lock:
            await self._pace()
            reused = self.connected
            try:
                words = await self._exchange(slave, address, count)
            except (SolisLoggerBusy, SolisLoggerOffline) as err:
                if not reused or not _stale_socket(err):
                    raise
                _LOGGER.debug("V5 socket to %s was stale; reconnecting once", self.host)
                words = await self._exchange(slave, address, count)
            except ModbusException as err:
                gateway = no_rs485_answer(err)
                if gateway is not None:
                    raise gateway from err
                raise
            return words

    async def _exchange(self, slave: int, address: int, count: int) -> list[int]:
        reader, writer = await self._ensure()
        # Like pysolarmanv5: an 8-bit counter, sent with a zero high byte.
        sequence = self._sequence & 0xFF
        self._sequence = (self._sequence + 1) & 0xFF
        request = build_request(self.serial, sequence, rtu_read(slave, address, count))
        try:
            await self._send(writer, request)
            modbus = await self._read_matching(reader, writer, sequence, slave, count)
            words = decode_read(modbus)
            if len(words) != count:
                raise ValueError("solarman register count does not match")
        except (SolisLoggerBusy, SolisLoggerOffline):
            await self.close()
            raise
        except ModbusException:
            raise
        except IO_ERRORS as err:
            await self.close()
            raise classify_io(err, connected=True) from err
        return words

    async def _read_matching(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        sequence: int,
        slave: int,
        count: int,
    ) -> bytes:
        """Return the Modbus frame that answers this request.

        One deadline covers every frame read, so a stick that keeps sending
        heartbeats cannot stretch the wait. Frames for another sequence,
        logger, slave or register count are skipped.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            frame = await self._read_one(reader, deadline)
            control = int.from_bytes(frame[3:5], "little")
            if frame[3] == _CONTROL_SUFFIX and frame[4] in _UNSOLICITED:
                await self._answer_unsolicited(writer, frame)
                continue
            if control != _RSP_CONTROL or not sequence_matches(frame, sequence):
                continue
            if logger_serial_of(frame) != self.serial:
                continue
            if len(frame) > 11 and frame[11] != 0x02:
                # Frame type 2 is the solar inverter; pysolarmanv5 rejects others.
                continue
            if (sum(frame[1:-2]) & 0xFF) != frame[-2]:
                raise ValueError("solarman checksum mismatch")
            modbus = frame[25:-2]
            if len(modbus) < 5:
                # The stick wrapped no Modbus reply: nothing came back on RS485.
                raise SolisLoggerOffline(
                    "logger answered but the inverter did not",
                    connected=True,
                    reason=REASON_TIMEOUT,
                )
            if not _modbus_matches(modbus, slave, count):
                continue
            return modbus

    async def _answer_unsolicited(self, writer: asyncio.StreamWriter, frame: bytes) -> None:
        _LOGGER.debug("V5 control 0x%02x%02x from %s answered", frame[4], frame[3], self.host)
        try:
            writer.write(build_time_response(self.serial, frame))
        except (ConnectionError, OSError, RuntimeError) as err:
            raise classify_io(err, connected=True) from err

    async def _read_one(self, reader: asyncio.StreamReader, deadline: float) -> bytes:
        loop = asyncio.get_running_loop()
        while True:
            frame, self._buffer = take_frame(self._buffer)
            if frame is not None:
                return frame
            remaining = deadline - loop.time()
            try:
                if remaining <= 0:
                    raise TimeoutError
                chunk = await asyncio.wait_for(reader.read(512), timeout=remaining)
            except (TimeoutError, ConnectionError, OSError) as err:
                raise classify_io(err, connected=True) from err
            if not chunk:
                raise SolisLoggerOffline(
                    "logger closed the connection", connected=True, reason=REASON_CLOSED
                )
            if len(self._buffer) > _MAX_PAYLOAD:
                self._buffer = self._buffer[-_MAX_PAYLOAD:]
            self._buffer += chunk


def _stale_socket(err: SolisLoggerBusy | SolisLoggerOffline) -> bool:
    """An idle socket the stick already dropped: EOF or a reset on the first send."""
    if isinstance(err, SolisLoggerBusy):
        return not err.waiting
    return err.connected and err.reason == REASON_CLOSED


class ModbusTcpClient(_PacedClient):
    """Plain Modbus TCP. One request is in flight.

    Function 4 is the Solis input map. An S2-WL-ST that only answers function
    3 (the official Modbus Poll demo) is retried once when ``fallback_function``
    is set. Illegal-function is exception 1. Any other exception is the inverter.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 8899,
        timeout: float = 8.0,
        gap: float = 1.0,
        function: int = 4,
        fallback_function: int = 0,
    ) -> None:
        """Store the address. The socket opens on the first read."""
        super().__init__(host, port=port, timeout=timeout, gap=gap)
        self.function = function if function in (3, 4) else 4
        self._fallback = fallback_function if fallback_function in (3, 4) else 0
        self._transaction = 1
        # Transactions whose reply timed out on a socket that was kept open.
        self._late: set[int] = set()
        self.late_reply_pending = False

    async def close(self) -> None:
        """Close the socket and forget replies that were still owed on it."""
        self._late.clear()
        self.late_reply_pending = False
        await super().close()

    async def read_input(self, slave: int, address: int, count: int) -> list[int]:
        """Read input registers over MBAP."""
        async with host_gate(self.host, self._port), self._lock:
            await self._pace()
            try:
                try:
                    return await self._exchange(slave, address, count)
                except ModbusException as err:
                    if err.code != 1 or self._fallback == 0 or self.function == self._fallback:
                        raise
                    self.function = self._fallback
                    self._fallback = 0
                    await self._pace()
                    return await self._exchange(slave, address, count)
            except ModbusException as err:
                gateway = no_rs485_answer(err)
                if gateway is not None:
                    raise gateway from err
                raise

    async def _exchange(self, slave: int, address: int, count: int) -> list[int]:
        reader, writer = await self._ensure()
        transaction = self._transaction & 0xFFFF
        self._transaction = (self._transaction + 1) & 0xFFFF
        request = build_mbap(transaction, slave, address, count, self.function)
        try:
            await self._send(writer, request)
            frame = await self._read_reply(reader, transaction)
        except _HeaderTimeout as err:
            if self.late_reply_pending:
                # Two timeouts in a row: the socket is probably half-open.
                await self.close()
            else:
                # Nothing of the reply arrived, so the stream is still frame-aligned.
                # Closing would cost a reconnect, and on an S2-WL-ST a close can
                # wedge port 502 for minutes. A late reply is skipped next time.
                self._late.add(transaction)
                self.late_reply_pending = True
            raise classify_io(TimeoutError(), connected=True) from err
        except (SolisLoggerBusy, SolisLoggerOffline):
            await self.close()
            raise
        except IO_ERRORS as err:
            await self.close()
            raise classify_io(err, connected=True) from err
        self._late.clear()
        self.late_reply_pending = False
        if int.from_bytes(frame[:2], "big") != transaction:
            await self.close()
            raise SolisLoggerOffline(
                "modbus tcp transaction did not match", connected=True, reason="garbage"
            )
        if len(frame) < 7 or frame[6] != (slave & 0xFF):
            await self.close()
            raise SolisLoggerOffline(
                "modbus tcp unit id did not match",
                connected=True,
                reason="wrong_slave",
            )
        try:
            words = decode_mbap(frame)
        except ValueError as err:
            await self.close()
            raise SolisLoggerOffline(
                "modbus tcp reply is not a valid read response",
                connected=True,
                reason="garbage",
            ) from err
        if len(words) != count:
            await self.close()
            raise SolisLoggerOffline(
                "modbus tcp register count did not match",
                connected=True,
                reason="garbage",
            )
        return words

    async def _read_reply(self, reader: asyncio.StreamReader, transaction: int) -> bytes:
        """Read this transaction's reply, skipping late replies to timed-out ones."""
        for _ in range(len(self._late) + 1):
            frame = await self._read_response(reader)
            received = int.from_bytes(frame[:2], "big")
            if received == transaction or received not in self._late:
                return frame
            self._late.discard(received)
        return frame

    async def _read_response(self, reader: asyncio.StreamReader) -> bytes:
        try:
            # readexactly takes nothing from the buffer until all six bytes are
            # there, so a timeout here leaves the stream on a frame boundary.
            header = await asyncio.wait_for(reader.readexactly(6), timeout=self._timeout)
        except TimeoutError as err:
            raise _HeaderTimeout from err
        except asyncio.IncompleteReadError as err:
            raise SolisLoggerOffline(
                "logger closed the connection", connected=True, reason="closed"
            ) from err
        except (ConnectionError, OSError) as err:
            raise classify_io(err, connected=True) from err
        try:
            length = int.from_bytes(header[4:6], "big")
            if length < 2 or length > _MAX_PAYLOAD:
                raise ValueError("mbap length is not plausible")
            body = await asyncio.wait_for(reader.readexactly(length), timeout=self._timeout)
        except asyncio.IncompleteReadError as err:
            raise SolisLoggerOffline(
                "logger closed the connection", connected=True, reason="closed"
            ) from err
        except (TimeoutError, ConnectionError, OSError) as err:
            raise classify_io(err, connected=True) from err
        return header + body


class _HeaderTimeout(Exception):
    """No byte of an MBAP reply arrived in time; the stream is still aligned."""


def _local_target(host: str) -> bool:
    if host in {"255.255.255.255", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or int(address) == 0xFFFFFFFF
    )


async def discover_sticks(
    *,
    timeout: float = 2.0,
    extra_targets: tuple[str, ...] = ("127.0.0.1",),
) -> list[Stick]:
    """Broadcast the IGEN discovery probes and collect ``ip,mac,serial`` replies.

    Probes go to the limited broadcast and to loopback or private addresses
    passed in. They are not sent to public hosts.
    """
    loop = asyncio.get_running_loop()
    found: dict[int, Stick] = {}

    class _Collector(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            stick = parse_discovery(data)
            # A stick names its own address. A reply that names another host is
            # someone else steering the entry there.
            if stick is not None and stick.host == addr[0] and _is_lan_address(stick.host):
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
        allowed = [item for item in extra_targets if _local_target(item)]
        targets = tuple(dict.fromkeys(("255.255.255.255", *allowed)))
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
