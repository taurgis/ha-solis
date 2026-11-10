"""Unique-id and device-id migration off hultenvp/solis-sensor."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.solis.const import DOMAIN
from custom_components.solis.migration import (
    async_migrate_devices,
    async_migrate_entity_ids,
    legacy_name_from_entry,
    legacy_unique_id,
)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=2)
    entry.add_to_hass(hass)
    return entry


def test_legacy_unique_id_replaces_spaces() -> None:
    """Upstream ids baked the config name and the sensor label into one string."""
    assert (
        legacy_unique_id("SN1", "North Roof", "AC Output Total Power")
        == "SN1North_Roof_AC_Output_Total_Power"
    )
    assert legacy_name_from_entry({"name": "Roof"}) == "Roof"
    assert legacy_name_from_entry({}) == "Solis"


async def test_migrates_known_labels_and_leaves_foreign_ids(hass: HomeAssistant) -> None:
    """Known upstream labels move, and unrelated entities stay put."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    cases = {
        legacy_unique_id("SN1", "Roof", "Energy Today"): "SN1_energy_today",
        legacy_unique_id("SN1", "Solis", "Battery Power"): "SN1_battery_power",
        legacy_unique_id("SN1", "Roof", "Daily On-grid Energy"): "plant-1_grid_export_today",
    }
    for old in cases:
        registry.async_get_or_create(
            domain="sensor",
            platform=DOMAIN,
            unique_id=old,
            config_entry=entry,
        )
    registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="leave-me",
        config_entry=entry,
    )
    async_migrate_entity_ids(hass, entry.entry_id, "Roof", {"SN1"}, {"SN1": "plant-1"})
    for old, new in cases.items():
        assert registry.async_get_entity_id("sensor", DOMAIN, old) is None
        assert registry.async_get_entity_id("sensor", DOMAIN, new) is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, "leave-me") is not None


async def test_duplicate_stable_id_keeps_the_legacy_entity(hass: HomeAssistant) -> None:
    """A second run keeps the legacy entity id, which is the one with history."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    old = legacy_unique_id("SN1", "Solis", "Temperature")
    legacy = registry.async_get_or_create(
        domain="sensor", platform=DOMAIN, unique_id=old, config_entry=entry
    )
    duplicate = registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id="SN1_temperature",
        config_entry=entry,
    )
    async_migrate_entity_ids(hass, entry.entry_id, "Solis", {"SN1"})
    assert registry.async_get_entity_id("sensor", DOMAIN, "SN1_temperature") == legacy.entity_id
    assert registry.async_get(duplicate.entity_id) is None
    async_migrate_entity_ids(hass, entry.entry_id, "Solis", {"SN1"})
    assert registry.async_get_entity_id("sensor", DOMAIN, "SN1_temperature") == legacy.entity_id


async def test_device_migration_from_ginlong_name(hass: HomeAssistant) -> None:
    """A Ginlong Platform 2.0 device id is retargeted to inverter:{serial}."""
    entry = _entry(hass)
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN9_Ginlong Platform 2.0")},
        name="old",
    )
    async_migrate_devices(hass, entry.entry_id, {"SN9"})
    assert (
        devices.async_get_device_by_identifier((DOMAIN, "inverter:SN9"), entry.entry_id) is not None
    )
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, "SN9_Ginlong Platform 2.0"),
            entry.entry_id,
        )
        is None
    )


async def test_plant_counter_moves_onto_the_station(hass: HomeAssistant) -> None:
    """Daily grid energy used to be keyed by the inverter serial."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    old = legacy_unique_id("SN1", "Roof", "Daily On-grid Energy")
    legacy = registry.async_get_or_create(
        domain="sensor", platform=DOMAIN, unique_id=old, config_entry=entry
    )
    async_migrate_entity_ids(
        hass,
        entry.entry_id,
        "Roof",
        {"SN1"},
        {"SN1": "station-9"},
    )
    assert registry.async_get_entity_id("sensor", DOMAIN, "station-9_grid_export_today") == (
        legacy.entity_id
    )


async def test_string_label_migrates(hass: HomeAssistant) -> None:
    """Upstream PV string sensors use the DC Voltage PV label."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    old = legacy_unique_id("SN1", "Solis", "DC Voltage PV1")
    registry.async_get_or_create(
        domain="sensor", platform=DOMAIN, unique_id=old, config_entry=entry
    )
    async_migrate_entity_ids(hass, entry.entry_id, "Solis", {"SN1"})
    assert registry.async_get_entity_id("sensor", DOMAIN, "SN1_string_1_voltage") is not None


async def test_device_migration_skips_when_new_id_exists(hass: HomeAssistant) -> None:
    """An inverter that already has the new identifier is not rewritten."""
    entry = _entry(hass)
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "inverter:SN9")},
        name="current",
    )
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "SN9_SolisCloud")},
        name="legacy",
    )
    async_migrate_devices(hass, entry.entry_id, {"SN9"})
    assert (
        devices.async_get_device_by_identifier((DOMAIN, "SN9_SolisCloud"), entry.entry_id) is None
    )
