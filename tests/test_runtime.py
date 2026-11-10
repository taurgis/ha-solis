"""Coordinator, entities, diagnostics and unique-id migration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import mock_entry
from custom_components.solis.const import DOMAIN
from custom_components.solis.migration import legacy_unique_id
from mock_soliscloud.catalog import GRID_SERIAL, HYBRID_SERIAL, NORTH_SERIAL, STALE_SERIAL


def _state(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state.state


def _number(hass: HomeAssistant, entity_id: str) -> float:
    return float(_state(hass, entity_id))


async def _refresh(hass: HomeAssistant, entry: Any) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _setup(hass: HomeAssistant, url: str) -> Any:
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_match_mock(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Online, offline, stale and hybrid values follow the mock fixture."""
    _state_obj, url = solis_server
    await _setup(hass, url)
    assert _number(hass, "sensor.north_roof_s6_gr1p5k_ac_power") == 1250
    assert _number(hass, "sensor.north_roof_s6_gr1p5k_energy_total") == 12500
    assert _number(hass, "sensor.north_roof_s6_gr1p5k_energy_today") == 4.321
    assert _number(hass, "sensor.barn_hybrid_battery_power") == -1200
    assert _number(hass, "sensor.barn_hybrid_battery") == 72
    assert _number(hass, "sensor.barn_grid_ac_power") == 0
    assert _number(hass, "sensor.barn_grid_energy_total") == 3200
    assert _state(hass, "sensor.south_shed_ac_power") == "unavailable"
    assert _state(hass, "sensor.south_shed_status") == "stale"
    assert _state(hass, "sensor.north_roof_load_today") == "3.2"
    assert _state(hass, "binary_sensor.barn_hybrid_alarm") == "on"
    assert _state(hass, "binary_sensor.north_roof_s6_gr1p5k_alarm") == "off"
    assert hass.states.get("sensor.barn_hybrid_phase_2_voltage") is not None
    assert hass.states.get("sensor.north_roof_s6_gr1p5k_phase_2_voltage") is None


async def test_failure_then_recovery(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """A 502 makes entities unavailable and a later success restores them."""
    state, url = solis_server
    entry = await _setup(hass, url)
    entity = "sensor.north_roof_s6_gr1p5k_ac_power"
    state.mode = "server_error"
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        await _refresh(hass, entry)
    assert _state(hass, entity) == "unavailable"
    state.mode = "ok"
    await _refresh(hass, entry)
    assert _number(hass, entity) == 1250


async def test_inverter_list_failure_keeps_devices(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A failed inverterList does not delete inverters that were already known."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.fail_inverter_list = True
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        await _refresh(hass, entry)
    assert hass.states.get("sensor.barn_hybrid_ac_power") is not None
    assert _number(hass, "sensor.barn_hybrid_ac_power") == 3500


async def test_diagnostics_redact(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Secrets and coordinates are removed from diagnostics."""
    _state_obj, url = solis_server
    entry = await _setup(hass, url)
    from custom_components.solis.diagnostics import async_get_config_entry_diagnostics

    payload = await async_get_config_entry_diagnostics(hass, entry)
    assert payload["entry"]["data"]["key_secret"] == "**REDACTED**"
    assert payload["snapshot"]["stations"][0]["latitude"] == "**REDACTED**"
    assert payload["snapshot"]["inverters"]


async def test_unique_id_and_device_migration(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Upstream name-based ids and device identifiers are rewritten."""
    _state_obj, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = legacy_unique_id(NORTH_SERIAL, "Solis", "AC Output Total Power")
    registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=old,
        config_entry=entry,
        original_name="Solis AC Output Total Power",
    )
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{HYBRID_SERIAL}_SolisCloud")},
        name="old hybrid",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    migrated = registry.async_get_entity_id("sensor", DOMAIN, f"{NORTH_SERIAL}_ac_power")
    assert migrated is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, old) is None
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, f"inverter:{HYBRID_SERIAL}"),
            entry.entry_id,
        )
        is not None
    )
    assert (
        devices.async_get_device_by_identifier(
            (DOMAIN, f"{HYBRID_SERIAL}_SolisCloud"), entry.entry_id
        )
        is None
    )


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A rejected key marks the entry for reauth and opens a repair."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.mode = "auth_error"
    await _refresh(hass, entry)
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"].get("source") == "reauth" for flow in flows)
    from homeassistant.helpers import issue_registry as ir

    issues = ir.async_get(hass).issues
    assert any(issue_id.startswith("invalid_auth") for (_domain, issue_id) in issues)


def test_serials_are_stable() -> None:
    """The fixture serials stay referenced by the migration test."""
    assert NORTH_SERIAL and HYBRID_SERIAL and GRID_SERIAL and STALE_SERIAL
