"""Solis Modbus maps used by a Solarman stick.

The string map is the grid-inverter table (function 4, 3000 range) checked
against Ginlong ModbusRTU Ver.3.7 and a real stick. The hybrid map is the
33000 range used by S5/S6 hybrids. Auto-detection reads 3004 first (an old
Solis-mini 4G resets the session on reads at 3050 and above), then the type
register 35000, then 33022.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .const import LOCAL_STATION
from .models import InverterSnapshot, Phase, PvString

STRING_BLOCKS: tuple[tuple[int, int], ...] = ((3000, 20), (3020, 20), (3040, 10))
# 33029-33058: energy counters, PV1-PV4 voltage and current, total PV power.
# 33073-33095: grid phases, AC active power (S32 at 33079), temperature,
# frequency and the status word. 33133-33152: battery, house load and the
# grid port. 33161-33168: battery charge and discharge counters. Addresses,
# scales and signs follow the ESINV-33000ID map as read by hn/ginlong-solis,
# davidrapan/ha-solarman solis_hybrid.yaml and Pho3niX90/solis_modbus.
HYBRID_BLOCKS: tuple[tuple[int, int], ...] = (
    (33029, 30),
    (33073, 23),
    (33133, 20),
    (33161, 8),
)
SERIAL_BLOCK = (3060, 4)
# The hybrid serial is plain ASCII, two characters per register.
HYBRID_SERIAL_BLOCK = (33004, 16)
# Solis inverter type definition. Value // 100 is 10 for the 3000-range
# (INV-3000ID) map and 20 for the 33000-range (ESINV-33000ID) map; some
# firmware stores the same digits as hex (0x10xx, 0x20xx).
TYPE_REGISTER = 35000


def u32(words: dict[int, int], address: int) -> int:
    """Read a big-endian uint32. The high word is the lower address."""
    return ((words.get(address, 0) & 0xFFFF) << 16) | (words.get(address + 1, 0) & 0xFFFF)


def s32(words: dict[int, int], address: int) -> int:
    """Read a big-endian int32."""
    value = u32(words, address)
    if value & 0x80000000:
        return value - 0x100000000
    return value


def s16(words: dict[int, int], address: int) -> int:
    """Read a two's-complement int16."""
    value = words.get(address, 0) & 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def _scale(words: dict[int, int], address: int, factor: float) -> float:
    # Rounded so 28 * 0.1 is 2.8 and not 2.8000000000000003 in the recorder.
    return round((words.get(address, 0) & 0xFFFF) * factor, 2)


def _scale_signed(words: dict[int, int], address: int, factor: float) -> float:
    return round(s16(words, address) * factor, 2)


def _grid_frequency(value: float) -> float | None:
    """A register that reads 0 (no grid) is no frequency, as in the cloud parser."""
    return value if 40.0 <= value <= 70.0 else None


def map_from_type(value: int) -> str | None:
    """Return ``string`` or ``hybrid`` from register 35000, or None when it is not set."""
    value &= 0xFFFF
    if value // 100 == 10 or value >> 8 == 0x10:
        return "string"
    if value // 100 == 20 or value >> 8 == 0x20:
        return "hybrid"
    return None


def _nibble_swap(byte: int) -> int:
    return ((byte & 0x0F) << 4) | ((byte & 0xF0) >> 4)


def _serial_hex(words: list[int]) -> str:
    """Solis hex-nibble serial (hn/ginlong-solis ``SDT_SNHEX``).

    Each register gives four hex digits: the nibble-swapped low byte, then
    the nibble-swapped high byte. ``0x1301`` is ``1031``.
    """
    text = "".join(
        f"{_nibble_swap(word & 0xFF):02X}{_nibble_swap((word >> 8) & 0xFF):02X}" for word in words
    )
    if not text.strip("0") or not text.strip("F"):
        return ""
    return text


def _serial_ascii(raw: bytes) -> str:
    text = raw.decode("ascii", errors="ignore").strip("\x00 ").strip()
    if not text or not text.isprintable() or not any(c.isalnum() for c in text):
        return ""
    return text


