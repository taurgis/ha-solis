"""Move hultenvp/solis-sensor entity and device ids onto stable keys.

Upstream unique ids were ``{serial}{config name} {sensor label}`` with spaces
turned into underscores, so renaming the entry orphaned history. The new id
is ``{serial}_{key}`` and does not include the display name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


def legacy_unique_id(serial: str, config_name: str, label: str) -> str:
    """Rebuild an upstream unique id."""
    return f"{serial}{config_name} {label}".replace(" ", "_")


def async_migrate_entity_ids(
    hass: HomeAssistant, entry_id: str, legacy_name: str, serials: set[str]
) -> None:
    """Point old entity registry rows at the stable unique ids."""
    registry = er.async_get(hass)
    names = {legacy_name, "Solis"}
    claimed = {entity.unique_id for entity in registry.entities.values()}
    for entity in list(registry.entities.values()):
        if entity.config_entry_id != entry_id or entity.platform != DOMAIN:
            continue
        migrated = _match(entity.unique_id, names, serials)
        if migrated is None or migrated in claimed:
            continue
        claimed.add(migrated)
        registry.async_update_entity(entity.entity_id, new_unique_id=migrated)


def _match(unique_id: str, names: set[str], serials: set[str]) -> str | None:
    for serial in serials:
        for name in names:
            for key, label in LEGACY_LABELS.items():
                if unique_id == legacy_unique_id(serial, name, label):
                    return f"{serial}_{key}"
    return None


def async_migrate_devices(hass: HomeAssistant, entry_id: str, serials: set[str]) -> None:
    """Retarget devices created as ``{serial}_{api name}``."""
    registry = dr.async_get(hass)
    for serial in serials:
        current = registry.async_get_device_by_identifier((DOMAIN, f"inverter:{serial}"), entry_id)
        if current is not None:
            continue
        for api_name in _LEGACY_API_NAMES:
            device = registry.async_get_device_by_identifier(
                (DOMAIN, f"{serial}_{api_name}"),
                entry_id,
            )
            if device is None:
                continue
            registry.async_update_device(
                device.id,
                new_identifiers={(DOMAIN, f"inverter:{serial}")},
            )
            break


def legacy_name_from_entry(data: Mapping[str, Any]) -> str:
    """Return the name upstream baked into unique ids."""
    return data.get(CONF_LEGACY_NAME) or data.get("name") or "Solis"
