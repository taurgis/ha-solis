"""Alarm binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SolisConfigEntry
from .const import DOMAIN
from .coordinator import SolisCoordinator
from .entity import SolisEntity, inverter_device
from .migration import async_migrate_entity_ids, legacy_name_from_entry

PARALLEL_UPDATES = 0

ALARM = BinarySensorEntityDescription(
    key="alarm",
    translation_key="alarm",
    device_class=BinarySensorDeviceClass.PROBLEM,
)


class SolisAlarmBinarySensor(SolisEntity, BinarySensorEntity):
    """On when the inverter has a pending SolisCloud alarm."""

    entity_description = ALARM

    def __init__(
        self,
        coordinator: SolisCoordinator,
        serial: str,
        station_name: str | None,
        via_device_id: str | None,
    ) -> None:
        """Bind the alarm to one inverter device."""
        inverter = next(item for item in coordinator.data.inverters if item.serial == serial)
        super().__init__(
            coordinator,
            ALARM,
            f"{serial}_alarm",
            inverter_device(inverter, station_name, via_device_id),
        )
        self._serial = serial

    @property
    def is_on(self) -> bool:
        """Return whether a pending alarm exists."""
        return bool(self.coordinator.data.active_alarms(self._serial))

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Expose the latest pending alarm text."""
        alarms = self.coordinator.data.active_alarms(self._serial)
        if not alarms:
            return {}
        current = alarms[0]
        return {"code": current.code, "message": current.message, "advice": current.advice}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolisConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add an alarm binary sensor per inverter."""
    solis_entry = entry
    coordinator = solis_entry.runtime_data
    added: set[str] = set()
    migrated = False

    @callback
    def _sync() -> None:
        nonlocal migrated
        names = {station.station_id: station.name for station in coordinator.data.stations}
        if not migrated:
            async_migrate_entity_ids(
                hass,
                solis_entry.entry_id,
                legacy_name_from_entry(solis_entry.data),
                {inverter.serial for inverter in coordinator.data.inverters},
            )
            migrated = True
        fresh = []
        for inverter in coordinator.data.inverters:
            if inverter.serial in added:
                continue
            added.add(inverter.serial)
            parent = dr.async_get(hass).async_get_device_by_identifier(
                (DOMAIN, f"station:{inverter.station_id}"),
                solis_entry.entry_id,
            )
            fresh.append(
                SolisAlarmBinarySensor(
                    coordinator,
                    inverter.serial,
                    names.get(inverter.station_id),
                    None if parent is None else parent.id,
                )
            )
        if fresh:
            async_add_entities(fresh)

    solis_entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()