def serial_from_words(words: dict[int, int]) -> str:
    """Decode registers 3060-3063 as an inverter serial.

    Solis firmware stores the serial hex-nibble encoded (see ``_serial_hex``).
    A register bank whose eight bytes are all printable letters or digits is
    taken as ASCII instead: a real hex-encoded serial would need every second
    digit to be 3 to 7 for that to happen.
    """
    values = [int(words.get(address, 0)) & 0xFFFF for address in range(3060, 3064)]
    if not any(values):
        return ""
    raw = b"".join(value.to_bytes(2, "big") for value in values)
    trimmed = raw.rstrip(b"\x00 ")
    if trimmed and all(chr(byte).isalnum() for byte in trimmed):
        return _serial_ascii(raw)
    return _serial_hex(values)


def hybrid_serial_from_words(words: list[int]) -> str:
    """Decode 33004-33019, ASCII with two characters per register.

    A register whose high byte is zero holds one character, which some
    firmware does. A register below 0x10 is one hex digit.
    """
    if words and all(0 < (value & 0xFFFF) < 0x10 for value in words if value):
        return "".join(f"{value & 0xF:X}" for value in words if value)
    raw = bytearray()
    for value in words:
        high, low = (value >> 8) & 0xFF, value & 0xFF
        if high:
            raw.append(high)
        if low:
            raw.append(low)
    return _serial_ascii(bytes(raw))


def firmware_version(words: dict[int, int]) -> str | None:
    """Return the cloud-style version from registers 3000 and 3001.

    On the Solis-mini-2500-4G those registers are 15 and 15, and SolisCloud
    shows inverter version ``0F000F``.
    """
    if 3000 not in words or 3001 not in words:
        return None
    return f"{words[3000] & 0xFF:02X}{words[3001] & 0xFFFF:04X}"


def device_model_name(series: str | None, model_code: str | None) -> str:
    """Join a series such as Solis-mini-2500-4G with a model code such as E4."""
    if series and model_code and model_code not in series:
        return f"{series} ({model_code})"
    if series:
        return series
    if model_code:
        return model_code
    return "Solis inverter"


