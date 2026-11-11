"""Sensors for Solis plants, inverters, strings, loggers and EPMs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import SolisConfigEntry
from .const import CONF_TIME_ZONE, DOMAIN, MANUFACTURER
from .coordinator import SolisCoordinator
from .energy import consider_energy, sanitize_kwh
from .entity import SolisEntity, collector_device, epm_device, inverter_device, station_device
from .migration import async_migrate_devices, async_migrate_entity_ids, legacy_name_from_entry
from .models import InverterSnapshot, SolisSnapshot
from .parser import plant_period_start, resolve_zone

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SolisSensorDescription(SensorEntityDescription):
    """Sensor description plus how to read it."""

    kind: str
    instant: bool = False
    period: str | None = None


def _measurement(
    key: str,
    *,
    kind: str,
    device_class: SensorDeviceClass | None,
    unit: str | None,
    instant: bool = False,
    period: str | None = None,
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT,
    category: EntityCategory | None = None,
    enabled_default: bool = True,
    precision: int | None = None,
    options: list[str] | None = None,
) -> SolisSensorDescription:
    return SolisSensorDescription(
        key=key,
        translation_key=key,
        kind=kind,
        instant=instant,
        period=period,
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=(
            None if options else SensorStateClass.TOTAL_INCREASING if period else state_class
        ),
        entity_category=category,
        entity_registry_enabled_default=enabled_default,
        suggested_display_precision=precision,
        options=options,
    )


_INVERTER: tuple[SolisSensorDescription, ...] = (
    _measurement(
        "status",
        kind="inverter",
        device_class=SensorDeviceClass.ENUM,
        unit=None,
        state_class=None,
        options=["online", "offline", "alarm", "stale", "unknown"],
    ),
    _measurement(
        "data_source",
        kind="inverter",
        device_class=SensorDeviceClass.ENUM,
        unit=None,
        state_class=None,
        options=["local", "cloud", "asleep"],
        category=EntityCategory.DIAGNOSTIC,
    ),
    _measurement(
        "ac_power",
        kind="inverter",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
    ),
    _measurement(
        "frequency",
        kind="inverter",
        device_class=SensorDeviceClass.FREQUENCY,
        unit=UnitOfFrequency.HERTZ,
        instant=True,
        precision=2,
    ),
    _measurement(
        "temperature",
        kind="inverter",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        instant=True,
        precision=1,
    ),
    _measurement(
        "energy_today",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "energy_month",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="month",
        precision=2,
    ),
    _measurement(
        "energy_year",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="year",
        precision=2,
    ),
    _measurement(
        "energy_total",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        precision=2,
    ),
    _measurement(
        "data_timestamp",
        kind="inverter",
        device_class=SensorDeviceClass.TIMESTAMP,
        unit=None,
        state_class=None,
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
    ),
    _measurement(
        "grid_power",
        kind="inverter",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
    ),
    _measurement(
        "consumption_power",
        kind="inverter",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
    ),
)

_BATTERY: tuple[SolisSensorDescription, ...] = (
    _measurement(
        "battery_power",
        kind="inverter",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
    ),
    _measurement(
        "battery_soc",
        kind="inverter",
        device_class=SensorDeviceClass.BATTERY,
        unit=PERCENTAGE,
        precision=0,
    ),
    _measurement(
        "battery_soh",
        kind="inverter",
        device_class=SensorDeviceClass.BATTERY,
        unit=PERCENTAGE,
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        precision=0,
    ),
    _measurement(
        "battery_voltage",
        kind="inverter",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
        instant=True,
        precision=1,
    ),
    _measurement(
        "battery_current",
        kind="inverter",
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
        instant=True,
        precision=2,
    ),
    _measurement(
        "battery_charge_today",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "battery_discharge_today",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "battery_charge_total",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        precision=2,
    ),
    _measurement(
        "battery_discharge_total",
        kind="inverter",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        precision=2,
    ),
    _measurement(
        "force_charge_soc",
        kind="inverter",
        device_class=SensorDeviceClass.BATTERY,
        unit=PERCENTAGE,
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        precision=0,
    ),
    _measurement(
        "force_discharge_soc",
        kind="inverter",
        device_class=SensorDeviceClass.BATTERY,
        unit=PERCENTAGE,
        category=EntityCategory.DIAGNOSTIC,
        enabled_default=False,
        precision=0,
    ),
)

_STATION: tuple[SolisSensorDescription, ...] = (
    _measurement(
        "day_energy",
        kind="station",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "grid_import_today",
        kind="station",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "grid_export_today",
        kind="station",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
    _measurement(
        "load_today",
        kind="station",
        device_class=SensorDeviceClass.ENERGY,
        unit=UnitOfEnergy.KILO_WATT_HOUR,
        period="day",
        precision=2,
    ),
)

_PHASE = {
    "voltage": _measurement(
        "phase_voltage",
        kind="phase",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
        instant=True,
        precision=1,
    ),
    "current": _measurement(
        "phase_current",
        kind="phase",
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
        instant=True,
        precision=2,
    ),
    "power": _measurement(
        "phase_power",
        kind="phase",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
    ),
}

_STRING = {
    "voltage": _measurement(
        "string_voltage",
        kind="string",
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
        instant=True,
        precision=1,
    ),
    "current": _measurement(
        "string_current",
        kind="string",
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
        instant=True,
        precision=2,
    ),
    "power": _measurement(
        "string_power",
        kind="string",
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
        instant=True,
        precision=0,
        enabled_default=False,
    ),
}


def _power(value: float | None, condition: str) -> float | None:
    """Zero a live power reading when the inverter is offline, and hide stale ones."""
    if condition == "stale":
        return None
    if condition == "offline":
        return 0.0
    return value


def _inverter_value(inverter: InverterSnapshot, key: str, now: datetime) -> StateType | datetime:
    condition = inverter.condition(now)
    values: dict[str, Any] = {
        "status": condition,
        "data_source": inverter.data_source,
        "ac_power": _power(inverter.ac_power_w, condition),
        "frequency": None if condition == "stale" else inverter.frequency_hz,
        "temperature": None if condition == "stale" else inverter.temperature_c,
        "energy_today": inverter.energy_today_kwh,
        "energy_month": inverter.energy_month_kwh,
        "energy_year": inverter.energy_year_kwh,
        "energy_total": inverter.energy_total_kwh,
        "data_timestamp": inverter.data_timestamp,
        "grid_power": _power(inverter.grid_power_w, condition),
        "consumption_power": _power(inverter.consumption_power_w, condition),
        "battery_power": _power(inverter.battery_power_w, condition),
        "battery_soc": inverter.battery_soc,
        "battery_soh": inverter.battery_soh,
        "battery_voltage": None if condition == "stale" else inverter.battery_voltage_v,
        "battery_current": None if condition == "stale" else inverter.battery_current_a,
        "battery_charge_today": inverter.battery_charge_today_kwh,
        "battery_discharge_today": inverter.battery_discharge_today_kwh,
        "battery_charge_total": inverter.battery_charge_total_kwh,
        "battery_discharge_total": inverter.battery_discharge_total_kwh,
        "force_charge_soc": inverter.force_charge_soc,
        "force_discharge_soc": inverter.force_discharge_soc,
    }
    return values.get(key)


class SolisSensor(SolisEntity, RestoreSensor, SensorEntity):
    """One Solis sensor."""

    entity_description: SolisSensorDescription

    def __init__(
        self,
        coordinator: SolisCoordinator,
        description: SolisSensorDescription,
        unique_id: str,
        device_info: DeviceInfo,
        *,
        serial: str | None = None,
        station_id: str | None = None,
        index: int | None = None,
    ) -> None:
        """Store which snapshot object this sensor reads."""
        super().__init__(coordinator, description, unique_id, device_info)
        self._serial = serial
        self._station_id = station_id
        self._index = index
        self._energy_last: float | None = None
        self._period_key: str | None = None
        if index is not None:
            self._attr_translation_placeholders = {"index": str(index)}

    async def async_added_to_hass(self) -> None:
        """Restore the last good energy total so a restart cannot open a dip."""
        await super().async_added_to_hass()
        if not self._tracks_energy():
            return
        restored = await self.async_get_last_sensor_data()
        if restored is None:
            return
        value = sanitize_kwh(
            restored.native_value if isinstance(restored.native_value, (int, float)) else None
        )
        if value is None:
            return
        if restored.native_unit_of_measurement != self.native_unit_of_measurement:
            return
        if self.entity_description.period:
            previous = await self.async_get_last_state()
            start = self._period_start(dt_util.utcnow())
            if previous is None or previous.last_updated < start:
                return
            self._period_key = start.isoformat()
        self._energy_last = value

    @property
    def available(self) -> bool:
        """Hide instantaneous values once the sample itself is stale."""
        if not super().available:
            return False
        if not self.entity_description.instant:
            return self._target() is not None
        inverter = self._inverter()
        if inverter is None:
            return self._target() is not None
        return inverter.condition(dt_util.utcnow()) != "stale"

    @property
    def native_value(self) -> StateType | datetime:
        """Return the current reading, holding energy across nulls and dips."""
        now = dt_util.utcnow()
        raw = self._raw_value(now)
        if not self._tracks_energy():
            return raw
        return self._guard_energy(raw, now)

    def _raw_value(self, now: datetime) -> StateType | datetime:
        target = self._target()
        key = self.entity_description.key
        if isinstance(target, InverterSnapshot):
            if self.entity_description.kind == "phase" and self._index is not None:
                return _phase_value(target, key, self._index, now)
            if self.entity_description.kind == "string" and self._index is not None:
                return _string_value(target, key, self._index, now)
            return _inverter_value(target, key, now)
        if target is None:
            return None
        return getattr(target, _STATION_ATTR.get(key, key), None)

    def _tracks_energy(self) -> bool:
        description = self.entity_description
        return (
            description.device_class is SensorDeviceClass.ENERGY
            and description.state_class is SensorStateClass.TOTAL_INCREASING
        )

    def _guard_energy(self, raw: StateType | datetime, now: datetime) -> float | None:
        value = sanitize_kwh(raw if isinstance(raw, (int, float)) else None)
        period = self.entity_description.period
        period_key = None if period is None else self._period_start(now).isoformat()
        changed = bool(self._period_key) and period_key != self._period_key
        published = consider_energy(
            self._energy_last,
            value,
            monotonic=period is None,
            period_changed=changed,
        )
        if isinstance(value, float) and published != value:
            _LOGGER.debug(
                "Holding %s at %s instead of %s",
                self.entity_description.key,
                published,
                value,
            )
        self._period_key = period_key
        if changed or published is not None:
            self._energy_last = published
        return published

    def _period_start(self, now: datetime) -> datetime:
        zone = resolve_zone(
            self._offset(),
            str(self.coordinator.config_entry.options.get(CONF_TIME_ZONE, "")),
            self.coordinator.hass.config.time_zone,
            now,
        )
        return plant_period_start(
            self._offset(),
            now,
            self.entity_description.period or "day",
            zone,
        )

    def _target(self) -> Any:
        data = self.coordinator.data
        if self._serial and self.entity_description.kind in {"inverter", "phase", "string"}:
            return self._inverter()
        if self._station_id and self.entity_description.kind == "station":
            return data.station(self._station_id)
        if self._serial and self.entity_description.kind == "collector":
            for collector in data.collectors:
                if collector.serial == self._serial:
                    return collector
        if self._serial and self.entity_description.kind == "epm":
            for epm in data.epms:
                if epm.serial == self._serial:
                    return epm
        return None

    def _inverter(self) -> InverterSnapshot | None:
        for inverter in self.coordinator.data.inverters:
            if inverter.serial == self._serial:
                return inverter
        return None

    def _offset(self) -> float:
        station_id = self._station_id
        if station_id is None:
            inverter = self._inverter()
            station_id = inverter.station_id if inverter else None
        if station_id is None:
            return 0.0
        station = self.coordinator.data.station(station_id)
        if station is None or station.timezone_offset_hours is None:
            return 0.0
        return station.timezone_offset_hours


_STATION_ATTR = {
    "day_energy": "day_energy_kwh",
    "grid_import_today": "grid_import_today_kwh",
    "grid_export_today": "grid_export_today_kwh",
    "load_today": "load_today_kwh",
    "rssi": "rssi",
    "power": "power_w",
}


def _phase_value(inverter: InverterSnapshot, key: str, index: int, now: datetime) -> float | None:
    condition = inverter.condition(now)
    for phase in inverter.phases:
        if phase.index != index:
            continue
        if key == "phase_voltage":
            return None if condition == "stale" else phase.voltage_v
        if key == "phase_current":
            return None if condition == "stale" else phase.current_a
        return _power(phase.power_w, condition)
    return None


def _string_value(inverter: InverterSnapshot, key: str, index: int, now: datetime) -> float | None:
    condition = inverter.condition(now)
    for pv in inverter.strings:
        if pv.index != index:
            continue
        if key == "string_voltage":
            return None if condition == "stale" else pv.voltage_v
        if key == "string_current":
            return None if condition == "stale" else pv.current_a
        return _power(pv.power_w, condition)
    return None


def _parent_devices(hass: HomeAssistant, entry_id: str, data: SolisSnapshot) -> dict[str, str]:
    """Register plant devices and return their registry ids."""
    registry = dr.async_get(hass)
    parents: dict[str, str] = {}
    for station in data.stations:
        device = registry.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, f"station:{station.station_id}")},
            manufacturer=MANUFACTURER,
            name=station.name,
            model="SolisCloud plant",
        )
        parents[station.station_id] = device.id
    return parents


@dataclass
class _Seen:
    """Sensor keys that have appeared, so one null poll does not delete them."""

    battery_keys: dict[str, set[str]] = field(default_factory=dict)
    phases: dict[str, set[int]] = field(default_factory=dict)
    strings: dict[str, set[int]] = field(default_factory=dict)

    def retain(self, bucket: dict[str, set[int]], serial: str, current: set[int]) -> set[int]:
        """Remember indexes and return every index still worth an entity."""
        known = bucket.setdefault(serial, set())
        known.update(current)
        return set(known)

    def drop_absent(self, serials: set[str]) -> None:
        """Forget devices that the cloud no longer lists."""
        self.battery_keys = {
            key: value for key, value in self.battery_keys.items() if key in serials
        }
        self.phases = {key: value for key, value in self.phases.items() if key in serials}
        self.strings = {key: value for key, value in self.strings.items() if key in serials}


def _entities(
    coordinator: SolisCoordinator,
    parents: dict[str, str],
    seen: _Seen,
) -> list[SolisSensor]:
    data: SolisSnapshot = coordinator.data
    entities: list[SolisSensor] = []
    names = {station.station_id: station.name for station in data.stations}
    for station in data.stations:
        device = station_device(station)
        for description in _STATION:
            entities.append(
                SolisSensor(
                    coordinator,
                    description,
                    f"{station.station_id}_{description.key}",
                    device,
                    station_id=station.station_id,
                )
            )
    for inverter in data.inverters:
        device = inverter_device(
            inverter,
            names.get(inverter.station_id),
            parents.get(inverter.station_id),
        )
        entities.extend(_inverter_entities(coordinator, inverter, device, seen))
    for collector in data.collectors:
        device = collector_device(collector, parents.get(collector.station_id or ""))
        entities.append(
            SolisSensor(
                coordinator,
                _measurement(
                    "rssi",
                    kind="collector",
                    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                    unit="dBm",
                    state_class=SensorStateClass.MEASUREMENT,
                    category=EntityCategory.DIAGNOSTIC,
                    enabled_default=False,
                ),
                f"{collector.serial}_rssi",
                device,
                serial=collector.serial,
            )
        )
    for epm in data.epms:
        device = epm_device(epm, parents.get(epm.station_id or ""))
        entities.append(
            SolisSensor(
                coordinator,
                _measurement(
                    "power",
                    kind="epm",
                    device_class=SensorDeviceClass.POWER,
                    unit=UnitOfPower.WATT,
                    instant=True,
                    precision=0,
                ),
                f"{epm.serial}_power",
                device,
                serial=epm.serial,
            )
        )
    return entities


def _inverter_entities(
    coordinator: SolisCoordinator,
    inverter: InverterSnapshot,
    device: DeviceInfo,
    seen: _Seen,
) -> list[SolisSensor]:
    rows = [
        SolisSensor(
            coordinator,
            description,
            f"{inverter.serial}_{description.key}",
            device,
            serial=inverter.serial,
        )
        for description in _INVERTER
    ]
    if inverter.has_battery or inverter.serial in seen.battery_keys:
        known = seen.battery_keys.setdefault(inverter.serial, set())
        for description in _BATTERY:
            present = getattr(inverter, _BATTERY_ATTR[description.key]) is not None
            if present or description.key == "battery_power":
                known.add(description.key)
            if description.key not in known:
                continue
            rows.append(
                SolisSensor(
                    coordinator,
                    description,
                    f"{inverter.serial}_{description.key}",
                    device,
                    serial=inverter.serial,
                )
            )
    phase_indexes = seen.retain(
        seen.phases,
        inverter.serial,
        {phase.index for phase in inverter.phases},
    )
    for index in sorted(phase_indexes):
        for metric, description in _PHASE.items():
            rows.append(
                SolisSensor(
                    coordinator,
                    description,
                    f"{inverter.serial}_phase_{index}_{metric}",
                    device,
                    serial=inverter.serial,
                    index=index,
                )
            )
    string_indexes = seen.retain(
        seen.strings,
        inverter.serial,
        {pv.index for pv in inverter.strings},
    )
    for index in sorted(string_indexes):
        for metric, description in _STRING.items():
            rows.append(
                SolisSensor(
                    coordinator,
                    description,
                    f"{inverter.serial}_string_{index}_{metric}",
                    device,
                    serial=inverter.serial,
                    index=index,
                )
            )
    return rows


_BATTERY_ATTR = {
    "battery_power": "battery_power_w",
    "battery_soc": "battery_soc",
    "battery_soh": "battery_soh",
    "battery_voltage": "battery_voltage_v",
    "battery_current": "battery_current_a",
    "battery_charge_today": "battery_charge_today_kwh",
    "battery_discharge_today": "battery_discharge_today_kwh",
    "battery_charge_total": "battery_charge_total_kwh",
    "battery_discharge_total": "battery_discharge_total_kwh",
    "force_charge_soc": "force_charge_soc",
    "force_discharge_soc": "force_discharge_soc",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolisConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors and keep them aligned with later polls."""
    coordinator: SolisCoordinator = entry.runtime_data
    added: dict[str, SolisSensor] = {}
    seen = _Seen()

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        serials = {inverter.serial for inverter in coordinator.data.inverters}
        async_migrate_entity_ids(
            hass,
            entry.entry_id,
            legacy_name_from_entry(entry.data),
            serials,
            {inverter.serial: inverter.station_id for inverter in coordinator.data.inverters},
        )
        async_migrate_devices(hass, entry.entry_id, serials)
        if not coordinator.data.partial:
            seen.drop_absent(serials)
        fresh: list[SolisSensor] = []
        parents = _parent_devices(hass, entry.entry_id, coordinator.data)
        wanted = _entities(coordinator, parents, seen)
        wanted_ids = {entity.unique_id for entity in wanted if entity.unique_id}
        for entity in wanted:
            if entity.unique_id and entity.unique_id not in added:
                added[entity.unique_id] = entity
                fresh.append(entity)
        if not coordinator.data.partial:
            _remove_missing(hass, entry, coordinator.data, added, wanted_ids)
        if fresh:
            async_add_entities(fresh)

    entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()


def _remove_missing(
    hass: HomeAssistant,
    entry: SolisConfigEntry,
    data: SolisSnapshot,
    added: dict[str, SolisSensor],
    wanted_ids: set[str],
) -> None:
    for unique_id, entity in list(added.items()):
        if unique_id in wanted_ids or entity.hass is None:
            continue
        entry.async_create_background_task(
            hass,
            entity.async_remove(),
            name=f"solis_remove_{unique_id}",
        )
        del added[unique_id]
    entry_id = entry.entry_id
    registry = dr.async_get(hass)
    present = {f"inverter:{inverter.serial}" for inverter in data.inverters}
    present.update(f"station:{station.station_id}" for station in data.stations)
    present.update(f"collector:{collector.serial}" for collector in data.collectors)
    present.update(f"epm:{epm.serial}" for epm in data.epms)
    for device in dr.async_entries_for_config_entry(registry, entry_id):
        identifier = next((item[1] for item in device.identifiers if item[0] == DOMAIN), None)
        if identifier is not None and identifier not in present:
            registry.async_update_device(device.id, remove_config_entry_id=entry_id)
