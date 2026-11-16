"""Read a Solis stick that only has a status web page.

S3-class sticks serve no Modbus and no Solarman V5. Their status page loads
``inverter.cgi`` and ``moniter.cgi`` (the spelling is the stick's own) behind
basic auth with the user ``admin``. The stick polls the inverter about every
five minutes and serves the cached values, so reading more often only costs
requests. This module is the HTTP client and the two parsers. It speaks one
HTTP/1.0 request per connection, follows no redirect and keeps no socket.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
import math

from ..const import (
    REASON_REFUSED,
    REASON_WEB_AUTH,
    REASON_WEB_PAGE,
    WEB_PORT,
    WEB_USER,
)
from ..errors import SolisLoggerBusy, SolisLoggerOffline
from ..solarman import classify_io
from .fingerprint import probe_host_ok

INVERTER_PATH = "/inverter.cgi"
COLLECTOR_PATH = "/moniter.cgi"
_MAX_BODY = 16384
_INVERTER_FIELDS = 8
_COLLECTOR_FIELDS = 13
_NO_ALARM = "NO"
# The stick pads its replies. A real stick pads the front with NUL bytes.
_PAD = " \t\r\n\x00"
# A reading outside these bounds is a garbled page, not a measurement.
_MAX_POWER_W = 10_000_000.0
_TEMPERATURE_RANGE_C = (-80.0, 300.0)
_MAX_KWH = 10_000_000.0


@dataclass(frozen=True, slots=True)
class WebInverter:
    """One ``inverter.cgi`` reply."""

    serial: str
    firmware: str
    model_code: str
    temperature_c: float
    power_w: float
    energy_today_kwh: float
    energy_total_kwh: float
    alarm: str

    @property
    def in_alarm(self) -> bool:
        """The page prints ``NO`` when the inverter reports no alarm."""
        return self.alarm.upper() != _NO_ALARM


@dataclass(frozen=True, slots=True)
class WebCollector:
    """The part of one ``moniter.cgi`` reply the integration keeps."""

    serial: str
    version: str
    mac: str


def is_solis_web_banner(page: str) -> bool:
    """True for the stick's own 401 page: ``WWW-authenticate: Basic realm="solis"``."""
    return 'realm="solis"' in page.casefold()


def parse_inverter(body: str) -> WebInverter:
    """Split the semicolon string. Raise ``ValueError`` for anything else.

    The fields are serial, firmware, model code, temperature in degrees,
    power in watts, energy today and total in kWh, and an alarm word.
    """
    fields = [item.strip(_PAD) for item in body.split(";")]
    if len(fields) < _INVERTER_FIELDS:
        raise ValueError("inverter.cgi has too few fields")
    if not fields[7]:
        raise ValueError("inverter.cgi has no alarm word")
    temperature = _number(fields[3], *_TEMPERATURE_RANGE_C)
    power = _number(fields[4], -_MAX_POWER_W, _MAX_POWER_W)
    today = _number(fields[5], 0.0, _MAX_KWH)
    total = _number(fields[6], 0.0, _MAX_KWH)
    return WebInverter(
        serial=fields[0],
        firmware=fields[1],
        model_code=fields[2],
        temperature_c=temperature,
        power_w=power,
        energy_today_kwh=today,
        energy_total_kwh=total,
        alarm=fields[7],
    )


def _number(text: str, low: float, high: float) -> float:
    """Parse one field. NaN, infinity and out-of-range numbers raise ``ValueError``."""
    number = float(text)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{text!r} is not a plausible reading")
    return number


def parse_collector(body: str) -> WebCollector | None:
    """Return the stick serial, firmware and MAC, or ``None`` if the page differs."""
    fields = [item.strip(_PAD) for item in body.split(";")]
    if len(fields) < _COLLECTOR_FIELDS or not fields[2]:
        return None
    mac = fields[11] if fields[11].lower() != "null" else ""
    return WebCollector(serial=fields[2], version=fields[3], mac=mac)


def _basic(password: str) -> str:
    return base64.b64encode(f"{WEB_USER}:{password}".encode()).decode("ascii")


def _request(host: str, path: str, password: str) -> bytes:
    return (
        f"GET {path} HTTP/1.0\r\nHost: {host}\r\nAuthorization: Basic {_basic(password)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")


def _split_response(raw: bytes) -> tuple[int, str]:
    """Return the status code and body. A reply without a status line is unreadable."""
    head, _sep, body = raw.partition(b"\r\n\r\n")
    parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise ValueError("not an HTTP reply")
    return int(parts[1]), body.decode("latin-1", errors="replace")


def _complete(raw: bytes) -> bool:
    """True once the body has reached its declared length, so the read need not wait for EOF."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return False
    for line in head.split(b"\r\n")[1:]:
        name, _colon, value = line.partition(b":")
        length = value.strip()
        if name.strip().lower() == b"content-length" and length.isdigit() and len(length) <= 9:
            return len(body) >= int(length)
    return False


class WebStickClient:
    """Fetch the stick pages. Each call is its own short connection."""

    def __init__(
        self, host: str, password: str, *, port: int = WEB_PORT, timeout: float = 8.0
    ) -> None:
        """Remember where the stick is. Nothing connects until a read."""
        self.host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._connected = False

    async def close(self) -> None:
        """Nothing stays open between requests."""

    async def read_inverter(self) -> WebInverter:
        """Read ``inverter.cgi``. Raises the logger errors the coordinator understands."""
        body = await self._get(INVERTER_PATH)
        try:
            return parse_inverter(body)
        except ValueError as err:
            raise SolisLoggerBusy(
                f"{self.host} inverter page is not the Solis format", reason=REASON_WEB_PAGE
            ) from err

    async def read_collector(self) -> WebCollector | None:
        """Read ``moniter.cgi``. A page that differs is not an error here."""
        try:
            return parse_collector(await self._get(COLLECTOR_PATH))
        except SolisLoggerBusy as err:
            if err.reason == REASON_WEB_AUTH:
                raise
            return None

    async def _get(self, path: str) -> str:
        if not probe_host_ok(self.host):
            raise SolisLoggerOffline(f"{self.host} is not a host name", reason=REASON_REFUSED)
        self._connected = False
        try:
            raw = await asyncio.wait_for(self._exchange(path), timeout=self._timeout)
        except (TimeoutError, OSError) as err:
            failure = classify_io(err, connected=self._connected)
            if isinstance(failure, SolisLoggerBusy):
                raise SolisLoggerBusy(str(failure), reason=REASON_WEB_PAGE) from err
            raise failure from err
        try:
            status, body = _split_response(raw)
        except ValueError as err:
            raise SolisLoggerBusy(
                f"{self.host} did not answer with a web page", reason=REASON_WEB_PAGE
            ) from err
        if status == 401:
            raise SolisLoggerBusy(f"{self.host} rejected the web password", reason=REASON_WEB_AUTH)
        if status != 200:
            raise SolisLoggerBusy(
                f"{self.host} answered {path} with status {status}", reason=REASON_WEB_PAGE
            )
        return body

    async def _exchange(self, path: str) -> bytes:
        reader, writer = await asyncio.open_connection(self.host, self._port)
        self._connected = True
        try:
            writer.write(_request(self.host, path, self._password))
            await writer.drain()
            received = b""
            while len(received) < _MAX_BODY:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                received += chunk
                if _complete(received):
                    break
        finally:
            writer.close()
            with suppress(ConnectionError, OSError, TimeoutError):
                await writer.wait_closed()
        return received
