"""Leaving the stick for SolisCloud, and swapping one stick for another.

Local only → local with SolisCloud backup → SolisCloud only, and a web-page
stick replaced by a Modbus one. Entity ids, and so history, stay each time.
"""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .flow_helpers import logger_form, pick, start_user
from custom_components.solis.const import DOMAIN
from mock_soliscloud import catalog
from mock_soliscloud.catalog import KEY_ID, NORTH, SECRET
from mock_soliscloud.stick import (
    SERIAL,
    StickEmulator,
    StickState,
    captured_registers,
    encode_serial,
)
from mock_soliscloud.web_stick import WebStickEmulator, WebStickState

# A 16-character serial, as the stick stores it, shared by cloud and sticks.
NORTH_SERIAL = "1011A021B1500010"
KEYS = ("ac_power", "energy_total", "temperature")


@pytest.fixture(autouse=True)
def _sixteen_char_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "NORTH_SERIAL", NORTH_SERIAL)


def _registers() -> dict[int, int]:
    words = captured_registers()
    for offset, value in enumerate(encode_serial(NORTH_SERIAL)):
        words[3060 + offset] = value
    return words


async def _reconfigure(hass: HomeAssistant, entry: ConfigEntry, step: str) -> dict[str, Any]:
    menu = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )
    return await pick(hass, menu["flow_id"], step)


async def _submit(hass: HomeAssistant, form: dict[str, Any], data: dict[str, Any]) -> None:
    done = await hass.config_entries.flow.async_configure(form["flow_id"], data)
    assert done["type"] is FlowResultType.ABORT, done
    assert done["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()


def _entity_ids(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, str]:
    registry = er.async_get(hass)
    found = {}
    for key in KEYS:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{NORTH_SERIAL}_{key}")
        assert entity_id is not None, key
        row = registry.async_get(entity_id)
        assert row is not None and row.config_entry_id == entry.entry_id
        state = hass.states.get(entity_id)
        assert state is not None and state.state not in ("unknown", "unavailable"), key
        found[key] = entity_id
    return found


def _identifiers(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {item[1] for device in devices for item in device.identifiers}


async def test_local_entry_moves_to_both_then_cloud_only(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A stick-only entry adds SolisCloud, then drops the stick; the entities stay."""
    _state, url = solis_server
    emulator = StickEmulator(StickState(tcp_port=8899, registers=_registers()))
    await emulator.start()
    try:
        created = await start_user(hass, "local")
        created = await hass.config_entries.flow.async_configure(
            created["flow_id"],
            logger_form(logger_kind="solarman", logger_host="127.0.0.1", logger_serial=str(SERIAL)),
        )
        entry = created["result"]
        await hass.async_block_till_done()
        assert entry.options["scan_interval"] == 60
        local = _entity_ids(hass, entry)

        form = await _reconfigure(hass, entry, "reconfigure_cloud")
        stations = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
        )
        assert stations["step_id"] == "stations"
        await _submit(hass, stations, {"station_ids": [NORTH]})
        assert entry.data["source"] == "both"
        assert entry.options["scan_interval"] == 60
        assert _entity_ids(hass, entry) == local

        await _submit(hass, await _reconfigure(hass, entry, "connection"), {"source": "cloud"})
        assert entry.data["source"] == "cloud"
        assert entry.runtime_data.data.source == "cloud"
        # The stick's one-minute default became the SolisCloud default.
        assert entry.options["scan_interval"] == 300
        assert _entity_ids(hass, entry) == local
        assert f"station:{NORTH}" in _identifiers(hass, entry)
        # The stick settings stay, so the entry can switch back.
        assert entry.data["logger_host"] == "127.0.0.1"

        await _submit(hass, await _reconfigure(hass, entry, "connection"), {"source": "both"})
        assert entry.options["scan_interval"] == 60
        assert _entity_ids(hass, entry) == local
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await emulator.close()


async def test_a_chosen_interval_stays_when_switching_to_cloud(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Only the default follows the connection."""
    _state, url = solis_server
    emulator = StickEmulator(StickState(tcp_port=8899, registers=_registers()))
    await emulator.start()
    try:
        created = await start_user(hass, "both")
        form = await hass.config_entries.flow.async_configure(
            created["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
        )
        form = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"station_ids": [NORTH]}
        )
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            logger_form(logger_kind="solarman", logger_host="127.0.0.1", logger_serial=str(SERIAL)),
        )
        entry = created["result"]
        await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "scan_interval": 90}
        )
        await _submit(hass, await _reconfigure(hass, entry, "connection"), {"source": "cloud"})
        assert entry.options["scan_interval"] == 90
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await emulator.close()


