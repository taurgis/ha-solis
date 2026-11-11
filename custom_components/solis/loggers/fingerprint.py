"""Tell an S2-WL-ST from a Modbus gateway without a second session on port 502.

The inverter register map is the same through either device. A second Modbus
connection is not opened: firmware 1001318c can ignore port 502 for minutes
after a socket closes. The signal is the device's own page on port 80.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import re

from ..const import LOGGER_MBTCP, LOGGER_S2

# Solis documents a web page on the S2-WL-ST (admin, LAN Settings). The
# S2-WL token is the strong match. "solis" alone is not: a gateway status
# page can name the inverter.
_S2_STRONG = ("s2-wl", "s2_wl")
_S2_CONTEXT = ("logger", "datalogger", "data logger", "lan settings")
_S2_VENDOR = ("solis", "ginlong")
# Vendor pages. Hi-Flying is the Elfin module. Solarman sticks use that
# module too, but those answer V5 on 8899, not this Modbus TCP probe.
_GATEWAY = (
    "waveshare",
    "rs485 to eth",
    "rs485-to-eth",
    "rs485_to_eth",
    "elfin",
    "ew11",
    "ee11",
    "hi-flying",
    "hiflying",
    "hf-a11",
    "hf-lpt",
    "usr-tcp",
    "tcp232",
    "有人物联",
)
# LAN hostnames reported for these gateways. Not MAC OUIs: none are published
# for the S2, and a wrong OUI would claim every device from that vendor.
_GATEWAY_HOSTS = ("ew11", "ee11", "elfin", "usr-tcp", "usr-w610", "usr-dr")


@dataclass(frozen=True, slots=True)
class HttpIdentity:
    """What port 80 said about the device that answered Modbus on 502."""

    confident_kind: str
    suggested: str
    confidence: str
    note: str
    detection: str


def classify_http(page: str) -> HttpIdentity:
    """Score one HTTP banner. An empty page is inconclusive and suggests an S2."""
    text = page.casefold()
    s2 = _s2_page(text)
    gateway = any(token in text for token in _GATEWAY)
    if s2 and not gateway:
        return HttpIdentity(
            confident_kind=LOGGER_S2,
            suggested=LOGGER_S2,
            confidence="high",
            note=("Port 80 identified a Solis S2-WL-ST page. Modbus TCP on 502 is that stick."),
            detection="auto_s2_http",
        )
    if gateway and not s2:
        return HttpIdentity(
            confident_kind=LOGGER_MBTCP,
            suggested=LOGGER_MBTCP,
            confidence="high",
            note=(
                "Port 80 identified a gateway web page (Waveshare, Elfin, "
                "Hi-Flying or USR). Port 502 is Modbus TCP to the inverter, "
                "not an S2-WL-ST."
            ),
            detection="auto_gateway_http",
        )
    if s2 and gateway:
        return HttpIdentity(
            confident_kind="",
            suggested=LOGGER_S2,
            confidence="low",
            note=(
                "Port 80 contained both a Solis logger name and a gateway name. "
                "Pick the device. S2-WL-ST is selected because that is the "
                "stick Solis documents on port 502."
            ),
            detection="",
        )
    if text.strip():
        note = (
            "Port 502 returned a Solis register map. Port 80 answered, and "
            "the page did not name an S2-WL-ST or a gateway. Several S2 "
            "firmware builds have a generic status page. Pick the device."
        )
    else:
        note = (
            "Port 502 returned a Solis register map and port 80 did not "
            "identify the device. Solis documents that map on the S2-WL-ST, "
            "and several firmware builds have no web page. A gateway in "
            "Modbus TCP mode looks the same. Pick the device."
        )
    return HttpIdentity(
        confident_kind="",
        suggested=LOGGER_S2,
        confidence="low",
        note=note,
        detection="",
    )


def hostname_is_known_gateway(hostname: str) -> bool:
    """True for Elfin and USR hostnames reported on the LAN. Not a MAC OUI."""
    text = hostname.strip().lower().replace("_", "-")
    if not text:
        return False
    return any(text == marker or text.startswith(marker) for marker in _GATEWAY_HOSTS)


def rtu_default_port(preset: str) -> int:
    """Factory listen port for a transparent gateway. An explicit port wins elsewhere."""
    from ..const import RTU_PORT

    return {"waveshare": 4196, "usr": 26, "esp": 6638}.get(preset, RTU_PORT)


_PROBE_HOST = re.compile(r"\A[A-Za-z0-9.-]{1,253}\Z")


def _probe_host_ok(host: str) -> bool:
    """Accept a hostname or IP. Reject header injection and URLs."""
    return _PROBE_HOST.fullmatch(host) is not None and ".." not in host


async def read_http_banner(host: str, *, timeout: float = 1.2, port: int = 80) -> str:
    """Read the device page on this host only.

    The request is one GET with a size cap. Redirects are not followed, so a
    Location header cannot send the probe to another machine. Connect, write
    and read share one timeout.
    """
    if not _probe_host_ok(host):
        return ""
    try:
        return await asyncio.wait_for(_read_banner(host, port), timeout=timeout)
    except (TimeoutError, OSError):
        return ""


async def _read_banner(host: str, port: int) -> str:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        target = host.encode("ascii")
        writer.write(b"GET / HTTP/1.0\r\nHost: " + target + b"\r\nConnection: close\r\n\r\n")
        await writer.drain()
        data = await reader.read(4096)
    finally:
        writer.close()
        with suppress(ConnectionError, OSError, TimeoutError):
            await writer.wait_closed()
    return data.decode("latin-1", errors="ignore")


def _s2_page(text: str) -> bool:
    if any(token in text for token in _S2_STRONG):
        return True
    vendor = any(token in text for token in _S2_VENDOR)
    context = any(token in text for token in _S2_CONTEXT)
    return vendor and context
