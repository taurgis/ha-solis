"""Diagnostics redaction for a single device and for legacy secret keys."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .conftest import mock_entry
from custom_components.solis.const import DOMAIN
from custom_components.solis.diagnostics import TO_REDACT, async_get_device_diagnostics


def test_redaction_list_covers_legacy_portal_secrets() -> None:
    """Keys stored by solis-sensor 4.x are on the redact list."""
    redacted = async_redact_data(
        {
            "portal_password": "pw",
            "portal_username": "owner",
            "portal_secret": "sekret",
            "latitude": 1.0,
            "address": "secret street",
        },
        TO_REDACT,
    )
    assert redacted["portal_password"] == "**REDACTED**"
    assert redacted["portal_username"] == "**REDACTED**"
    assert redacted["latitude"] == "**REDACTED**"
    assert redacted["address"] == "**REDACTED**"


async def test_station_diagnostics_omit_other_plants(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A plant device diagnostic only includes that plant."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "station:1900000000000001001"),
        entry.entry_id,
    )
    assert device is not None
    payload = await async_get_device_diagnostics(hass, entry, device)
    assert payload["snapshot"]["stations"][0]["station_id"] == "1900000000000001001"
    assert payload["snapshot"]["inverters"] == []
    assert "latitude" not in payload["snapshot"]["stations"][0] or (
        payload["snapshot"]["stations"][0]["latitude"] == "**REDACTED**"
    )
