"""Alarm binary sensors."""

from __future__ import annotations

try:
    from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
except ImportError:  # pragma: no cover  # Home Assistant 2025.10
    from homeassistant.components.binary_sensor import (  # type: ignore[attr-defined]
        BinarySensorDeviceClass,
    )
from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SolisConfigEntry
from .const import DOMAIN
from .coordinator import SolisCoordinator
from .entity import SolisEntity, inverter_device
from .migration import async_migrate_entity_ids, device_for_identifier, legacy_name_from_entry
from .models import InverterSnapshot

PARALLEL_UPDATES = 0

ALARM = BinarySensorEntityDescription(
    key="alarm",
    translation_key="alarm",
    device_class=BinarySensorDeviceClass.PROBLEM,
)

# Present on Home Assistant 2025.10 and newer. Guarded so an older core that
# lacks the enum still loads the alarm sensor.
_CHARGING_CLASS = getattr(BinarySensorDeviceClass, "BATTERY_CHARGING", None)
CHARGING = (
    BinarySensorEntityDescription(
        key="battery_charging",
        translation_key="battery_charging",
        device_class=_CHARGING_CLASS,
    )
    if _CHARGING_CLASS is not None
    else None
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


class SolisBatteryChargingBinarySensor(SolisEntity, BinarySensorEntity):
    """On while battery power is positive (charging)."""

    entity_description = CHARGING or ALARM

    def __init__(
        self,
        coordinator: SolisCoordinator,
        serial: str,
        station_name: str | None,
        via_device_id: str | None,
    ) -> None:
        """Bind the charging sensor to one hybrid inverter."""
        inverter = next(item for item in coordinator.data.inverters if item.serial == serial)
        description = CHARGING or ALARM
        super().__init__(
            coordinator,
            description,
            f"{serial}_battery_charging",
            inverter_device(inverter, station_name, via_device_id),
        )
        self._serial = serial

    @property
    def available(self) -> bool:
        """Hide the state when the inverter sample is stale or unread."""
        if not super().available:
            return False
        inverter = self._inverter()
        if inverter is None:
            return False
        return inverter.condition(dt_util.utcnow()) != "stale"

    @property
    def is_on(self) -> bool:
        """Return whether the battery is charging."""
        inverter = self._inverter()
        if inverter is None or inverter.battery_power_w is None:
            return False
        return inverter.battery_power_w > 0

    def _inverter(self) -> InverterSnapshot | None:
        for item in self.coordinator.data.inverters:
            if item.serial == self._serial:
                return item
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolisConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add an alarm binary sensor per inverter."""
    solis_entry = entry
    coordinator = solis_entry.runtime_data
    added_alarms: dict[str, SolisAlarmBinarySensor] = {}
    added_charging: dict[str, SolisBatteryChargingBinarySensor] = {}

    @callback
    def _sync() -> None:
        if coordinator.data is None:
            return
        serials = {inverter.serial for inverter in coordinator.data.inverters}
        async_migrate_entity_ids(
            hass,
            solis_entry.entry_id,
            legacy_name_from_entry(solis_entry.data),
            serials,
            {inverter.serial: inverter.station_id for inverter in coordinator.data.inverters},
        )
        names = {station.station_id: station.name for station in coordinator.data.stations}
        fresh: list[SolisAlarmBinarySensor | SolisBatteryChargingBinarySensor] = []
        for inverter in coordinator.data.inverters:
            parent = device_for_identifier(
                dr.async_get(hass),
                (DOMAIN, f"station:{inverter.station_id}"),
                solis_entry.entry_id,
            )
            via = None if parent is None else parent.id
            station_name = names.get(inverter.station_id)
            if inverter.serial not in added_alarms:
                entity = SolisAlarmBinarySensor(coordinator, inverter.serial, station_name, via)
                added_alarms[inverter.serial] = entity
                fresh.append(entity)
            if (
                CHARGING is not None
                and inverter.has_battery
                and inverter.serial not in added_charging
            ):
                charging = SolisBatteryChargingBinarySensor(
                    coordinator, inverter.serial, station_name, via
                )
                added_charging[inverter.serial] = charging
                fresh.append(charging)
        if not coordinator.data.partial:
            _drop_missing(added_alarms, serials, "alarm")
            _drop_missing(added_charging, serials, "battery_charging")
        if fresh:
            async_add_entities(fresh)

    def _drop_missing[EntityT: SolisEntity](
        added: dict[str, EntityT],
        serials: set[str],
        label: str,
    ) -> None:
        for serial, entity in list(added.items()):
            if serial in serials or entity.hass is None:
                continue
            solis_entry.async_create_background_task(
                hass,
                entity.async_remove(),
                name=f"solis_remove_{label}_{serial}",
            )
            del added[serial]

    solis_entry.async_on_unload(coordinator.async_add_listener(_sync))
    _sync()
