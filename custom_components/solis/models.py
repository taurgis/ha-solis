"""Snapshots produced by every Solis transport.

Cloud, and later Modbus or Solarman, all end in these objects. Entity code
does not know which transport filled them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

STALE_AFTER = timedelta(minutes=15)


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

    def condition(self, now: datetime, stale_after: timedelta = STALE_AFTER) -> str:
        """Return online, offline, alarm, stale, or unknown."""
        if self.data_timestamp is None or now - self.data_timestamp > stale_after:
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

    def station(self, station_id: str) -> StationSnapshot | None:
        """Return the station with this id."""
        for station in self.stations:
            if station.station_id == station_id:
                return station
        return None

    def active_alarms(self, serial: str) -> tuple[AlarmSnapshot, ...]:
        """Return pending alarms for one inverter."""
        return tuple(alarm for alarm in self.alarms if alarm.serial == serial and alarm.active)