async def test_web_page_stick_swapped_for_a_modbus_stick(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """Reconfigure → Local stick moves the entry to the S2; entities and history stay."""
    web = WebStickEmulator(WebStickState(serial=NORTH_SERIAL))
    await web.start()
    monkeypatch.setattr("custom_components.solis.web_transport.WEB_PORT", web.port)
    created = await start_user(hass, "local")
    created = await hass.config_entries.flow.async_configure(
        created["flow_id"], logger_form(logger_kind="auto", logger_host="127.0.0.1")
    )
    if created.get("step_id") == "logger_password":
        created = await hass.config_entries.flow.async_configure(
            created["flow_id"], {"logger_password": "123456789"}
        )
    entry = created["result"]
    await hass.async_block_till_done()
    assert entry.data["logger_kind"] == "solis_web"
    assert entry.data["logger_password"]
    on_web = _entity_ids(hass, entry)
    web_loggers = {item for item in _identifiers(hass, entry) if item.startswith("collector:")}
    assert web_loggers
    # The web stick is unplugged; the S2-WL-ST takes its place.
    await hass.config_entries.async_unload(entry.entry_id)
    await web.close()

    s2 = StickEmulator(StickState(kind="s2", tcp_port=0, udp_port=0, registers=_registers()))
    await s2.start()
    assert s2._tcp is not None
    port = s2._tcp.sockets[0].getsockname()[1]
    try:
        form = await _reconfigure(hass, entry, "logger")
        assert form["step_id"] == "logger"
        await _submit(
            hass,
            form,
            logger_form(
                logger_kind="s2_wl_st",
                logger_host="127.0.0.1",
                logger_port=str(port),
                inverter_serial=NORTH_SERIAL,
            ),
        )
        assert entry.data["logger_kind"] == "s2_wl_st"
        assert entry.unique_id == f"s2:{NORTH_SERIAL}"
        assert "logger_password" not in entry.data
        assert entry.runtime_data.data.source == "local"
        assert _entity_ids(hass, entry) == on_web
        # The web stick's logger device went with it.
        assert not web_loggers & _identifiers(hass, entry)
        assert f"inverter:{NORTH_SERIAL}" in _identifiers(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await s2.close()


async def test_both_entry_shows_the_plant_while_the_stick_is_dark(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A dark stick on a producing inverter publishes SolisCloud, plant included.

    The next local read keeps the plant: only loggers are pruned on a local read.
    """
    _state, url = solis_server
    emulator = StickEmulator(StickState(tcp_port=8899, registers=_registers()))
    await emulator.start()
    try:
        created = await start_user(hass, "both")
        form = await hass.config_entries.flow.async_configure(
            created["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
        )
        form = await hass.config_entries.flow.async_configure(
            form["flow_id"], {"station_ids": [NORTH]}
        )
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            logger_form(logger_kind="solarman", logger_host="127.0.0.1", logger_serial=str(SERIAL)),
        )
        entry = created["result"]
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        assert coordinator.data.source == "local"
        registry = er.async_get(hass)
        plant_unique = f"{NORTH}_grid_import_today"

        await emulator.apply({"night": True})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data.source == "degraded"
        plant_entity = registry.async_get_entity_id("sensor", DOMAIN, plant_unique)
        assert plant_entity is not None
        state = hass.states.get(plant_entity)
        assert state is not None and state.state == "1.5"
        assert f"station:{NORTH}" in _identifiers(hass, entry)
        # The stick stays the only logger: SolisCloud's would be pruned again.
        assert {i for i in _identifiers(hass, entry) if i.startswith("collector:")} == {
            f"collector:{SERIAL}"
        }

        await emulator.apply({"night": False})
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.data.source == "local"
        assert registry.async_get_entity_id("sensor", DOMAIN, plant_unique) == plant_entity
        assert f"station:{NORTH}" in _identifiers(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await emulator.close()
