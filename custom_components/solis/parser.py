"""Turn SolisCloud JSON into snapshots.

Field names are the Platform API v2 names. ``dcInputType`` is a count minus
one in the field table (0 means one string). ``dataTimestamp`` is treated as a
Unix epoch even though the PDF says "under UTC+8": the published samples are
epoch milliseconds, and the companion string is the part that is rendered in
China Standard Time.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    AlarmSnapshot,
    CollectorSnapshot,
    EpmSnapshot,
    InverterSnapshot,
    Phase,
    PvString,
    StationSnapshot,
)
from .units import as_float, is_missing, to_identity, to_kilowatt_hours, to_watts, unit_key

_MAX_STRINGS = 24


def extract_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the row list from either ``data.page.records`` or ``data.records``."""
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    page = data.get("page")
    if isinstance(page, dict) and isinstance(page.get("records"), list):
        return [row for row in page["records"] if isinstance(row, dict)]
    records = data.get("records")
    if isinstance(records, list):
        return [row for row in records if isinstance(row, dict)]
    return []


def page_count(payload: dict[str, Any]) -> int | None:
    """Return the reported page count when the platform sent one."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    for container in (data, data.get("page") if isinstance(data.get("page"), dict) else {}):
        if isinstance(container, dict) and container.get("pages") is not None:
            number = as_float(container.get("pages"))
            if number is not None:
                return int(number)
    return None


def first(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-null field."""
    for key in keys:
        if key in data and not is_missing(data[key]):
            return data[key]
    return None


def parse_epoch(value: Any) -> datetime | None:
    """Parse a Solis timestamp in epoch seconds or milliseconds."""
    number = as_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number = number / 1000.0
    if number < 1_000_000_000:
        return None
    return datetime.fromtimestamp(number, UTC)


def parse_offset_hours(value: Any) -> float | None:
    """Parse ``8``, ``-9.0`` or ``UTC+08:00`` into hours east of UTC."""
    if is_missing(value):
        return None
    text = str(value).strip().upper()
    if text.startswith("UTC"):
        rest = text[3:]
        if not rest:
            return 0.0
        sign = -1.0 if rest[0] == "-" else 1.0
        clock = rest[1:] if rest[0] in "+-" else rest
        hours, _, minutes = clock.partition(":")
        try:
            return sign * (float(hours) + float(minutes or 0) / 60.0)
        except ValueError:
            return None
    return as_float(value)


def resolve_zone(
    cloud_offset_hours: float | None,
    preference: str | None,
    hass_time_zone: str | None,
    now: datetime,
) -> ZoneInfo | None:
    """Choose the civil timezone used for "today", or None to keep the fixed offset.

    An explicit IANA name always wins. ``plant`` keeps the numeric offset Solis
    sent. The default follows Home Assistant's zone when that zone is within an
    hour of the plant offset, which is the daylight-saving gap in issue 533,
    and leaves a plant in another region on the offset Solis published.
    """
    chosen = (preference or "").strip()
    if chosen == "plant":
        return None
    if chosen:
        return _zone(chosen)
    if cloud_offset_hours is None:
        return _zone(hass_time_zone)
    home = _zone(hass_time_zone)
    if home is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    home_offset = now.astimezone(home).utcoffset()
    if home_offset is None:
        return None
    if abs(home_offset.total_seconds() / 3600.0 - cloud_offset_hours) <= 1.0:
        return home
    return None


