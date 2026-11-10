"""Branches that the happy-path tests do not reach."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from .conftest import mock_entry
from custom_components.solis.diagnostics import async_get_device_diagnostics
from custom_components.solis.parser import (
    extract_records,
    local_date,
    page_count,
    parse_offset_hours,
)
from custom_components.solis.repairs import async_create_fix_flow
from custom_components.solis.signing import signature_matches


def test_signature_rejects_missing_headers() -> None:
    """An unsigned request does not match."""
    assert not signature_matches(
        secret="secret",
        key_id="key",
        path="/v1/api/inverterList",
        body=b"{}",
        headers={},
    )


def test_record_shapes_and_offsets() -> None:
    """Both page layouts and a UTC offset string parse."""
    assert extract_records({"data": [{"sn": "A"}]}) == [{"sn": "A"}]
    assert extract_records({"data": {"records": [{"sn": "B"}]}}) == [{"sn": "B"}]
    assert extract_records({"data": "nope"}) == []
    assert page_count({"data": {"pages": "2"}}) == 2
    assert page_count({"data": []}) is None
    assert parse_offset_hours("UTC") == 0
    assert parse_offset_hours("not-a-zone") is None
    from datetime import UTC, datetime

    assert local_date(None, datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02"


async def test_repairs_flow(hass: HomeAssistant) -> None:
    """Auth repairs start reauth and other repairs just confirm."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.solis.const import DOMAIN

    entry = MockConfigEntry(domain=DOMAIN, data={}, version=2)
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(
        hass,
        f"invalid_auth_{entry.entry_id}",
        {"entry_id": entry.entry_id},
    )
    flow.hass = hass
    flow.data = {"entry_id": entry.entry_id}
    result = await flow.async_step_init()
    assert result["type"] == "create_entry"
    other = await async_create_fix_flow(hass, "legacy_control_x", None)
    assert type(other).__name__ == "ConfirmRepairFlow"


async def test_clock_abnormal_and_empty_list(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Clock skew, abnormal data and a repeated empty list are handled."""
    state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state.mode = "clock"
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.north_roof_s6_gr1p5k_ac_power").state == "unavailable"
    state.mode = "abnormal"
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        await entry.runtime_data.async_refresh()
    state.mode = "ok"
    state.empty_inverters = True
    with patch("custom_components.solis.api.asyncio.sleep", new=AsyncMock()):
        await entry.runtime_data.async_refresh()
        await entry.runtime_data.async_refresh()
    assert hass.states.get("sensor.barn_hybrid_ac_power") is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_device_diagnostics(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Device diagnostics only include that device's rows."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.helpers import device_registry as dr

    from custom_components.solis.const import DOMAIN

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "inverter:1102C021B150002"),
        entry.entry_id,
    )
    assert device is not None
    payload = await async_get_device_diagnostics(hass, entry, device)
    serials = [row["serial"] for row in payload["snapshot"]["inverters"]]
    assert serials == ["1102C021B150002"]


async def test_config_flow_error_modes(hass: HomeAssistant, solis_server: tuple[Any, str]) -> None:
    """Timeouts and server errors surface as config-flow errors."""
    state, url = solis_server
    from custom_components.solis.const import DOMAIN
    from mock_soliscloud.catalog import KEY_ID, SECRET

    state.mode = "clock"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["errors"]["base"] == "clock_skew"
    state.mode = "server_error"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
    )
    assert result["errors"]["base"] == "cannot_connect"
