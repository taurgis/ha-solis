"""A migrated solis-sensor (cloud) entry moves to cloud + local, then local only.

The path is Reconfigure → Logger (the cloud entry gains a stick and becomes
``both``), then Reconfigure → Connection → local. The legacy entity ids, the
devices they sit on, and so their recorder history, must survive each step.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .flow_helpers import logger_form, pick
from custom_components.solis.const import DOMAIN
from custom_components.solis.migration import legacy_unique_id
from mock_soliscloud import catalog
from mock_soliscloud.catalog import KEY_ID, NORTH, SECRET
from mock_soliscloud.stick import (
    SERIAL,
    StickEmulator,
    StickState,
    captured_registers,
    encode_serial,
)

# Real Solis serials are 16 characters, the length the stick stores. The
# catalog's 15-character serial would decode with a padding nibble.
NORTH_SERIAL = "1011A021B1500010"

INVERTER_LABELS = {
    "ac_power": "AC Output Total Power",
    "energy_total": "Energy Total",
    "energy_today": "Energy Today",
    "temperature": "Temperature",
}
PLANT_LABEL = ("grid_import_today", "Daily Grid Energy Purchased")


@pytest.fixture(autouse=True)
def _sixteen_char_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "NORTH_SERIAL", NORTH_SERIAL)


def _north_registers() -> dict[int, int]:
    """The stick reports the serial SolisCloud knows for the North Roof inverter."""
    words = captured_registers()
    for offset, value in enumerate(encode_serial(NORTH_SERIAL)):
        words[3060 + offset] = value
    return words


def _legacy_entry(url: str, refresh: int = 300) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "portal_domain": url,
            "portal_username": "owner",
            "portal_key_id": KEY_ID,
            "portal_secret": SECRET,
            "portal_plant_id": NORTH,
            "name": "Solis",
            "refresh_ok": refresh,
        },
        version=1,
    )


def _legacy_rows(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, dict[str, str]]:
    """Registry rows solis-sensor 4.x left behind, on its ``{serial}_SolisCloud`` device."""
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{NORTH_SERIAL}_SolisCloud")},
        name="Solis",
    )
    entity_ids: dict[str, str] = {}
    for key, label in (*INVERTER_LABELS.items(), PLANT_LABEL):
        row = registry.async_get_or_create(
            domain="sensor",
            platform=DOMAIN,
            unique_id=legacy_unique_id(NORTH_SERIAL, "Solis", label),
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"solis_{key}",
        )
        entity_ids[key] = row.entity_id
    return device.id, entity_ids


async def _add_stick(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    menu = await entry.start_reconfigure_flow(hass)
    assert menu["menu_options"] == ["reconfigure_cloud", "stations", "logger"]
    form = await pick(hass, menu["flow_id"], "logger")
    assert form["step_id"] == "logger"
    done = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        logger_form(logger_kind="solarman", logger_host="127.0.0.1", logger_serial=str(SERIAL)),
    )
    await hass.async_block_till_done()
    return dict(done)


async def _switch(hass: HomeAssistant, entry: MockConfigEntry, source: str) -> None:
    menu = await entry.start_reconfigure_flow(hass)
    assert "connection" in menu["menu_options"]
    form = await pick(hass, menu["flow_id"], "connection")
    done = await hass.config_entries.flow.async_configure(form["flow_id"], {"source": source})
    assert done["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["source"] == source


async def test_migrated_cloud_entry_moves_to_both_then_local(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Legacy ids and devices survive cloud → cloud + local → local → cloud."""
    _state, url = solis_server
    entry = _legacy_entry(url)
    entry.add_to_hass(hass)
    inverter_device, entity_ids = _legacy_rows(hass, entry)
    registry = er.async_get(hass)
    devices = dr.async_get(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.data["source"] == "cloud"
    plant_entity = entity_ids.pop(PLANT_LABEL[0])
    plant_row = registry.async_get(plant_entity)
    assert plant_row is not None
    assert plant_row.unique_id == f"{NORTH}_{PLANT_LABEL[0]}"
    plant_device = plant_row.device_id
    assert plant_device is not None and plant_device != inverter_device

    def check(stage: str) -> dict[str, str]:
        values = {}
        for key, entity_id in entity_ids.items():
            row = registry.async_get(entity_id)
            assert row is not None, (stage, key)
            assert row.unique_id == f"{NORTH_SERIAL}_{key}", (stage, row.unique_id)
            assert row.device_id == inverter_device, (stage, key)
            state = hass.states.get(entity_id)
            assert state is not None and state.state not in ("unknown", "unavailable"), (
                stage,
                key,
            )
            values[key] = state.state
        # The plant counter only SolisCloud reports keeps its row and device.
        row = registry.async_get(plant_entity)
        assert row is not None, stage
        assert row.device_id == plant_device, stage
        assert devices.async_get(plant_device) is not None, stage
        return values

    cloud = check("cloud")
    assert cloud["energy_total"] == "12500.0"

    emulator = StickEmulator(StickState(tcp_port=8899, registers=_north_registers()))
    await emulator.start()
    try:
        done = await _add_stick(hass, entry)
        assert done["type"] is FlowResultType.ABORT
        assert done["reason"] == "reconfigure_successful"
        assert entry.data["source"] == "both"
        assert entry.data["inverter_serial"] == NORTH_SERIAL
        assert entry.unique_id == str(SERIAL)
        # The cloud default of five minutes became the local default.
        assert entry.options["scan_interval"] == 60
        assert entry.runtime_data.data.source == "local"
        both = check("both")
        # The stick's lifetime counter now drives the same entity.
        assert both["energy_total"] == "21970.0"

        await _switch(hass, entry, "local")
        assert entry.runtime_data.data.source == "local"
        assert check("local")["energy_total"] == "21970.0"

        # And back: SolisCloud alone still owns the same rows.
        await _switch(hass, entry, "cloud")
        assert entry.runtime_data.data.source == "cloud"
        assert entry.options["scan_interval"] == 300
        check("cloud again")
        plant_state = hass.states.get(plant_entity)
        assert plant_state is not None
        assert plant_state.state not in ("unknown", "unavailable")
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await emulator.close()


async def test_a_chosen_poll_interval_survives_adding_a_stick(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Only the cloud default moves to the local default; a custom interval stays."""
    _state, url = solis_server
    entry = _legacy_entry(url, refresh=120)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.options["scan_interval"] == 120
    emulator = StickEmulator(StickState(tcp_port=8899, registers=_north_registers()))
    await emulator.start()
    try:
        done = await _add_stick(hass, entry)
        assert done["reason"] == "reconfigure_successful"
        assert entry.data["source"] == "both"
        assert entry.options["scan_interval"] == 120
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await emulator.close()


async def test_a_cloud_read_still_prunes_a_dropped_plant(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Keeping cloud devices on a local read does not stop a cloud read from pruning."""
    _state, url = solis_server
    entry = _legacy_entry(url)
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    gone = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "station:1900000000000009999")},
        name="Sold plant",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert devices.async_get(gone.id) is None
    await hass.config_entries.async_unload(entry.entry_id)
