"""Registry of local logger kinds.

Adding a dongle is a ``DongleSpec`` here. A new framing also needs a client
with ``read_input`` and ``close``. The config form reads ``SELECTABLE_KINDS``
so the list is not copied into the flow.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import (
    LOGGER_AUTO,
    LOGGER_MBTCP,
    LOGGER_RTU,
    LOGGER_S2,
    LOGGER_S5,
    LOGGER_SOLARMAN,
    PROTOCOL_MBTCP,
    PROTOCOL_RTU,
    PROTOCOL_V5,
    RTU_PORT,
    S2_PORT,
    SOLARMAN_PORT,
)


@dataclass(frozen=True, slots=True)
class DongleSpec:
    """Static facts about one logger kind. The socket is not one of them."""

    kind: str
    label: str
    default_port: int
    protocol: str
    experimental: bool
    powered_by_inverter: bool
    unique_prefix: str
    bridge: bool


SPECS: dict[str, DongleSpec] = {
    LOGGER_SOLARMAN: DongleSpec(
        kind=LOGGER_SOLARMAN,
        label="Solarman",
        default_port=SOLARMAN_PORT,
        protocol=PROTOCOL_V5,
        experimental=False,
        powered_by_inverter=True,
        unique_prefix="",
        bridge=False,
    ),
    LOGGER_S2: DongleSpec(
        kind=LOGGER_S2,
        label="S2-WL-ST",
        default_port=S2_PORT,
        protocol=PROTOCOL_MBTCP,
        experimental=False,
        powered_by_inverter=True,
        unique_prefix="s2",
        bridge=False,
    ),
    LOGGER_S5: DongleSpec(
        kind=LOGGER_S5,
        label="S5-WiFi-ST",
        default_port=S2_PORT,
        protocol=PROTOCOL_MBTCP,
        experimental=True,
        powered_by_inverter=True,
        unique_prefix="s2",
        bridge=False,
    ),
    LOGGER_MBTCP: DongleSpec(
        kind=LOGGER_MBTCP,
        label="Modbus TCP gateway",
        default_port=S2_PORT,
        protocol=PROTOCOL_MBTCP,
        experimental=False,
        powered_by_inverter=False,
        unique_prefix="tcp",
        bridge=True,
    ),
    LOGGER_RTU: DongleSpec(
        kind=LOGGER_RTU,
        label="Modbus RTU over TCP",
        default_port=RTU_PORT,
        protocol=PROTOCOL_RTU,
        experimental=True,
        powered_by_inverter=False,
        unique_prefix="rtu",
        bridge=True,
    ),
}

SELECTABLE_KINDS: tuple[str, ...] = (
    LOGGER_AUTO,
    LOGGER_SOLARMAN,
    LOGGER_S2,
    LOGGER_MBTCP,
    LOGGER_S5,
    LOGGER_RTU,
)

_DETECT_WHY = {
    "auto_s2": (
        "Auto-detect stored this as an S2-WL-ST after Modbus TCP on port 502 "
        "returned a Solis register map. An S5-WiFi-ST uses the same frames. "
        "A gateway on port 502 does too; pick Modbus TCP gateway for that device."
    ),
    "auto_solarman": (
        "Auto-detect stored this as a Solarman logger after V5 or Modbus TCP answered on its port."
    ),
    "manual_solarman": "The form selected Solarman V5 and the probe confirmed a Solis map.",
    "manual_s2_wl_st": "The form selected Solis S2-WL-ST and the probe confirmed Modbus TCP.",
    "manual_s5_wifi_st": (
        "The form selected Solis S5-WiFi-ST (experimental). The probe used the "
        "S2 Modbus TCP client. Later firmware often closes port 502."
    ),
    "manual_modbus_tcp": (
        "The form selected a Modbus TCP gateway and the probe confirmed MBAP "
        "and a Solis map. A refused port is a down gateway, not Solis firmware."
    ),
    "manual_rtu_tcp": (
        "The form selected Modbus RTU over TCP (experimental) and the probe "
        "confirmed a raw RTU frame and a Solis map."
    ),
    "": (
        "This entry was saved before dongle detection was recorded. "
        "Kind and protocol are what the entry is using now."
    ),
}


def spec_for(kind: str) -> DongleSpec:
    """Return the spec for ``kind``. An empty or unknown kind is Solarman."""
    return SPECS.get(kind, SPECS[LOGGER_SOLARMAN])


def kind_label(kind: str) -> str:
    """Short name used in the one info log when an address changes."""
    if not kind or kind == LOGGER_AUTO:
        return SPECS[LOGGER_SOLARMAN].label
    return spec_for(kind).label


def detection_code(request_kind: str, probe_kind: str) -> str:
    """Stable code stored on the entry so diagnostics can explain the choice."""
    if request_kind in {"", LOGGER_AUTO}:
        if probe_kind in {LOGGER_S2, LOGGER_S5}:
            return "auto_s2"
        return "auto_solarman"
    return f"manual_{probe_kind}"


def detection_why(code: str) -> str:
    """One sentence for diagnostics. Unknown codes use the legacy sentence."""
    return _DETECT_WHY.get(code, _DETECT_WHY[""])
