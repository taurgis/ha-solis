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


def _reading(data: dict[str, Any], *keys: str) -> tuple[Any, Any, Any]:
    """Return the first present value with its own ``*Str`` (or ``*Unit``) and ``*Pec``.

    The unit and scale factor are read from the same key that supplied the
    value, so an alias such as ``bstteryCurrent`` never borrows another key's
    unit.
    """
    for key in keys:
        if key in data and not is_missing(data[key]):
            unit = first(data, f"{key}Str", f"{key}Unit")
            return data[key], unit, first(data, f"{key}Pec")
    return None, None, None


def _watts(data: dict[str, Any], *keys: str, default: str = "kw") -> float | None:
    value, unit, pec = _reading(data, *keys)
    return to_watts(value, unit, pec, default=default)


def _kwh(data: dict[str, Any], *keys: str, default: str = "kwh") -> float | None:
    value, unit, pec = _reading(data, *keys)
    return to_kilowatt_hours(value, unit, pec, default=default)


def _ident(data: dict[str, Any], *keys: str, default: str) -> float | None:
    value, unit, pec = _reading(data, *keys)
    return to_identity(value, unit, pec, default=default)


def parse_epoch(value: Any) -> datetime | None:
    """Parse a Solis timestamp in epoch seconds or milliseconds."""
    number = as_float(value)
    if number is None:
        return None
    if number > 10_000_000_000:
        number = number / 1000.0
    if number < 1_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(number, UTC)
    except (OverflowError, ValueError, OSError):
        return None


def parse_offset_hours(value: Any) -> float | None:
    """Parse ``8``, ``-9.0``, ``UTC+08:00`` or ``(UTC+10:00)`` into hours east of UTC."""
    if is_missing(value):
        return None
    # stationDetail's ``timeZoneStr`` comes in brackets, e.g. "(UTC+10:00)".
    text = str(value).strip().strip("()").replace(" ", "").upper()
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
    plant_zone: str | None = None,
) -> ZoneInfo | None:
    """Choose the civil timezone used for "today", or None to keep the fixed offset.

    An explicit IANA name always wins. ``plant`` keeps the numeric offset
    Solis sent. The automatic default prefers the plant's own IANA zone
    (``timeZoneStandardId``) when it loads and agrees with that offset to
    within an hour. Without one it follows Home Assistant's zone when that
    zone is within an hour of the plant offset, which is the daylight-saving
    gap in issue 533, and leaves a plant in another region on the offset
    Solis published.

    Zone names are loaded with ``ZoneInfo``, which reads tzdata on a cache
    miss. Callers in the event loop warm the cache first (the cloud transport
    loads the plant zone in the executor and keeps a reference).
    """
    chosen = (preference or "").strip()
    if chosen == "plant":
        return None
    if chosen:
        return _zone(chosen)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    plant = _zone(plant_zone)
    if plant is not None and _within_hour(plant, cloud_offset_hours, now):
        return plant
    home = _zone(hass_time_zone)
    if home is not None and _within_hour(home, cloud_offset_hours, now):
        return home
    return None


def _within_hour(zone: ZoneInfo, offset_hours: float | None, now: datetime) -> bool:
    if offset_hours is None:
        return True
    current = now.astimezone(zone).utcoffset() or timedelta(0)
    return abs(current.total_seconds() / 3600.0 - offset_hours) <= 1.0


def _zone(name: str | None) -> ZoneInfo | None:
    if not name or name == "plant":
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
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
        voltage = _ident(data, f"uPv{index}", default="v")
        current = _ident(data, f"iPv{index}", default="a")
        power = _watts(data, f"pow{index}", default="w")
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
        voltage = _ident(data, f"uAc{index}", default="v")
        current = _ident(data, f"iAc{index}", default="a")
        power = _watts(data, power_key, default="w")
        rows.append(Phase(index=index, voltage_v=voltage, current_a=current, power_w=power))
    return tuple(rows)


def _battery_power(data: dict[str, Any], current: float | None) -> float | None:
    """Return signed battery watts. Positive is charging, negative is discharging.

    ``batteryPowerZheng`` and ``batteryPowerFu`` are the charge and discharge
    magnitudes. They are always watts and have no unit field of their own, so
    ``batteryPowerStr`` (usually ``kW``) does not apply to them: a real
    capture has ``batteryPower: -1.364`` kW with ``batteryPowerFu: 1364.0``.
    They decide the sign; the magnitude comes from ``batteryPower`` with its
    own ``*Str`` and ``*Pec``. Without ``batteryPower`` the reading is Zheng
    minus Fu in watts. Without either split, an unsigned ``batteryPower`` from
    a newer logger takes its sign from ``storageBatteryCurrent`` (positive while
    charging). An already-negative ``batteryPower`` is left as-is.
    """
    power = _watts(data, "batteryPower", default="kw")
    charging = to_watts(first(data, "batteryPowerZheng"), "w")
    discharging = to_watts(first(data, "batteryPowerFu"), "w")
    split = (charging or 0.0) - (discharging or 0.0)
    if power is None or (power == 0 and split != 0):
        if charging is None and discharging is None:
            return power
        return split
    if power == 0:
        return 0.0
    if split > 0:
        return abs(power)
    if split < 0:
        return -abs(power)
    if power > 0 and current is not None and current < 0:
        return -power
    return power


