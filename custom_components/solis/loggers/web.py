"""Read a Solis stick that only has a status web page.

Two page layouts are known. S3-class sticks made by Solis serve
``inverter.cgi`` and ``moniter.cgi`` (the spelling is the stick's own): one
semicolon string each. Sticks made by IGEN/Solarman for Solis serve
``status.html`` instead, with the readings as JavaScript variables
(``var webdata_now_p = "472";``). Both sit behind HTTP Basic auth with the
user ``admin``. An empty or factory password tries ``123456789`` and then
``admin`` (the IGEN factory password) and keeps the one that answers.
``inverter.cgi`` is tried first; a 404 or a reply in another
format falls back to ``status.html``, and the layout that answered is kept.

The stick polls the inverter about every five minutes and serves the cached
values, so reading more often only costs requests.

Why a small HTTP client over raw streams instead of the shared aiohttp
session: these are lwIP and vendor HTTP/1.0 servers on a Wi-Fi module. They
handle one socket at a time, pad replies with NUL bytes, may omit
Content-Length and end the body by closing, and some old firmware closes the
socket without a reply for pages it does not serve. The client sends one
request per connection with ``Connection: close``, keeps no pool or
keep-alive socket the stick would have to hold, follows no redirect (a
``Location`` header cannot send the request and its password to another
host), caps the reply size, bounds connect plus read with one timeout, and
reads chunked HTTP/1.1 replies too.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
import math
import re

from ..const import (
    REASON_REFUSED,
    REASON_WEB_AUTH,
    REASON_WEB_PAGE,
    WEB_DEFAULT_PASSWORD,
    WEB_FACTORY_PASSWORDS,
    WEB_PORT,
    WEB_USER,
)
from ..errors import SolisLoggerAuthError, SolisLoggerBusy, SolisLoggerOffline
from ..solarman import classify_io
from .fingerprint import probe_host_ok

INVERTER_PATH = "/inverter.cgi"
COLLECTOR_PATH = "/moniter.cgi"
STATUS_PATH = "/status.html"
_MAX_BODY = 65536
# serial; firmware; model; temperature; power. The rest is optional.
_INVERTER_MIN_FIELDS = 5
_COLLECTOR_FIELDS = 13
_NO_ALARM = "NO"
# The stick pads its replies. A real stick pads the front with NUL bytes.
_PAD = " \t\r\n\x00"
# A reading outside these bounds is a garbled page, not a measurement.
_MAX_POWER_W = 10_000_000.0
_TEMPERATURE_RANGE_C = (-80.0, 300.0)
_MAX_KWH = 10_000_000.0
_HEAD_END = re.compile(rb"\r?\n\r?\n")
_JS_VAR = re.compile(r"""var\s+(\w+)\s*=\s*(["'])(.*?)\2""", re.DOTALL)
# A status.html value: a number, optionally followed by a unit such as kWh or W.
_STATUS_NUMBER = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*[A-Za-z%]*$")
_BASIC_CHALLENGE = re.compile(r"^www-authenticate:\s*basic\b", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True, slots=True)
class WebInverter:
    """One inverter reading from ``inverter.cgi`` or ``status.html``.

    Power is ``None`` only on ``status.html`` when the stick has no reading
    from the inverter (an empty value or ``---``, usually at night).
    """

    serial: str
    firmware: str
    model_code: str
    temperature_c: float | None
    power_w: float | None
    energy_today_kwh: float | None
    energy_total_kwh: float | None
    alarm: str
    page: str = INVERTER_PATH

    @property
    def in_alarm(self) -> bool:
        """No alarm is ``NO`` on ``inverter.cgi`` and an empty value on ``status.html``."""
        word = self.alarm.strip().upper()
        return bool(word) and word != _NO_ALARM


@dataclass(frozen=True, slots=True)
class WebCollector:
    """The part of one ``moniter.cgi`` or ``status.html`` reply the integration keeps."""

    serial: str
    version: str
    mac: str


def is_solis_web_banner(page: str) -> bool:
    """True for the Solis stick's own 401 page: ``WWW-authenticate: Basic realm="solis"``."""
    return 'realm="solis"' in page.casefold()


def is_basic_auth_challenge(page: str) -> bool:
    """True for any HTTP 401 that asks for Basic auth, whatever the realm.

    Sticks differ in the realm they name, so the realm alone cannot confirm a
    Solis stick. The caller confirms by reading and parsing the data page.
    """
    status = page.split("\n", 1)[0].split()
    if len(status) < 2 or not status[0].startswith("HTTP/") or status[1] != "401":
        return False
    return _BASIC_CHALLENGE.search(page) is not None


def parse_inverter(body: str) -> WebInverter:
    """Split the ``inverter.cgi`` string. Raise ``ValueError`` if it has no power reading.

    The fields are serial, firmware, model code, temperature in degrees,
    power in watts, energy today and total in kWh, and an alarm word. Only
    power is required: some firmware prints ``d`` for the total. A
    temperature or energy field that is not a plausible number is ``None``.
    """
    fields = [item.strip(_PAD) for item in body.split(";")]
    if len(fields) < _INVERTER_MIN_FIELDS:
        raise ValueError("inverter.cgi has too few fields")
    power = _number(fields[4], -_MAX_POWER_W, _MAX_POWER_W)
    return WebInverter(
        serial=fields[0],
        firmware=fields[1],
        model_code=fields[2],
        temperature_c=_optional(fields[3], *_TEMPERATURE_RANGE_C),
        power_w=power,
        energy_today_kwh=_optional(_field(fields, 5), 0.0, _MAX_KWH),
        energy_total_kwh=_optional(_field(fields, 6), 0.0, _MAX_KWH),
        alarm=_field(fields, 7),
    )


def _field(fields: list[str], index: int) -> str:
    return fields[index] if index < len(fields) else ""


def _number(text: str, low: float, high: float) -> float:
    """Parse one field. NaN, infinity and out-of-range numbers raise ``ValueError``."""
    number = float(text)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError(f"{text!r} is not a plausible reading")
    return number


def _optional(text: str, low: float, high: float) -> float | None:
    """Parse an optional field. Anything that is not a plausible number is ``None``."""
    try:
        return _number(text, low, high)
    except ValueError:
        return None


def _status_value(text: str, low: float, high: float) -> float | None:
    """A ``status.html`` value. Empty, ``---`` and garbled values are ``None``."""
    match = _STATUS_NUMBER.match(text.strip())
    return _optional(match.group(1), low, high) if match else None


def _status_vars(body: str) -> dict[str, str]:
    return {name: value for name, _quote, value in _JS_VAR.findall(body)}


def parse_status(body: str) -> WebInverter:
    """Read the ``webdata_*`` variables of ``status.html``. Raise ``ValueError`` for another page.

    The page must declare ``webdata_now_p``. Its value and the energy values
    may be empty or ``---`` while the inverter is not talking to the stick.
    """
    values = _status_vars(body)
    if "webdata_now_p" not in values:
        raise ValueError("status.html has no webdata_now_p")
    return WebInverter(
        serial=values.get("webdata_sn", "").strip(_PAD),
        firmware=values.get("webdata_msvn", "").strip(_PAD),
        model_code="",
        temperature_c=None,
        power_w=_status_value(values["webdata_now_p"], -_MAX_POWER_W, _MAX_POWER_W),
        energy_today_kwh=_status_value(values.get("webdata_today_e", ""), 0.0, _MAX_KWH),
        energy_total_kwh=_status_value(values.get("webdata_total_e", ""), 0.0, _MAX_KWH),
        alarm=values.get("webdata_alarm", "").strip(_PAD),
        page=STATUS_PATH,
    )


def parse_collector(body: str) -> WebCollector | None:
    """Return the stick serial, firmware and MAC, or ``None`` if the page differs."""
    fields = [item.strip(_PAD) for item in body.split(";")]
    if len(fields) < _COLLECTOR_FIELDS or not fields[2]:
        return None
    mac = fields[11] if fields[11].lower() != "null" else ""
    return WebCollector(serial=fields[2], version=fields[3], mac=mac)


def parse_status_collector(body: str) -> WebCollector | None:
    """Return the stick serial (``cover_mid``), firmware and MAC from ``status.html``."""
    values = _status_vars(body)
    serial = values.get("cover_mid", "").strip(_PAD)
    if not serial:
        return None
    return WebCollector(
        serial=serial,
        version=values.get("cover_ver", "").strip(_PAD),
        mac=values.get("cover_sta_mac", "").strip(_PAD),
    )


def _basic(password: str) -> str:
    return base64.b64encode(f"{WEB_USER}:{password}".encode()).decode("ascii")


def _request(host: str, path: str, password: str) -> bytes:
    return (
        f"GET {path} HTTP/1.0\r\nHost: {host}\r\nAuthorization: Basic {_basic(password)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")


def _head_and_body(raw: bytes) -> tuple[bytes, bytes] | None:
    """Split at the blank line. Bare LF line ends are accepted."""
    end = _HEAD_END.search(raw)
    if end is None:
        return None
    return raw[: end.start()], raw[end.end() :]


def _headers(head: bytes) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for line in head.split(b"\n")[1:]:
        name, _colon, value = line.partition(b":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _chunked(headers: dict[bytes, bytes]) -> bool:
    return b"chunked" in headers.get(b"transfer-encoding", b"").lower()


def _dechunk(body: bytes) -> tuple[bytes, bool]:
    """Decode a chunked body. Return the data so far and whether the last chunk arrived."""
    data = bytearray()
    rest = body
    while True:
        line, sep, after = rest.partition(b"\n")
        if not sep:
            return bytes(data), False
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError:
            return bytes(data), False
        if size == 0:
            return bytes(data), True
        data += after[:size]
        if len(after) < size:
            return bytes(data), False
        rest = after[size:].removeprefix(b"\r\n").removeprefix(b"\n")


def _split_response(raw: bytes) -> tuple[int, str]:
    """Return the status code and body. A reply without a status line is unreadable."""
    parts_found = _head_and_body(raw)
    head, body = parts_found if parts_found is not None else (raw, b"")
    parts = head.split(b"\n", 1)[0].split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise ValueError("not an HTTP reply")
    if _chunked(_headers(head)):
        body, _done = _dechunk(body)
    return int(parts[1]), body.decode("latin-1", errors="replace")


def _complete(raw: bytes) -> bool:
    """True once the body is complete, so the read need not wait for EOF.

    That is the declared Content-Length, or the last chunk of a chunked body.
    """
    parts = _head_and_body(raw)
    if parts is None:
        return False
    head, body = parts
    headers = _headers(head)
    if _chunked(headers):
        return _dechunk(body)[1]
    length = headers.get(b"content-length", b"")
    if length.isdigit() and len(length) <= 9:
        return len(body) >= int(length)
    return False


class WebStickClient:
    """Fetch the stick pages. Each call is its own short connection."""

    def __init__(
        self, host: str, password: str, *, port: int = WEB_PORT, timeout: float = 8.0
    ) -> None:
        """Remember where the stick is. Nothing connects until a read.

        An empty password, or the factory one, means "whichever factory
        password the stick accepts": see ``WEB_FACTORY_PASSWORDS``.
        """
        self.host = host
        self._port = port
        factory = password in ("", WEB_DEFAULT_PASSWORD)
        self._passwords = WEB_FACTORY_PASSWORDS if factory else (password,)
        self._password = self._passwords[0]
        self._timeout = timeout
        self._connected = False
        # The data page that answered last: "" until known.
        self.page = ""
        self._status_body = ""

    async def close(self) -> None:
        """Nothing stays open between requests."""

    async def read_inverter(self) -> WebInverter:
        """Read the inverter page. Raises the logger errors the coordinator understands.

        A wrong password raises ``SolisLoggerAuthError``. A page in no known
        format raises ``SolisLoggerBusy`` with the web-page reason.
        """
        tried = ""
        if self.page != STATUS_PATH:
            status, body = await self._fetch(INVERTER_PATH)
            if status == 200:
                with suppress(ValueError):
                    page = parse_inverter(body)
                    self.page = INVERTER_PATH
                    return page
            tried = self._failure(INVERTER_PATH, status)
            if self.page == INVERTER_PATH or status not in (200, 404):
                raise SolisLoggerBusy(f"{self.host} {tried}", reason=REASON_WEB_PAGE)
        status, body = await self._fetch(STATUS_PATH)
        if status == 200:
            with suppress(ValueError):
                page = parse_status(body)
                self.page = STATUS_PATH
                self._status_body = body
                return page
        # Look for both layouts again next time.
        self.page = ""
        failed = ", ".join(item for item in (tried, self._failure(STATUS_PATH, status)) if item)
        raise SolisLoggerBusy(f"{self.host} {failed}", reason=REASON_WEB_PAGE)

    @staticmethod
    def _failure(path: str, status: int) -> str:
        if status == 200:
            return f"{path} is not a Solis stick page"
        return f"{path} answered with status {status}"

    async def read_collector(self) -> WebCollector | None:
        """Read the stick details. A page that differs is not an error here."""
        if self.page == STATUS_PATH:
            body = self._status_body
            if not body:
                status, body = await self._fetch(STATUS_PATH)
                if status != 200:
                    return None
            return parse_status_collector(body)
        try:
            status, body = await self._fetch(COLLECTOR_PATH)
        except SolisLoggerAuthError:
            raise
        except SolisLoggerBusy:
            return None
        return parse_collector(body) if status == 200 else None

    async def _fetch(self, path: str) -> tuple[int, str]:
        """GET one page. A 401 raises ``SolisLoggerAuthError``; other statuses return."""
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
            following = self._passwords.index(self._password) + 1
            if following < len(self._passwords):
                self._password = self._passwords[following]
                return await self._fetch(path)
            # Every candidate failed: start from the first one on the next read.
            self._password = self._passwords[0]
            raise SolisLoggerAuthError(
                f"{self.host} rejected the web password", reason=REASON_WEB_AUTH
            )
        return status, body

    async def _exchange(self, path: str) -> bytes:
        reader, writer = await asyncio.open_connection(self.host, self._port)
        self._connected = True
        try:
            writer.write(_request(self.host, path, self._password))
            await writer.drain()
            received = b""
            while len(received) < _MAX_BODY:
                chunk = await reader.read(4096)
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
