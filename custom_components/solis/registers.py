"""Solis Modbus maps used by a Solarman stick.

The string map is the grid-inverter table (function 4, 3000 range) checked
against Ginlong ModbusRTU Ver.3.7 and a real stick. The hybrid map is the
33000 range used by S5/S6 hybrids. Auto-detection probes 33022 first: this
string inverter answers that read with Modbus exception 2.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import InverterSnapshot, Phase, PvString

STRING_BLOCKS: tuple[tuple[int, int], ...] = ((3000, 20), (3020, 20), (3040, 10))
HYBRID_BLOCKS: tuple[tuple[int, int], ...] = ((33029, 10), (33073, 6), (33133, 18))
SERIAL_BLOCK = (3060, 4)


def u32(words: dict[int, int], address: int) -> int:
    """Read a big-endian uint32. The high word is the lower address."""
    return ((words.get(address, 0) & 0xFFFF) << 16) | (words.get(address + 1, 0) & 0xFFFF)


def s32(words: dict[int, int], address: int) -> int:
    """Read a big-endian int32."""
    value = u32(words, address)
    if value & 0x80000000:
        return value - 0x100000000
    return value


def _scale(words: dict[int, int], address: int, factor: float) -> float:
    return (words.get(address, 0) & 0xFFFF) * factor


def serial_from_words(words: dict[int, int]) -> str:
    """Decode registers 3060-3063 as an ASCII inverter serial, if present."""
    raw = b"".join(
        int(words.get(address, 0) & 0xFFFF).to_bytes(2, "big") for address in range(3060, 3064)
    )
    text = raw.decode("ascii", errors="ignore").strip("\x00 ").strip()
    if not text or not any(character.isalnum() for character in text):
        return ""
    return text


def _string_state(raw: int) -> int | None:
    """Map register 3043 onto the cloud status codes.

    Ver.3.7 points at an appendix that is not in the register table. Value 1
    is the usual on-grid code in the Solarman string profiles. Value 2 is a
    fault. Value 3 was read from a waking string inverter that still had grid
    voltage and PV voltage, so it is not the cloud's alarm code.
    """
    if raw == 1:
        return 1
    if raw == 2:
        return 3
    return None


def decode_string(
    words: dict[int, int],
    *,
    serial: str,
    logger_serial: str,
    source: str = "local",
    now: datetime | None = None,
) -> InverterSnapshot:
    """Build an inverter snapshot from the 3000-range map."""
    moment = now or datetime.now(UTC)
    ac_type = words.get(3002, 0) & 0xFFFF
    phases = _string_phases(words, ac_type)
    strings = _pv_strings(words, (words.get(3003, 0) & 0xFFFF) + 1)
    hmi = words.get(3001)
    return InverterSnapshot(
        serial=serial,
        device_id=serial,
        station_id="local",
        name=serial,
        model="String inverter",
        product_model="1",
        hmi_version=None if hmi is None else str(hmi),
        state=_string_state(words.get(3043, 0)),
        ac_output_type=0 if ac_type == 0 else 1,
        data_timestamp=moment,
        collector_serial=logger_serial,
        ac_power_w=float(s32(words, 3004)),
        frequency_hz=_scale(words, 3042, 0.01),
        temperature_c=_scale(words, 3041, 0.1),
        energy_today_kwh=_scale(words, 3014, 0.1),
        energy_month_kwh=float(u32(words, 3010)),
        energy_year_kwh=float(u32(words, 3016)),
        energy_total_kwh=float(u32(words, 3008)),
        battery_power_w=None,
        battery_soc=None,
        battery_soh=None,
        battery_voltage_v=None,
        battery_current_a=None,
        battery_charge_today_kwh=None,
        battery_discharge_today_kwh=None,
        battery_charge_total_kwh=None,
        battery_discharge_total_kwh=None,
        grid_power_w=None,
        consumption_power_w=None,
        force_charge_soc=None,
        force_discharge_soc=None,
        phases=phases,
        strings=strings,
        data_source=source,
    )


def _string_phases(words: dict[int, int], ac_type: int) -> tuple[Phase, ...]:
    phases = [
        Phase(
            index=index,
            voltage_v=_scale(words, voltage, 0.1),
            current_a=_scale(words, current, 0.1),
            power_w=None,
        )
        for index, voltage, current in ((1, 3033, 3036), (2, 3034, 3037), (3, 3035, 3038))
    ]
    if ac_type != 0:
        return tuple(phases)
    live = next((phase for phase in phases if phase.voltage_v), phases[0])
    return (
        Phase(
            index=1,
            voltage_v=live.voltage_v,
            current_a=live.current_a,
            power_w=live.power_w,
        ),
    )


def _pv_strings(words: dict[int, int], count: int) -> tuple[PvString, ...]:
    rows: list[PvString] = []
    for index, address in enumerate(range(3021, 3029, 2), start=1):
        if index > max(1, min(count, 4)):
            break
        voltage = _scale(words, address, 0.1)
        current = _scale(words, address + 1, 0.1)
        power = None if voltage == 0 and current == 0 else voltage * current
        rows.append(PvString(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def decode_hybrid(
    words: dict[int, int],
    *,
    serial: str,
    logger_serial: str,
    source: str = "local",
    now: datetime | None = None,
) -> InverterSnapshot:
    """Build an inverter snapshot from the 33000-range hybrid map.

    Battery power at 33149 is often an unsigned magnitude. Register 33135 is
    0 while charging and 1 while discharging, so discharge is stored negative.
    """
    moment = now or datetime.now(UTC)
    power = float(s32(words, 33149))
    if words.get(33135) == 1 and power > 0:
        power = -power
    current = _scale(words, 33134, 0.1)
    if words.get(33135) == 1 and current > 0:
        current = -current
    return InverterSnapshot(
        serial=serial,
        device_id=serial,
        station_id="local",
        name=serial,
        model="Hybrid inverter",
        product_model="2",
        hmi_version=None,
        state=1,
        ac_output_type=1,
        data_timestamp=moment,
        collector_serial=logger_serial,
        ac_power_w=float(u32(words, 33057)) if 33057 in words else None,
        frequency_hz=None,
        temperature_c=None,
        energy_today_kwh=_scale(words, 33035, 0.1) if 33035 in words else None,
        energy_month_kwh=None,
        energy_year_kwh=None,
        energy_total_kwh=float(u32(words, 33029)) if 33029 in words else None,
        battery_power_w=power if 33149 in words else None,
        battery_soc=_scale(words, 33139, 1.0) if 33139 in words else None,
        battery_soh=_scale(words, 33140, 1.0) if 33140 in words else None,
        battery_voltage_v=_scale(words, 33133, 0.1) if 33133 in words else None,
        battery_current_a=current if 33134 in words else None,
        battery_charge_today_kwh=None,
        battery_discharge_today_kwh=None,
        battery_charge_total_kwh=None,
        battery_discharge_total_kwh=None,
        grid_power_w=None,
        consumption_power_w=None,
        force_charge_soc=None,
        force_discharge_soc=None,
        phases=(),
        strings=(),
        data_source=source,
    )
