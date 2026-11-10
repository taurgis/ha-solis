"""Move hultenvp/solis-sensor entity and device ids onto stable keys.

Upstream unique ids were ``{serial}{config name} {sensor label}`` with spaces
turned into underscores, so renaming the entry orphaned history. The new id
is ``{serial}_{key}`` and does not include the display name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_LEGACY_NAME, DOMAIN

# Labels are the English strings hultenvp/solis-sensor used in SENSOR_TYPES.
LEGACY_LABELS: dict[str, str] = {
    "ac_power": "AC Output Total Power",
    "frequency": "AC Frequency",
    "temperature": "Temperature",
    "energy_today": "Energy Today",
    "energy_month": "Energy This Month",
    "energy_year": "Energy This Year",
    "energy_total": "Energy Total",
    "battery_power": "Battery Power",
    "battery_soc": "Remaining Battery Capacity",
    "battery_soh": "Battery State Of Health",
    "battery_voltage": "Battery Voltage",
    "battery_current": "Battery Current",
    "battery_charge_today": "Daily Energy Charged",
    "battery_discharge_today": "Daily Energy Discharged",
    "battery_charge_total": "Total Energy Charged",
    "battery_discharge_total": "Total Energy Discharged",
    "grid_power": "Power Grid total power",
    "consumption_power": "Total Consumption power",
    "force_charge_soc": "Force Charge SOC",
    "force_discharge_soc": "Force Discharge SOC",
    "phase_1_voltage": "AC Voltage R",
    "phase_2_voltage": "AC Voltage S",
    "phase_3_voltage": "AC Voltage T",
    "phase_1_current": "AC Current R",
    "phase_2_current": "AC Current S",
    "phase_3_current": "AC Current T",
    "load_today": "Daily Grid Energy Used",
    "grid_import_today": "Daily Grid Energy Purchased",
    "grid_export_today": "Daily On-grid Energy",
}

_LEGACY_API_NAMES = ("SolisCloud", "Ginlong Platform 2.0")
# These counters moved from the inverter device onto the plant device.
_PLANT_KEYS = frozenset({"load_today", "grid_import_today", "grid_export_today"})

for _index in range(1, 25):
    LEGACY_LABELS[f"string_{_index}_voltage"] = f"DC Voltage PV{_index}"
    LEGACY_LABELS[f"string_{_index}_current"] = f"DC Current PV{_index}"
    LEGACY_LABELS[f"string_{_index}_power"] = f"DC Power PV{_index}"


def legacy_unique_id(serial: str, config_name: str, label: str) -> str:
    """Rebuild an upstream unique id."""
    return f"{serial}{config_name} {label}".replace(" ", "_")


def async_migrate_entity_ids(
    hass: HomeAssistant,
    entry_id: str,
    legacy_name: str,
    serials: set[str],
    serial_stations: Mapping[str, str] | None = None,
) -> None:
    """Point old entity registry rows at the stable unique ids.

    Safe to call on every poll. Inverters that were offline during the first
    poll are migrated when they appear. When a legacy row and a new row both
    exist, the legacy entity id is kept because that is the one with history,
    and the duplicate new row is removed.
    """
    registry = er.async_get(hass)
    names = {legacy_name, "Solis"}
    stations = serial_stations or {}
    for entity in list(registry.entities.values()):
        if entity.config_entry_id != entry_id or entity.platform != DOMAIN:
            continue
        matched = _match(entity.unique_id, names, serials)
        if matched is None:
            continue
        serial, key = matched
        if key in _PLANT_KEYS:
            station_id = stations.get(serial)
            if not station_id:
                continue
            new_unique_id = f"{station_id}_{key}"
        else:
            new_unique_id = f"{serial}_{key}"
        if new_unique_id == entity.unique_id:
            continue
        _retarget(registry, entity, new_unique_id)


def _retarget(registry: er.EntityRegistry, entity: er.RegistryEntry, new_unique_id: str) -> None:
    existing_id = registry.async_get_entity_id(entity.domain, DOMAIN, new_unique_id)
    if existing_id is not None and existing_id != entity.entity_id:
        registry.async_remove(existing_id)
    registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)


def _match(unique_id: str, names: set[str], serials: set[str]) -> tuple[str, str] | None:
    for serial in serials:
        for name in names:
            for key, label in LEGACY_LABELS.items():
                if unique_id == legacy_unique_id(serial, name, label):
                    return serial, key
    return None


def device_for_identifier(
    registry: dr.DeviceRegistry,
    identifier: tuple[str, str],
    entry_id: str,
) -> dr.DeviceEntry | None:
    """Return the device for an identifier owned by this config entry.

    ``async_get_device_by_identifier`` arrived after Home Assistant 2025.10.
    Older cores resolve the same row with ``async_get_device``.
    """
    finder = getattr(registry, "async_get_device_by_identifier", None)
    if finder is not None:
        return cast("dr.DeviceEntry | None", finder(identifier, entry_id))
    device = registry.async_get_device(identifiers={identifier})
    if device is None or entry_id not in device.config_entries:
        return None
    return device


def async_migrate_devices(hass: HomeAssistant, entry_id: str, serials: set[str]) -> None:
    """Retarget devices created as ``{serial}_{api name}``.

    When the new device already exists, the legacy device is detached from
    this entry so a partial migration does not leave two devices.
    """
    registry = dr.async_get(hass)
    for serial in serials:
        current = device_for_identifier(registry, (DOMAIN, f"inverter:{serial}"), entry_id)
        legacy = None
        for api_name in _LEGACY_API_NAMES:
            legacy = device_for_identifier(
                registry,
                (DOMAIN, f"{serial}_{api_name}"),
                entry_id,
            )
            if legacy is not None:
                break
        if legacy is None:
            continue
        if current is None:
            registry.async_update_device(
                legacy.id,
                new_identifiers={(DOMAIN, f"inverter:{serial}")},
            )
            continue
        if current.id != legacy.id:
            registry.async_update_device(legacy.id, remove_config_entry_id=entry_id)


def legacy_name_from_entry(data: Mapping[str, Any]) -> str:
    """Return the name upstream baked into unique ids."""
    return data.get(CONF_LEGACY_NAME) or data.get("name") or "Solis"
