"""Config flow, options, reauth and entry migration."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import entry_data, entry_options
from .flow_helpers import pick, start_user
from custom_components.solis.const import DOMAIN
from mock_soliscloud.catalog import KEY_ID, SECRET


async def test_user_flow_creates_entry(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Selecting both plants creates one entry keyed by the API id."""
    _state, url = solis_server
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["cloud", "local", "both"]
    result = await pick(hass, result["flow_id"], "cloud")
    assert result["step_id"] == "cloud"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "stations"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"station_ids": ["1900000000000001001"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "North Roof"
    assert result["data"]["station_ids"] == ["1900000000000001001"]
    assert result["options"]["request_timeout"] == 30


async def test_invalid_auth(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """A bad secret is reported as invalid auth, not a generic connection error."""
    _state, url = solis_server
    result = await start_user(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": "nope"},
    )
    assert result["errors"]["base"] == "invalid_auth"


async def test_duplicate_key(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """The same API key cannot be added twice."""
    _state, url = solis_server
    existing = MockConfigEntry(domain=DOMAIN, data=entry_data(url), unique_id=KEY_ID, version=2)
    existing.add_to_hass(hass)
    result = await start_user(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_key_of_a_local_with_cloud_entry_is_not_added_again(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A stick entry keyed by its logger id still owns its SolisCloud key."""
    _state, url = solis_server
    stick = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), "source": "both", "logger_host": "10.0.0.5"},
        unique_id="1902137671",
        version=3,
    )
    stick.add_to_hass(hass)
    result = await start_user(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # A second stick on the same account is a separate inverter and stays allowed.
    second = await start_user(hass, "both")
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert second["step_id"] == "stations"


async def test_reauth_and_reconfigure(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Reauth replaces the secret and reconfigure changes the plants only."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["reconfigure_cloud", "stations", "logger"]
    result = await pick(hass, result["flow_id"], "stations")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"station_ids": ["1900000000000001002"]}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["station_ids"] == ["1900000000000001002"]
    # Polling lives in options only. Reconfigure leaves it alone.
    assert entry.options == entry_options()


async def test_options_flow_keeps_timeout(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Saving options must not drop the request timeout."""
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
        {"scan_interval": 180, "advanced": {"request_timeout": 40, "endpoint_interval": 2}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["request_timeout"] == 40
    assert entry.options["scan_interval"] == 180


async def test_cloud_options_reject_a_30_second_poll(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A cloud entry cannot be polled faster than every 60 seconds."""
    from homeassistant.data_entry_flow import InvalidData

    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    started = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            started["flow_id"],
            {"scan_interval": 30, "advanced": {"request_timeout": 40, "endpoint_interval": 2}},
        )


async def test_local_options_allow_a_30_second_poll(hass: HomeAssistant) -> None:
    """A local logger may poll every 30 seconds."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "local", "logger_host": "127.0.0.1", "logger_serial": "1"},
        options={
            "scan_interval": 60,
            "request_timeout": 30,
            "endpoint_interval": 2,
            "time_zone": "",
        },
        version=3,
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Saving options reloads the entry; the stick itself is not under test here.
    with patch("custom_components.solis.async_setup_entry", return_value=True) as setup:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"scan_interval": 30, "time_zone": "auto"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["scan_interval"] == 30
    assert entry.options["time_zone"] == ""
    assert setup.call_count == 1


async def test_migrate_legacy_entry(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """A solis-sensor 4.x entry becomes version 2 and drops the control password."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "portal_domain": url,
            "portal_username": "owner@example.com",
            "portal_password": "secret-password",
            "portal_key_id": KEY_ID,
            "portal_secret": SECRET,
            "portal_plant_id": "1900000000000001001",
            "portal_control_api": True,
            "name": "Roof",
            "refresh_ok": 180,
            "request_timeout": 25,
        },
        version=1,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.data["source"] == "cloud"
    assert entry.data["key_id"] == KEY_ID
    assert entry.data["legacy_name"] == "Roof"
    assert "portal_password" not in entry.data
    assert entry.options["request_timeout"] == 25
    assert entry.options["scan_interval"] == 180
    assert entry.state is not None
