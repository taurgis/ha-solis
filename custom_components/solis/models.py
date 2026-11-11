"""Snapshots produced by every Solis transport.

Cloud, and later Modbus or Solarman, all end in these objects. Entity code
does not know which transport filled them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import STALE_AFTER


@dataclass(frozen=True, slots=True)
class PvString:
    """One PV input."""

    index: int
    voltage_v: float | None
    current_a: float | None
    power_w: float | None


@dataclass(frozen=True, slots=True)
class Phase:
    """One AC phase."""

    index: int
    voltage_v: float | None
    current_a: float | None
    power_w: float | None


@dataclass(frozen=True, slots=True)
class InverterSnapshot:
    """One inverter at one poll."""

    serial: str
    device_id: str
    station_id: str
    name: str
    model: str | None
    product_model: str | None
    hmi_version: str | None
    state: int | None
    ac_output_type: int | None
    data_timestamp: datetime | None
    collector_serial: str | None
    ac_power_w: float | None
    frequency_hz: float | None
    temperature_c: float | None
    energy_today_kwh: float | None
    energy_month_kwh: float | None
    energy_year_kwh: float | None
    energy_total_kwh: float | None
    battery_power_w: float | None
    battery_soc: float | None
    battery_soh: float | None
    battery_voltage_v: float | None
    battery_current_a: float | None
    battery_charge_today_kwh: float | None
    battery_discharge_today_kwh: float | None
    battery_charge_total_kwh: float | None
    battery_discharge_total_kwh: float | None
    grid_power_w: float | None
    consumption_power_w: float | None
    force_charge_soc: float | None
    force_discharge_soc: float | None
    phases: tuple[Phase, ...]
    strings: tuple[PvString, ...]
    # This poll could not read the inverter. Instant sensors hide the previous
    # sample; energy totals still publish it.
    unreachable: bool = False
    # local, cloud, asleep, or degraded. Asleep means the stick is powered off.
    # Degraded means local Modbus reset while a cloud sample is in use.
    data_source: str = "cloud"
    # Short Solis model code such as E4, kept apart from the series name.
    model_code: str | None = None

    def condition(self, now: datetime, stale_after: timedelta = STALE_AFTER) -> str:
        """Return online, offline, alarm, stale, or unknown."""
        timestamp = self.data_timestamp
        if self.unreachable or timestamp is None or now - timestamp > stale_after:
            return "stale"
        if self.state == 2:
            return "offline"
        if self.state == 3:
            return "alarm"
        if self.state == 1:
            return "online"
        return "unknown"

    @property
    def has_battery(self) -> bool:
        """Return whether this inverter reported a battery."""
        if self.product_model == "2":
            return True
        return any(
            value is not None
            for value in (
                self.battery_soc,
                self.battery_power_w,
                self.battery_voltage_v,
                self.battery_current_a,
            )
        )


@dataclass(frozen=True, slots=True)
class StationSnapshot:
    """One plant."""

    station_id: str
    name: str
    timezone_offset_hours: float | None
    day_energy_kwh: float | None
    grid_import_today_kwh: float | None
    grid_export_today_kwh: float | None
    load_today_kwh: float | None
    capacity_kw: float | None
    address: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    """One alarm row. State ``0`` is still pending."""

    alarm_id: str
    serial: str
    station_id: str
    code: str
    level: str
    message: str
    advice: str
    state: str
    began_at: datetime | None

    @property
    def active(self) -> bool:
        """Return whether Solis still marks the alarm pending."""
        return self.state == "0"


@dataclass(frozen=True, slots=True)
class CollectorSnapshot:
    """A data logger stick."""

    serial: str
    station_id: str | None
    model: str | None
    state: int | None
    rssi: float | None


@dataclass(frozen=True, slots=True)
class EpmSnapshot:
    """An export power manager."""

    serial: str
    station_id: str | None
    state: int | None
    power_w: float | None


@dataclass(frozen=True, slots=True)
class SolisSnapshot:
    """Everything one poll managed to collect."""

    stations: tuple[StationSnapshot, ...]
    inverters: tuple[InverterSnapshot, ...]
    alarms: tuple[AlarmSnapshot, ...]
    collectors: tuple[CollectorSnapshot, ...]
    epms: tuple[EpmSnapshot, ...]
    fetched_at: datetime
    partial: bool
    source: str = "cloud"

    def station(self, station_id: str) -> StationSnapshot | None:
        """Return the station with this id."""
        for station in self.stations:
            if station.station_id == station_id:
                return station
        return None

    def active_alarms(self, serial: str) -> tuple[AlarmSnapshot, ...]:
        """Return pending alarms for one inverter."""
        return tuple(alarm for alarm in self.alarms if alarm.serial == serial and alarm.active)


def snapshot_to_dict(snapshot: SolisSnapshot) -> dict[str, Any]:
    """Serialize a snapshot for Home Assistant storage.

    Plant address and coordinates are dropped. They are not needed to rebuild
    entities, and the storage file should not keep a location history.
    """
    raw = _jsonable(asdict(snapshot))
    if not isinstance(raw, dict):
        raise TypeError("snapshot did not serialize to an object")
    stations = raw.get("stations")
    if isinstance(stations, list):
        for station in stations:
            if isinstance(station, dict):
                station["address"] = None
                station["latitude"] = None
                station["longitude"] = None
    return raw


def snapshot_from_dict(raw: Mapping[str, Any]) -> SolisSnapshot:
    """Rebuild a snapshot stored by ``snapshot_to_dict``."""
    return SolisSnapshot(
        stations=tuple(_station(item) for item in _rows(raw, "stations")),
        inverters=tuple(_inverter(item) for item in _rows(raw, "inverters")),
        alarms=tuple(_alarm(item) for item in _rows(raw, "alarms")),
        collectors=tuple(_collector(item) for item in _rows(raw, "collectors")),
        epms=tuple(_epm(item) for item in _rows(raw, "epms")),
        fetched_at=_required_dt(raw.get("fetched_at")),
        partial=bool(raw.get("partial")),
        source=str(raw.get("source") or "cloud"),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _rows(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise TypeError(key)
    rows: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(key)
        rows.append(item)
    return rows


def _required_dt(value: Any) -> datetime:
    parsed = _optional_dt(value)
    if parsed is None:
        raise ValueError("missing timestamp")
    return parsed


def _optional_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_float(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(key)
    return float(value)


def _optional_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = _optional_float(raw, key)
    if value is None:
        return None
    return int(value)


def _optional_str(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    return str(value)


def _station(raw: Mapping[str, Any]) -> StationSnapshot:
    return StationSnapshot(
        station_id=str(raw["station_id"]),
        name=str(raw["name"]),
        timezone_offset_hours=_optional_float(raw, "timezone_offset_hours"),
        day_energy_kwh=_optional_float(raw, "day_energy_kwh"),
        grid_import_today_kwh=_optional_float(raw, "grid_import_today_kwh"),
        grid_export_today_kwh=_optional_float(raw, "grid_export_today_kwh"),
        load_today_kwh=_optional_float(raw, "load_today_kwh"),
        capacity_kw=_optional_float(raw, "capacity_kw"),
        address=_optional_str(raw, "address"),
        latitude=_optional_float(raw, "latitude"),
        longitude=_optional_float(raw, "longitude"),
    )


def _inverter(raw: Mapping[str, Any]) -> InverterSnapshot:
    phases = tuple(_phase(item) for item in _nested(raw, "phases"))
    strings = tuple(_string(item) for item in _nested(raw, "strings"))
    return InverterSnapshot(
        serial=str(raw["serial"]),
        device_id=str(raw["device_id"]),
        station_id=str(raw["station_id"]),
        name=str(raw["name"]),
        model=_optional_str(raw, "model"),
        product_model=_optional_str(raw, "product_model"),
        hmi_version=_optional_str(raw, "hmi_version"),
        state=_optional_int(raw, "state"),
        ac_output_type=_optional_int(raw, "ac_output_type"),
        data_timestamp=_optional_dt(raw.get("data_timestamp")),
        collector_serial=_optional_str(raw, "collector_serial"),
        ac_power_w=_optional_float(raw, "ac_power_w"),
        frequency_hz=_optional_float(raw, "frequency_hz"),
        temperature_c=_optional_float(raw, "temperature_c"),
        energy_today_kwh=_optional_float(raw, "energy_today_kwh"),
        energy_month_kwh=_optional_float(raw, "energy_month_kwh"),
        energy_year_kwh=_optional_float(raw, "energy_year_kwh"),
        energy_total_kwh=_optional_float(raw, "energy_total_kwh"),
        battery_power_w=_optional_float(raw, "battery_power_w"),
        battery_soc=_optional_float(raw, "battery_soc"),
        battery_soh=_optional_float(raw, "battery_soh"),
        battery_voltage_v=_optional_float(raw, "battery_voltage_v"),
        battery_current_a=_optional_float(raw, "battery_current_a"),
        battery_charge_today_kwh=_optional_float(raw, "battery_charge_today_kwh"),
        battery_discharge_today_kwh=_optional_float(raw, "battery_discharge_today_kwh"),
        battery_charge_total_kwh=_optional_float(raw, "battery_charge_total_kwh"),
        battery_discharge_total_kwh=_optional_float(raw, "battery_discharge_total_kwh"),
        grid_power_w=_optional_float(raw, "grid_power_w"),
        consumption_power_w=_optional_float(raw, "consumption_power_w"),
        force_charge_soc=_optional_float(raw, "force_charge_soc"),
        force_discharge_soc=_optional_float(raw, "force_discharge_soc"),
        phases=phases,
        strings=strings,
        unreachable=bool(raw.get("unreachable", False)),
        data_source=str(raw.get("data_source") or "cloud"),
        model_code=_optional_str(raw, "model_code"),
    )


def _nested(raw: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = raw.get(key) or []
    if not isinstance(value, list):
        raise TypeError(key)
    rows: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError(key)
        rows.append(item)
    return rows


def _phase(raw: Mapping[str, Any]) -> Phase:
    return Phase(
        index=int(raw["index"]),
        voltage_v=_optional_float(raw, "voltage_v"),
        current_a=_optional_float(raw, "current_a"),
        power_w=_optional_float(raw, "power_w"),
    )


def _string(raw: Mapping[str, Any]) -> PvString:
    return PvString(
        index=int(raw["index"]),
        voltage_v=_optional_float(raw, "voltage_v"),
        current_a=_optional_float(raw, "current_a"),
        power_w=_optional_float(raw, "power_w"),
    )


def _alarm(raw: Mapping[str, Any]) -> AlarmSnapshot:
    return AlarmSnapshot(
        alarm_id=str(raw["alarm_id"]),
        serial=str(raw["serial"]),
        station_id=str(raw["station_id"]),
        code=str(raw["code"]),
        level=str(raw["level"]),
        message=str(raw["message"]),
        advice=str(raw["advice"]),
        state=str(raw["state"]),
        began_at=_optional_dt(raw.get("began_at")),
    )


def _collector(raw: Mapping[str, Any]) -> CollectorSnapshot:
    return CollectorSnapshot(
        serial=str(raw["serial"]),
        station_id=_optional_str(raw, "station_id"),
        model=_optional_str(raw, "model"),
        state=_optional_int(raw, "state"),
        rssi=_optional_float(raw, "rssi"),
    )


def _epm(raw: Mapping[str, Any]) -> EpmSnapshot:
    return EpmSnapshot(
        serial=str(raw["serial"]),
        station_id=_optional_str(raw, "station_id"),
        state=_optional_int(raw, "state"),
        power_w=_optional_float(raw, "power_w"),
    )