def inverter_state(raw: int | None) -> int | None:
    """Map the status word (3043, or 33095 on a hybrid) onto the cloud codes.

    The Solis table is 0 waiting, 1 open loop, 2 soft start, 3 generating
    (15 is also a normal state on some firmware), and 0x1000 and above a
    fault such as 0x1015 no grid. An inverter that answers in any normal
    state is online (1); a fault is the cloud's alarm code (3).
    """
    if raw is None:
        return None
    value = raw & 0xFFFF
    if value >= 0x1000:
        return 3
    if value in (0, 1, 2, 3, 15):
        return 1
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
    strings = _pv_strings(words, words.get(3003, 0) & 0xFFFF)
    # A few tens of watts in the morning is production. Asleep is only the
    # stick being powered off, which never reaches this decoder.
    return InverterSnapshot(
        serial=serial,
        device_id=serial,
        station_id=LOCAL_STATION,
        name=serial,
        model="Solis 4G",
        product_model="1",
        hmi_version=firmware_version(words),
        state=inverter_state(words.get(3043)),
        ac_output_type=0 if ac_type == 0 else 1,
        data_timestamp=moment,
        collector_serial=logger_serial,
        ac_power_w=float(s32(words, 3004)),
        frequency_hz=_grid_frequency(_scale(words, 3042, 0.01)),
        temperature_c=_scale_signed(words, 3041, 0.1),
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


def _pv_strings(
    words: dict[int, int], dc_type: int, first: int = 3021, limit: int = 4
) -> tuple[PvString, ...]:
    """Build the PV inputs.

    Register 3003 is documented as the DC input type, without the encoding.
    A single-MPPT Solis-mini-2500-4G reads 1 there, so the value may be the
    input count or the count minus one. Inputs up to ``dc_type`` are always
    kept; the extra input that ``dc_type + 1`` would add is kept only when it
    shows voltage or current, so a one-input inverter has no phantom PV2.
    """
    sure = max(1, min(dc_type, limit))
    maybe = max(1, min(dc_type + 1, limit))
    rows: list[PvString] = []
    for index in range(1, maybe + 1):
        address = first + (index - 1) * 2
        voltage = _scale(words, address, 0.1)
        current = _scale(words, address + 1, 0.1)
        idle = voltage == 0 and current == 0
        if index > sure and idle:
            continue
        power = None if idle else round(voltage * current, 2)
        rows.append(PvString(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def _hybrid_strings(words: dict[int, int]) -> tuple[PvString, ...]:
    """PV1-PV4 at 33049-33056. PV1 is always kept; other idle inputs are left out."""
    rows: list[PvString] = []
    for index in range(1, 5):
        address = 33049 + (index - 1) * 2
        if address not in words:
            continue
        voltage = _scale(words, address, 0.1)
        current = _scale(words, address + 1, 0.1)
        idle = voltage == 0 and current == 0
        if index > 1 and idle:
            continue
        power = None if idle else round(voltage * current, 2)
        rows.append(PvString(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def _hybrid_phases(words: dict[int, int]) -> tuple[Phase, ...]:
    """Grid phases at 33073-33078. Phases B and C reading 0 V are a one-phase unit."""
    if 33073 not in words:
        return ()
    phases = [
        Phase(
            index=index,
            voltage_v=_scale(words, voltage, 0.1),
            current_a=_scale(words, current, 0.1),
            power_w=None,
        )
        for index, voltage, current in ((1, 33073, 33076), (2, 33074, 33077), (3, 33075, 33078))
    ]
    if not phases[1].voltage_v and not phases[2].voltage_v:
        return (phases[0],)
    return tuple(phases)


def _maybe(words: dict[int, int], address: int, value: float) -> float | None:
    return value if address in words else None


def decode_hybrid(
    words: dict[int, int],
    *,
    serial: str,
    logger_serial: str,
    source: str = "local",
    now: datetime | None = None,
) -> InverterSnapshot:
    """Build an inverter snapshot from the 33000-range hybrid map.

    AC active power is S32 at 33079 (33057 is total PV power). Temperature at
    33093 and battery current at 33134 are S16. Battery power at 33149 is
    often an unsigned magnitude. Register 33135 is 0 while charging and 1
    while discharging, so discharge is stored negative. The grid port power
    at 33151 is the inverter's AC port, not the meter, so it is not used as
    grid power.
    """
    moment = now or datetime.now(UTC)
    discharging = words.get(33135) == 1
    power = float(s32(words, 33149))
    if discharging and power > 0:
        power = -power
    current = _scale_signed(words, 33134, 0.1)
    if discharging and current > 0:
        current = -current
    phases = _hybrid_phases(words)
    frequency = _grid_frequency(_scale(words, 33094, 0.01)) if 33094 in words else None
    return InverterSnapshot(
        serial=serial,
        device_id=serial,
        station_id=LOCAL_STATION,
        name=serial,
        model="Hybrid inverter",
        product_model="2",
        hmi_version=None,
        state=inverter_state(words.get(33095)),
        ac_output_type=0 if len(phases) == 1 else 1,
        data_timestamp=moment,
        collector_serial=logger_serial,
        ac_power_w=_maybe(words, 33079, float(s32(words, 33079))),
        frequency_hz=frequency,
        temperature_c=_maybe(words, 33093, _scale_signed(words, 33093, 0.1)),
        energy_today_kwh=_maybe(words, 33035, _scale(words, 33035, 0.1)),
        energy_month_kwh=_maybe(words, 33031, float(u32(words, 33031))),
        energy_year_kwh=_maybe(words, 33037, float(u32(words, 33037))),
        energy_total_kwh=_maybe(words, 33029, float(u32(words, 33029))),
        battery_power_w=_maybe(words, 33149, power),
        battery_soc=_maybe(words, 33139, _scale(words, 33139, 1.0)),
        battery_soh=_maybe(words, 33140, _scale(words, 33140, 1.0)),
        battery_voltage_v=_maybe(words, 33133, _scale(words, 33133, 0.1)),
        battery_current_a=_maybe(words, 33134, current),
        battery_charge_today_kwh=_maybe(words, 33163, _scale(words, 33163, 0.1)),
        battery_discharge_today_kwh=_maybe(words, 33167, _scale(words, 33167, 0.1)),
        battery_charge_total_kwh=_maybe(words, 33161, float(u32(words, 33161))),
        battery_discharge_total_kwh=_maybe(words, 33165, float(u32(words, 33165))),
        grid_power_w=None,
        consumption_power_w=_maybe(words, 33147, _scale(words, 33147, 1.0)),
        force_charge_soc=None,
        force_discharge_soc=None,
        phases=phases,
        strings=_hybrid_strings(words),
        data_source=source,
    )
