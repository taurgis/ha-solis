"""Config-flow normalising, empty selection, and reauth of a different key."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import entry_data, entry_options
from .flow_helpers import pick, start_user
from custom_components.solis.config_flow import _normalize_url, migrate_entry_data
from custom_components.solis.const import DOMAIN
from mock_soliscloud.catalog import KEY_ID, SECRET


def test_url_normalisation_and_legacy_migration() -> None:
    """A bare host gets a scheme, and a 4.x entry drops the portal password."""
    assert _normalize_url("www.soliscloud.com:13333/") == "https://www.soliscloud.com:13333"
    assert _normalize_url("http://example.test") == "https://example.test"
    assert _normalize_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert _normalize_url("http://mock:8765") == "http://mock:8765"
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


async def test_options_reject_an_unknown_timezone(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A timezone that is not an IANA name, or plant, is rejected."""
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
        {
            "scan_interval": 240,
            "advanced": {"request_timeout": 55, "endpoint_interval": 3},
            "time_zone": "Not/AZone",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["time_zone"] == "invalid_timezone"
    # A path-like value is not a zone either, and must not reach the file system.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_interval": 240, "time_zone": "../etc/passwd"}
    )
    assert result["errors"]["time_zone"] == "invalid_timezone"


async def test_empty_station_selection_is_rejected(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """The plant step stays open when nothing is selected."""
    _state, url = solis_server
    result = await start_user(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"station_ids": []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "select_station"


async def test_reconfigure_aborts_when_the_cloud_is_down(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """The plant list cannot load when every call returns HTTP 502."""
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
    result = await entry.start_reconfigure_flow(hass)
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        result = await pick(hass, result["flow_id"], "stations")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_reauth_rejects_a_key_of_another_account(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A key that cannot see the entry's plants belongs to another account."""
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
    with patch(
        "custom_components.solis.config_flow._fetch_stations",
        new=AsyncMock(return_value={"999": "Someone else"}),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"key_id": "other-key", "key_secret": SECRET},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["key_id"] == KEY_ID


async def test_reauth_accepts_a_rotated_key(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A new key on the same account replaces the old one and the unique id."""
    state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    state.key_id = "rotated-key"
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"key_id": "rotated-key", "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["key_id"] == "rotated-key"
    assert entry.unique_id == "rotated-key"


async def test_reauth_rotated_key_already_used_elsewhere(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A rotated key that another cloud entry already reads is refused."""
    state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        unique_id=KEY_ID,
        version=2,
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), "key_id": "rotated-key"},
        options=entry_options(),
        unique_id="rotated-key",
        version=2,
    )
    other.add_to_hass(hass)
    state.key_id = "rotated-key"
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"key_id": "rotated-key", "key_secret": SECRET},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
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
        {"scan_interval": 240, "advanced": {"request_timeout": 55, "endpoint_interval": 3}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "scan_interval": 240,
        "request_timeout": 55,
        "endpoint_interval": 3.0,
        "time_zone": "",
    }


async def test_connection_switch_refuses_a_second_reader_of_the_account(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A backup entry cannot become SolisCloud-only while another entry reads the key."""
    _state, url = solis_server
    stick = {
        "source": "both",
        "logger_kind": "solarman",
        "logger_host": "192.0.2.10",
        "logger_serial": "1902137671",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), **stick},
        options=entry_options(),
        unique_id="1902137671",
        version=3,
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), **stick, "logger_serial": "1902137672"},
        options=entry_options(),
        unique_id="1902137672",
        version=3,
    )
    other.add_to_hass(hass)
    menu = await entry.start_reconfigure_flow(hass)
    shown = await pick(hass, menu["flow_id"], "connection")
    result = await hass.config_entries.flow.async_configure(shown["flow_id"], {"source": "cloud"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["source"] == "both"


async def test_backup_entry_points_at_the_existing_cloud_entry(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Adding a stick with backup on a SolisCloud-only account says where to add it."""
    _state, url = solis_server
    MockConfigEntry(
        domain=DOMAIN, data=entry_data(url), options=entry_options(), unique_id=KEY_ID, version=3
    ).add_to_hass(hass)
    result = await start_user(hass, "both")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cloud_entry_exists"