def _zone(name: str | None) -> ZoneInfo | None:
    if not name or name == "plant":
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def plant_period_start(
    offset_hours: float,
    now: datetime,
    period: str,
    zone: tzinfo | None = None,
) -> datetime:
    """Return the UTC instant when the current plant-local day, month or year began.

    ``zone`` follows daylight-saving transitions. Without it, the fixed numeric
    offset from Solis is used and does not change on those transitions.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if zone is not None:
        shifted = now.astimezone(zone)
        shifted = _truncate_period(shifted, period)
        return shifted.astimezone(UTC)
    shifted = now.astimezone(UTC) + timedelta(hours=offset_hours)
    return _truncate_period(shifted, period) - timedelta(hours=offset_hours)


def _truncate_period(moment: datetime, period: str) -> datetime:
    if period == "month":
        return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _text(data: dict[str, Any], *keys: str) -> str | None:
    value = first(data, *keys)
    if value is None:
        return None
    return str(value)


def _string_count(data: dict[str, Any]) -> int:
    raw = first(data, "dcInputType", "dcInputtype")
    if raw is not None:
        number = as_float(raw)
        if number is not None:
            return max(0, min(_MAX_STRINGS, int(number) + 1))
    count = 0
    for index in range(1, _MAX_STRINGS + 1):
        if first(data, f"uPv{index}") is not None:
            count = index
    return count


def _pv_strings(data: dict[str, Any]) -> tuple[PvString, ...]:
    rows: list[PvString] = []
    for index in range(1, _string_count(data) + 1):
        voltage = to_identity(
            first(data, f"uPv{index}"), first(data, f"uPv{index}Str"), default="v"
        )
        current = to_identity(
            first(data, f"iPv{index}"), first(data, f"iPv{index}Str"), default="a"
        )
        power = to_watts(first(data, f"pow{index}"), first(data, f"pow{index}Str"), default="w")
        if voltage is None and current is None and power is None:
            continue
        rows.append(PvString(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def _phases(data: dict[str, Any]) -> tuple[Phase, ...]:
    output = as_float(first(data, "acOutputType"))
    count = 1 if output == 0 else 3
    rows: list[Phase] = []
    for index, power_key in enumerate(("pA", "pB", "pC"), start=1):
        if index > count:
            break
        voltage = to_identity(
            first(data, f"uAc{index}"),
            first(data, f"uAc{index}Str"),
            default="v",
        )
        current = to_identity(
            first(data, f"iAc{index}"),
            first(data, f"iAc{index}Str"),
            default="a",
        )
        power = to_watts(first(data, power_key), first(data, f"{power_key}Str"), default="w")
        rows.append(Phase(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def _battery_power(data: dict[str, Any], current: float | None) -> float | None:
    """Return signed battery watts. Positive is charging, negative is discharging.

    ``batteryPowerZheng`` and ``batteryPowerFu`` are the charge and discharge
    components. A real SolisCloud day sample has ``batteryPower`` equal to
    Zheng minus Fu. Newer loggers also publish an unsigned ``batteryPower``;
    ``storageBatteryCurrent`` is then positive while charging and negative
    while discharging. An already-negative ``batteryPower`` is left as-is.
    """
    unit = first(data, "batteryPowerStr")
    pec = first(data, "batteryPowerPec")
    power = to_watts(first(data, "batteryPower"), unit, pec, default="kw")
    charging = to_watts(first(data, "batteryPowerZheng"), unit, default="kw")
    discharging = to_watts(first(data, "batteryPowerFu"), unit, default="kw")
    if (charging is not None and charging > 0) or (discharging is not None and discharging > 0):
        return (charging or 0.0) - (discharging or 0.0)
    if power is None:
        return None
    if power > 0 and current is not None and current < 0:
        return -power
    return power


def parse_inverter(
    data: dict[str, Any],
    *,
    station_id: str | None = None,
    name: str | None = None,
) -> InverterSnapshot:
    """Parse one inverter detail or, failing that, an inverter-list row."""
    current = to_identity(
        first(data, "storageBatteryCurrent", "bstteryCurrent", "batteryCurrent"),
        first(data, "storageBatteryCurrentStr", "bstteryCurrentStr", "batteryCurrentStr"),
        default="a",
    )
    product = _text(data, "productModel")
    series = _text(data, "machine", "series")
    code = _text(data, "model")
    if code and series and code == series:
        code = None
    return _plausible(
        InverterSnapshot(
            serial=str(first(data, "sn") or ""),
            device_id=str(first(data, "id") or ""),
            station_id=str(station_id or first(data, "stationId") or ""),
            name=str(name or first(data, "name", "stationName") or first(data, "sn") or "Solis"),
            model=series or code,
            product_model=product,
            hmi_version=_text(data, "version", "inverterVersion", "hmiVersionAll"),
            model_code=code if series else None,
            state=_as_int(first(data, "state")),
            ac_output_type=_as_int(first(data, "acOutputType")),
            data_timestamp=parse_epoch(first(data, "dataTimestamp")),
            collector_serial=_text(data, "collectorSn"),
            ac_power_w=to_watts(
                first(data, "pac"),
                first(data, "pacStr"),
                first(data, "pacPec"),
                default="kw",
            ),
            frequency_hz=to_identity(first(data, "fac"), first(data, "facStr"), default="hz"),
            temperature_c=to_identity(
                first(data, "inverterTemperature"),
                first(data, "inverterTemperatureStr"),
                default="c",
            ),
            energy_today_kwh=to_kilowatt_hours(
                first(data, "eToday"),
                first(data, "eTodayStr"),
                default="kwh",
            ),
            energy_month_kwh=to_kilowatt_hours(
                first(data, "eMonth"),
                first(data, "eMonthStr"),
                default="kwh",
            ),
            energy_year_kwh=to_kilowatt_hours(
                first(data, "eYear"),
                first(data, "eYearStr"),
                default="kwh",
            ),
            energy_total_kwh=to_kilowatt_hours(
                first(data, "eTotal"),
                first(data, "eTotalStr"),
                default="kwh",
            ),
            battery_power_w=_battery_power(data, current),
            battery_soc=to_identity(
                first(data, "batteryCapacitySoc"),
                first(data, "batteryCapacitySocStr"),
                default="%",
            ),
            battery_soh=to_identity(
                first(data, "batteryHealthSoh"),
                first(data, "batteryHealthSohStr"),
                default="%",
            ),
            battery_voltage_v=to_identity(
                first(data, "storageBatteryVoltage", "batteryVoltage"),
                first(data, "storageBatteryVoltageStr", "batteryVoltageStr"),
                default="v",
            ),
            battery_current_a=current,
            battery_charge_today_kwh=to_kilowatt_hours(
                first(data, "batteryTodayChargeEnergy"),
                first(data, "batteryTodayChargeEnergyStr"),
                default="kwh",
            ),
            battery_discharge_today_kwh=to_kilowatt_hours(
                first(data, "batteryTodayDischargeEnergy"),
                first(data, "batteryTodayDischargeEnergyStr"),
                default="kwh",
            ),
            battery_charge_total_kwh=to_kilowatt_hours(
                first(data, "batteryTotalChargeEnergy"),
                first(data, "batteryTotalChargeEnergyStr"),
                default="kwh",
            ),
            battery_discharge_total_kwh=to_kilowatt_hours(
                first(data, "batteryTotalDischargeEnergy"),
                first(data, "batteryTotalDischargeEnergyStr"),
                default="kwh",
            ),
            grid_power_w=to_watts(first(data, "psum"), first(data, "psumStr"), default="kw"),
            consumption_power_w=to_watts(
                first(data, "familyLoadPower"),
                first(data, "familyLoadPowerStr"),
                default="kw",
            ),
            force_charge_soc=to_identity(
                first(data, "socChargingSet"),
                first(data, "socChargingSetStr"),
                default="%",
            ),
            force_discharge_soc=to_identity(
                first(data, "socDischargeSet"),
                first(data, "socDischargeSetStr"),
                default="%",
            ),
            phases=_phases(data),
            strings=_pv_strings(data),
        )
    )


def _bounded(value: float | None, low: float, high: float) -> float | None:
    """Drop a reading outside a physical range. None stays None."""
    if value is None or value < low or value > high:
        return None
    return value


def _plausible(inverter: InverterSnapshot) -> InverterSnapshot:
    """Drop frequency, temperature and state-of-charge values that cannot be real."""
    return replace(
        inverter,
        frequency_hz=_bounded(inverter.frequency_hz, 40.0, 70.0),
        temperature_c=_bounded(inverter.temperature_c, -40.0, 150.0),
        battery_soc=_bounded(inverter.battery_soc, 0.0, 100.0),
        battery_soh=_bounded(inverter.battery_soh, 0.0, 100.0),
        force_charge_soc=_bounded(inverter.force_charge_soc, 0.0, 100.0),
        force_discharge_soc=_bounded(inverter.force_discharge_soc, 0.0, 100.0),
    )


def _as_int(value: Any) -> int | None:
    number = as_float(value)
    if number is None:
        return None
    return int(number)


def parse_station(
    summary: dict[str, Any],
    detail: dict[str, Any] | None,
    day: dict[str, Any] | None,
) -> StationSnapshot:
    """Merge a station list row, station detail and one day-energy row."""
    detail = detail or {}
    day = day or {}
    station_id = str(first(summary, "id") or first(detail, "id") or "")
    return StationSnapshot(
        station_id=station_id,
        name=str(
            first(summary, "stationName", "sno")
            or first(detail, "stationName", "sno")
            or station_id
        ),
        timezone_offset_hours=parse_offset_hours(
            first(detail, "timeZone", "timeZoneStr") or first(summary, "timeZone", "timeZoneStr")
        ),
        day_energy_kwh=_energy_fallback(
            detail,
            day,
            ("dayEnergy",),
            ("dayEnergyStr",),
            ("energy",),
            ("energyStr",),
        ),
        grid_import_today_kwh=_energy_fallback(
            detail,
            day,
            ("gridPurchasedDayEnergy", "gridPurchasedTodayEnergy"),
            ("gridPurchasedDayEnergyStr", "gridPurchasedTodayEnergyStr"),
            ("gridPurchasedEnergy",),
            ("gridPurchasedEnergyStr",),
        ),
        grid_export_today_kwh=_energy_fallback(
            detail,
            day,
            ("gridSellDayEnergy", "gridSellTodayEnergy"),
            ("gridSellDayEnergyStr", "gridSellTodayEnergyStr"),
            ("gridSellEnergy",),
            ("gridSellEnergyStr",),
        ),
        load_today_kwh=_energy_fallback(
            detail,
            day,
            ("homeLoadTodayEnergy", "homeLoadDayEnergy"),
            ("homeLoadTodayEnergyStr", "homeLoadDayEnergyStr"),
            ("homeLoadEnergy",),
            ("homeLoadEnergyStr",),
        ),
        capacity_kw=_kilowatts(
            to_watts(first(detail, "capacity"), first(detail, "capacityStr"), default="kw")
        ),
        address=_text(detail, "addr", "cityStr") or _text(summary, "cityStr"),
        latitude=as_float(first(detail, "latitude")),
        longitude=as_float(first(detail, "longitude")),
    )


def _energy_fallback(
    detail: dict[str, Any],
    day: dict[str, Any],
    detail_keys: tuple[str, ...],
    detail_units: tuple[str, ...],
    day_keys: tuple[str, ...],
    day_units: tuple[str, ...],
) -> float | None:
    primary = to_kilowatt_hours(
        first(detail, *detail_keys), first(detail, *detail_units), default="kwh"
    )
    if primary is not None:
        return primary
    return to_kilowatt_hours(first(day, *day_keys), first(day, *day_units), default="kwh")


def parse_alarm(row: dict[str, Any]) -> AlarmSnapshot:
    """Parse one alarm list row."""
    return AlarmSnapshot(
        alarm_id=str(first(row, "id") or ""),
        serial=str(first(row, "alarmDeviceSn", "sn") or ""),
        station_id=str(first(row, "stationId") or ""),
        code=str(first(row, "alarmCode") or ""),
        level=str(first(row, "alarmLevel") or ""),
        message=str(first(row, "alarmMsg") or ""),
        advice=str(first(row, "advice") or ""),
        state=str(first(row, "state") if first(row, "state") is not None else ""),
        began_at=parse_epoch(first(row, "alarmBeginTime", "alarmBegintime")),
    )


def parse_collector(row: dict[str, Any]) -> CollectorSnapshot:
    """Parse one collector list row."""
    return CollectorSnapshot(
        serial=str(first(row, "sn") or ""),
        station_id=_text(row, "stationId"),
        model=_text(row, "model", "machine"),
        state=_as_int(first(row, "state")),
        rssi=as_float(first(row, "rssi", "rssiLevel")),
    )


def parse_epm(row: dict[str, Any]) -> EpmSnapshot:
    """Parse one EPM list row."""
    return EpmSnapshot(
        serial=str(first(row, "sn") or ""),
        station_id=_text(row, "stationId"),
        state=_as_int(first(row, "state")),
        power_w=to_watts(first(row, "pEpmTotal"), first(row, "pEpmTotalStr"), default="kw"),
    )


def _kilowatts(watts: float | None) -> float | None:
    if watts is None:
        return None
    return watts / 1000.0


def local_date(
    offset_hours: float | None,
    now: datetime,
    zone: tzinfo | None = None,
) -> str:
    """Return the plant-local ``YYYY-MM-DD`` for a day-energy query."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if zone is not None:
        return now.astimezone(zone).date().isoformat()
    shifted = now.astimezone(UTC) + timedelta(hours=offset_hours or 0.0)
    return shifted.date().isoformat()


def unit_was_power_on_energy(unit: Any) -> bool:
    """Return whether a unit string used the documented kW-on-energy quirk."""
    return unit_key(unit) == "kw"
