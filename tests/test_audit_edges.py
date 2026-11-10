"""Setup, reload, removal and config-flow branches found in the audit."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er

from .conftest import mock_entry
from custom_components.solis.const import DOMAIN
from custom_components.solis.entity import _product_model
from custom_components.solis.errors import SolisTimeoutError
from mock_soliscloud.catalog import KEY_ID, NORTH_SERIAL, SECRET


def test_product_model_labels() -> None:
    """Unknown product codes still get a readable model."""
    assert _product_model("2") == "Hybrid inverter"
    assert _product_model("1") == "Grid inverter"
    assert _product_model(None) == "Solis inverter"
    assert _product_model("9") == "Solis inverter"


async def test_setup_rejects_a_bad_key(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Auth failure during setup becomes ConfigEntryAuthFailed."""
    state, url = solis_server
    state.mode = "auth_error"
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_retries_when_the_cloud_times_out(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A timeout on the first refresh leaves the entry to be retried."""
    state, url = solis_server
    state.mode = "timeout"
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_reload_the_entry(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Saving options reloads the coordinator with the new interval."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 240,
            "request_timeout": 20,
            "endpoint_interval": 1,
            "time_zone": "Europe/London",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options["time_zone"] == "Europe/London"
    assert entry.runtime_data.update_interval.total_seconds() == 240


async def test_removed_inverter_drops_its_entities(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """An inverter that disappears from a complete poll is removed."""
    state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.north_roof_s6_gr1p5k_ac_power") is not None
    state.skip_serials = {NORTH_SERIAL}
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.north_roof_s6_gr1p5k_ac_power") is None
    assert hass.states.get("binary_sensor.north_roof_s6_gr1p5k_alarm") is None
    assert hass.states.get("sensor.barn_hybrid_ac_power") is not None


async def test_restored_total_survives_a_zero_on_startup(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A restart mid-day does not accept a zero lifetime total."""
    state, url = solis_server
    state.energy_glitch = "zero"
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    "sensor.north_roof_s6_gr1p5k_energy_total",
                    "12500",
                    {"unit_of_measurement": "kWh"},
                ),
                SensorExtraStoredData(12500.0, "kWh").as_dict(),
            )
        ],
    )
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.states.get("sensor.north_roof_s6_gr1p5k_energy_total")
    assert restored is not None
    assert float(restored.state) == 12500


async def test_reconfigure_timezone_and_empty_selection(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Reconfigure rejects a bad timezone and an empty plant selection."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "api_url": url,
            "station_ids": ["1900000000000001001"],
            "scan_interval": 120,
            "request_timeout": 30,
            "endpoint_interval": 2,
            "time_zone": "Not/AZone",
        },
    )
    assert result["errors"]["time_zone"] == "invalid_timezone"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "api_url": url,
            "station_ids": [],
            "scan_interval": 120,
            "request_timeout": 30,
            "endpoint_interval": 2,
            "time_zone": "plant",
        },
    )
    assert result["errors"]["base"] == "no_stations"


async def test_two_plants_use_the_solis_title(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """More than one plant is titled Solis."""
    _state, url = solis_server
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"station_ids": ["1900000000000001001", "1900000000000001002"]},
    )
    assert result["title"] == "Solis"


async def test_discovery_timeout_and_nameless_plant(hass: HomeAssistant) -> None:
    """A timeout, a plant with no id, and an empty list are form errors."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})

    with patch(
        "custom_components.solis.config_flow.SolisCloudClient.user_station_list",
        new=AsyncMock(side_effect=SolisTimeoutError("slow")),
    ):
        timed_out = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"api_url": "https://example.test", "key_id": "k", "key_secret": "s"},
        )
    assert timed_out["errors"]["base"] == "timeout"

    with patch(
        "custom_components.solis.config_flow.SolisCloudClient.user_station_list",
        new=AsyncMock(return_value=[{"stationName": "Nameless"}]),
    ):
        nameless = await hass.config_entries.flow.async_configure(
            timed_out["flow_id"],
            {"api_url": "https://example.test", "key_id": "k", "key_secret": "s"},
        )
    assert nameless["errors"]["base"] == "no_stations"


async def test_registry_entity_for_removed_inverter_is_gone(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Removing an inverter also drops its registry row."""
    state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state.skip_serials = {NORTH_SERIAL}
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{NORTH_SERIAL}_ac_power") is None