def parse_inverter(
    data: dict[str, Any],
    *,
    station_id: str | None = None,
    name: str | None = None,
    serial: str | None = None,
) -> InverterSnapshot:
    """Parse one inverter detail or, failing that, an inverter-list row.

    ``serial`` is the inverter-list serial. When given it wins over the
    detail's ``sn``, which SolisCloud replaces with a placeholder for a serial
    it does not know.
    """
    current = _ident(data, "storageBatteryCurrent", "bstteryCurrent", "batteryCurrent", default="a")
    product = _text(data, "productModel")
    series = _text(data, "machine", "series")
    code = _text(data, "model")
    if code and series and code == series:
        code = None
    return _plausible(
        InverterSnapshot(
            serial=str(serial or first(data, "sn") or ""),
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
            ac_power_w=_watts(data, "pac", default="kw"),
            frequency_hz=_ident(data, "fac", default="hz"),
            temperature_c=_ident(data, "inverterTemperature", default="c"),
            energy_today_kwh=_kwh(data, "eToday"),
            energy_month_kwh=_kwh(data, "eMonth"),
            energy_year_kwh=_kwh(data, "eYear"),
            energy_total_kwh=_kwh(data, "eTotal"),
            battery_power_w=_battery_power(data, current),
            battery_soc=_ident(data, "batteryCapacitySoc", default="%"),
            battery_soh=_ident(data, "batteryHealthSoh", default="%"),
            battery_voltage_v=_ident(data, "storageBatteryVoltage", "batteryVoltage", default="v"),
            battery_current_a=current,
            battery_charge_today_kwh=_kwh(data, "batteryTodayChargeEnergy"),
            battery_discharge_today_kwh=_kwh(data, "batteryTodayDischargeEnergy"),
            battery_charge_total_kwh=_kwh(data, "batteryTotalChargeEnergy"),
            battery_discharge_total_kwh=_kwh(data, "batteryTotalDischargeEnergy"),
            # psum is negative while importing and positive while exporting.
            grid_power_w=_watts(data, "psum", default="kw"),
            consumption_power_w=_watts(data, "familyLoadPower", default="kw"),
            force_charge_soc=_ident(data, "socChargingSet", default="%"),
            force_discharge_soc=_ident(data, "socDischargeSet", default="%"),
            phases=_phases(data),
            strings=_pv_strings(data),
            grid_import_total_kwh=_kwh(data, "gridPurchasedTotalEnergy"),
            grid_export_total_kwh=_kwh(data, "gridSellTotalEnergy"),
            load_total_kwh=_kwh(data, "homeLoadTotalEnergy"),
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
        day_energy_kwh=_energy_fallback(detail, day, ("dayEnergy",), ("energy",)),
        grid_import_today_kwh=_energy_fallback(
            detail,
            day,
            ("gridPurchasedDayEnergy", "gridPurchasedTodayEnergy"),
            ("gridPurchasedEnergy",),
        ),
        grid_export_today_kwh=_energy_fallback(
            detail,
            day,
            ("gridSellDayEnergy", "gridSellTodayEnergy"),
            ("gridSellEnergy",),
        ),
        # Station detail carries today's load as homeLoadEnergy too (upstream
        # reads that one); either saves the stationDayEnergyList call.
        load_today_kwh=_energy_fallback(
            detail,
            day,
            ("homeLoadTodayEnergy", "homeLoadDayEnergy", "homeLoadEnergy"),
            ("homeLoadEnergy",),
        ),
        capacity_kw=_kilowatts(_watts(detail, "capacity", default="kw")),
        address=_text(detail, "addr", "cityStr") or _text(summary, "cityStr"),
        latitude=as_float(first(detail, "latitude")),
        longitude=as_float(first(detail, "longitude")),
        timezone_name=plant_zone_name(detail) or plant_zone_name(summary),
    )


def plant_zone_name(data: dict[str, Any] | None) -> str | None:
    """Return the plant's IANA zone name from ``timeZoneStandardId``, if any.

    The name is not loaded here, because the parser runs in the event loop.
    ``resolve_zone`` drops a name that does not load.
    """
    if not data:
        return None
    value = (_text(data, "timeZoneStandardId") or "").strip()
    return value or None


def _energy_fallback(
    detail: dict[str, Any],
    day: dict[str, Any],
    detail_keys: tuple[str, ...],
    day_keys: tuple[str, ...],
) -> float | None:
    primary = _kwh(detail, *detail_keys)
    if primary is not None:
        return primary
    return _kwh(day, *day_keys)


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
        ended_at=parse_epoch(first(row, "alarmEndTime", "alarmEndtime")),
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
        power_w=_watts(row, "pEpmTotal", default="kw"),
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
