"""Modbus RTU on a TCP stream, with no MBAP header.

Transparent Elfin, USR and Waveshare modes copy the RS485 bytes onto the
socket. One request is in flight. Function 4 is first. Exception 1 retries
function 3 once, which is the Solis Modbus Poll demo.
"""

from __future__ import annotations

import asyncio
import struct

from ..const import REASON_CLOSED, REASON_WRONG_PROTOCOL, REASON_WRONG_SLAVE
from ..errors import SolisLoggerBusy, SolisLoggerOffline
from ..solarman import ModbusException, _PacedClient, classify_io, crc16, host_gate

_IO_ERRORS = (TimeoutError, ConnectionError, OSError, asyncio.IncompleteReadError, ValueError)


def rtu_request(slave: int, function: int, address: int, count: int) -> bytes:
    """Build a function-3 or function-4 RTU read, CRC included."""
    body = struct.pack(">BBHH", slave & 0xFF, function & 0xFF, address & 0xFFFF, count & 0xFFFF)
    return body + struct.pack("<H", crc16(body))


def take_rtu(buffer: bytes) -> tuple[bytes | None, bytes]:
    """Pop one RTU frame.

    An incomplete but plausible frame returns the same buffer object so the
    caller can wait for more bytes. An illegal byte count drops the first
    byte and returns a new buffer.
    """
    if len(buffer) < 5:
        return None, buffer
    if buffer[1] & 0x80:
        return buffer[:5], buffer[5:]
    count = buffer[2]
    if count < 2 or count > 250 or count % 2:
        return None, buffer[1:]
    total = 3 + count + 2
    if len(buffer) < total:
        return None, buffer
    return buffer[:total], buffer[total:]


def decode_rtu(frame: bytes, function: int) -> list[int]:
    """Decode a function-3 or function-4 RTU response, or raise."""
    if len(frame) < 5:
        raise ValueError("rtu frame too short")
    if frame[1] & 0x80:
        body = frame[:3]
        crc = int.from_bytes(frame[3:5], "little")
        if crc16(body) != crc:
            raise ValueError("rtu crc mismatch")
        raise ModbusException(frame[2])
    if frame[1] != (function & 0xFF):
        raise ValueError(f"unexpected function {frame[1]}")
    count = frame[2]
    data = frame[3 : 3 + count]
    if len(data) != count or count % 2:
        raise ValueError("rtu byte count does not match")
    body = frame[: 3 + count]
    crc = int.from_bytes(frame[3 + count : 5 + count], "little")
    if crc16(body) != crc:
        raise ValueError("rtu crc mismatch")
    return [int.from_bytes(data[index : index + 2], "big") for index in range(0, count, 2)]


class RtuTcpClient(_PacedClient):
    """One transparent TCP session. The slave byte has to match the request."""

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
        """Store the gateway address. The socket opens on the first read."""
        super().__init__(host, port=port, timeout=timeout, gap=gap)
        self.function = function if function in (3, 4) else 4
        self._fallback = fallback_function if fallback_function in (3, 4) else 0
        self._buffer = b""

    async def close(self) -> None:
        """Close the socket and drop a partial frame."""
        self._buffer = b""
        await super().close()

    async def read_input(self, slave: int, address: int, count: int) -> list[int]:
        """Read input registers. Exception 1 retries the fallback function once."""
        async with host_gate(self.host, self._port), self._lock:
            await self._pace()
            try:
                return await self._exchange(slave, address, count, self.function)
            except ModbusException as err:
                if err.code != 1 or self._fallback == 0 or self.function == self._fallback:
                    raise
                self.function = self._fallback
                self._fallback = 0
                await self._pace()
                return await self._exchange(slave, address, count, self.function)

    async def _exchange(self, slave: int, address: int, count: int, function: int) -> list[int]:
        reader, writer = await self._ensure()
        request = rtu_request(slave, function, address, count)
        # Bytes left from the previous reply are not this reply. A transparent
        # bridge often echoes the request; that echo is not a register read.
        self._buffer = b""
        try:
            await self._send(writer, request)
            frame = await self._read_frame(reader, echo=request)
        except (SolisLoggerBusy, SolisLoggerOffline):
            await self.close()
            raise
        except _IO_ERRORS as err:
            await self.close()
            raise classify_io(err, connected=True) from err
        if frame[0] != (slave & 0xFF):
            await self.close()
            raise SolisLoggerOffline(
                "modbus rtu slave did not match",
                connected=True,
                reason=REASON_WRONG_SLAVE,
            )
        try:
            words = decode_rtu(frame, function)
            if len(words) != count:
                raise ValueError("rtu register count does not match")
            return words
        except ModbusException:
            raise
        except ValueError as err:
            await self.close()
            raise SolisLoggerOffline(
                "logger sent a frame that is not Modbus RTU",
                connected=True,
                reason=REASON_WRONG_PROTOCOL,
            ) from err

    async def _read_frame(self, reader: asyncio.StreamReader, *, echo: bytes = b"") -> bytes:
        while True:
            if echo and (self._buffer.startswith(echo) or echo.startswith(self._buffer)):
                if len(self._buffer) < len(echo):
                    self._buffer += await self._read_chunk(reader)
                    continue
                self._buffer = self._buffer[len(echo) :]
                continue
            frame, rest = take_rtu(self._buffer)
            if frame is not None:
                self._buffer = rest
                return frame
            if rest is not self._buffer:
                self._buffer = rest
                continue
            self._buffer += await self._read_chunk(reader)

    async def _read_chunk(self, reader: asyncio.StreamReader) -> bytes:
        try:
            chunk = await asyncio.wait_for(reader.read(256), timeout=self._timeout)
        except (TimeoutError, ConnectionError, OSError) as err:
            raise classify_io(err, connected=True) from err
        if not chunk:
            raise SolisLoggerOffline(
                "logger closed the connection",
                connected=True,
                reason=REASON_CLOSED,
            )
        return chunk
