"""Boot Home Assistant on a web-page stick: auto-detect, entities, reload, both mode.

Runs against the web-stick emulator, so it works on any network. To run the
same checks against a real stick on your LAN:

    SOLIS_WEB_STICK=192.168.0.112 python -m pytest tests/test_web_stick_setup.py -s

``SOLIS_WEB_PASSWORD`` overrides the factory password. A real stick is read
only (two GETs per poll), and the exact-value checks are skipped for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import os
from typing import Any, NamedTuple

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest_socket

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import entry_data, entry_options
from custom_components.solis.const import DOMAIN, SOURCE_BOTH
from custom_components.solis.diagnostics import async_get_config_entry_diagnostics
from mock_soliscloud.catalog import NORTH_SERIAL
from mock_soliscloud.web_stick import WebStickEmulator, WebStickState

_REAL = os.environ.get("SOLIS_WEB_STICK", "").strip()
_PASSWORD = os.environ.get("SOLIS_WEB_PASSWORD", "123456789")


class Target(NamedTuple):
    """Where the stick is. ``live`` is true for a real device."""

    host: str
    port: int
    live: bool


@pytest.fixture
async def target(monkeypatch: pytest.MonkeyPatch, socket_enabled: None) -> AsyncIterator[Target]:
    """The real stick when ``SOLIS_WEB_STICK`` is set, otherwise the emulator."""
    if _REAL:
        pytest_socket.socket_allow_hosts([_REAL, "127.0.0.1"], allow_unix_socket=True)
        yield Target(_REAL, 80, True)
        return
    emulator = WebStickEmulator(WebStickState())
    await emulator.start()
    monkeypatch.setattr("custom_components.solis.web_transport.WEB_PORT", emulator.port)
    try:
        yield Target("127.0.0.1", emulator.port, False)
    finally:
        await emulator.close()


def _states(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, str]:
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(item.entity_id)
        found[item.unique_id] = state.state if state else "missing"
    for key, value in sorted(found.items()):
        print(f"  {key:48} {value}")
    return found


def _no_repairs(hass: HomeAssistant) -> None:
    assert [issue_id for (_domain, issue_id) in ir.async_get(hass).issues] == []


async def test_auto_detect_setup_reload_and_diagnostics(
    hass: HomeAssistant, target: Target
) -> None:
    """Config flow auto-detects the stick, entities carry its values, reload keeps them."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"source": "local", "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    created = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"logger_kind": "auto", "logger_host": target.host, "logger_password": _PASSWORD},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY, created.get("errors")
    entry = created["result"]
    assert entry.data["logger_kind"] == "solis_web"
    assert entry.data["logger_detect"] == "auto_solis_web"
    assert str(entry.unique_id).startswith("web:")
    await hass.async_block_till_done()
    serial = entry.data["inverter_serial"]
    states = _states(hass, entry)
    assert states[f"{serial}_ac_power"] not in {"unknown", "unavailable", "missing"}
    assert states[f"{serial}_energy_total"] not in {"unknown", "unavailable", "missing"}
    if not target.live:
        assert float(states[f"{serial}_ac_power"]) == 382
        assert float(states[f"{serial}_energy_total"]) == 21986
        assert float(states[f"{serial}_temperature"]) == 38.4
    _no_repairs(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert _PASSWORD not in str(diagnostics)
    assert diagnostics["logger"]["kind"] == "solis_web"
    assert diagnostics["logger"]["protocol"] == "http"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _states(hass, entry)[f"{serial}_energy_total"] == states[f"{serial}_energy_total"]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_both_mode_fills_from_the_mock_cloud(
    hass: HomeAssistant, solis_server: tuple[Any, str], target: Target
) -> None:
    """The stick's power wins. Month energy and the plant come from the mock cloud."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **entry_data(url),
            "source": SOURCE_BOTH,
            "logger_kind": "solis_web",
            "logger_host": target.host,
            "logger_port": target.port,
            "logger_password": _PASSWORD,
            "inverter_serial": NORTH_SERIAL,
        },
        options=entry_options() | {"scan_interval": 60},
        version=3,
        unique_id="web:test",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    states = _states(hass, entry)
    report = entry.runtime_data.logger_report()
    print("logger report:", report)
    assert report["state"] == "ok"
    assert states[f"{NORTH_SERIAL}_ac_power"] not in {"unknown", "unavailable", "missing"}
    assert states[f"{NORTH_SERIAL}_energy_month"] not in {"unknown", "unavailable", "missing"}
    if not target.live:
        assert float(states[f"{NORTH_SERIAL}_ac_power"]) == 382
    _no_repairs(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
