"""Config-flow normalising, empty selection, and reauth of a different key."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import entry_data, entry_options
from custom_components.solis.config_flow import _normalize_url, migrate_entry_data
from custom_components.solis.const import DOMAIN
from mock_soliscloud.catalog import KEY_ID, SECRET


def test_url_normalisation_and_legacy_migration() -> None:
    """A bare host gets a scheme, and a 4.x entry drops the portal password."""
    assert _normalize_url("www.soliscloud.com:13333/") == "https://www.soliscloud.com:13333"
    assert _normalize_url("http://example.test") == "http://example.test"
    data, options = migrate_entry_data(
        {
            "portal_domain": "v3.soliscloud.com:13333",
            "portal_key_id": "key",
            "portal_secret": "secret",
            "portal_plant_id": "",
            "name": "Roof",
        },
        {},
    )
    assert data["api_url"] == "https://v3.soliscloud.com:13333"
    assert data["station_ids"] == []
    assert data["legacy_name"] == "Roof"
    assert options["scan_interval"] == 300
    assert options["endpoint_interval"] == 2.0


async def test_empty_station_selection_is_rejected(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """The plant step stays open when nothing is selected."""
    _state, url = solis_server
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"station_ids": []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "no_stations"


async def test_reconfigure_aborts_when_the_cloud_is_down(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Reconfigure cannot load plants when every call returns HTTP 502."""
    state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    state.mode = "server_error"
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_reauth_rejects_a_different_key(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A key that authenticates as a different account is not applied."""
    state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    state.key_id = "other-key"
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"key_id": "other-key", "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["key_id"] == KEY_ID


async def test_options_round_trip_keeps_every_knob(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Options save the interval, the timeout and the per-endpoint gap together."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"scan_interval": 240, "request_timeout": 55, "endpoint_interval": 3},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "scan_interval": 240,
        "request_timeout": 55,
        "endpoint_interval": 3.0,
    }
