"""Runtime fixes: setup and reload, repairs, stick reauth, devices, entities and icons."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

from .conftest import mock_entry
from custom_components.solis import _store_inverter_serial, _store_logger_host, _store_text
from custom_components.solis.binary_sensor import SolisAlarmBinarySensor
from custom_components.solis.const import (
    DATA_DEGRADED,
    DOMAIN,
    LOCAL_STATION,
    REASON_GATEWAY,
    REASON_WEB_AUTH,
    SOURCE_BOTH,
    SOURCE_LOCAL,
)
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.diagnostics import async_get_config_entry_diagnostics
from custom_components.solis.entity import collector_device, epm_device
from custom_components.solis.errors import (
    SolisAuthError,
    SolisLoggerAuthError,
    SolisLoggerBusy,
    SolisLoggerOffline,
)
from custom_components.solis.local_transport import asleep_snapshot, local_snapshot
from custom_components.solis.models import (
    CollectorSnapshot,
    EpmSnapshot,
    InverterSnapshot,
    Phase,
    SolisSnapshot,
)
from custom_components.solis.sensor import _BATTERY, _INVERTER, _LIFETIME
from mock_soliscloud.catalog import NORTH_SERIAL

PACKAGE = Path(__file__).parent.parent / "custom_components" / "solis"
LOGGER_SERIAL = "1234567890"
LOGGER_MAC = "BC54F9AABBCC"
SERIAL = "1031000000000001"
PLACEHOLDER = f"L{LOGGER_SERIAL}"


def _inverter(**kwargs: Any) -> InverterSnapshot:
    base: dict[str, Any] = {
        "serial": SERIAL,
        "device_id": SERIAL,
        "station_id": LOCAL_STATION,
        "name": SERIAL,
        "model": "Solis 4G",
        "product_model": "1",
        "hmi_version": "0F000F",
        "state": 1,
        "ac_output_type": 0,
        "data_timestamp": datetime.now(UTC),
        "collector_serial": LOGGER_SERIAL,
        "ac_power_w": 1200.0,
        "frequency_hz": 50.0,
        "temperature_c": 27.0,
        "energy_today_kwh": 3.1,
        "energy_month_kwh": 30.0,
        "energy_year_kwh": 2690.0,
        "energy_total_kwh": 21970.0,
        "battery_power_w": None,
        "battery_soc": None,
        "battery_soh": None,
        "battery_voltage_v": None,
        "battery_current_a": None,
        "battery_charge_today_kwh": None,
        "battery_discharge_today_kwh": None,
        "battery_charge_total_kwh": None,
        "battery_discharge_total_kwh": None,
        "grid_power_w": 10.0,
        "consumption_power_w": 20.0,
        "force_charge_soc": None,
        "force_discharge_soc": None,
        "phases": (Phase(index=1, voltage_v=231.0, current_a=5.2, power_w=1200.0),),
        "strings": (),
        "data_source": "local",
    }
    base.update(kwargs)
    return InverterSnapshot(**base)


def _local(**kwargs: Any) -> SolisSnapshot:
    return local_snapshot(_inverter(**kwargs), LOGGER_SERIAL, datetime.now(UTC))


class _Transport:
    """Hands out snapshots in order (the last one repeats) and records close()."""

    def __init__(self, *results: SolisSnapshot | Exception) -> None:
        self.results = list(results)
        self.close = AsyncMock()
        self.degraded_reason = ""

    async def async_read(self) -> SolisSnapshot:
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, Exception):
            raise item
        return item


def _local_entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "192.0.2.10",
            "logger_serial": LOGGER_SERIAL,
            "logger_mac": LOGGER_MAC,
            "logger_kind": "solarman",
            "inverter_serial": SERIAL,
            **data,
        },
        options={},
        version=3,
        unique_id=LOGGER_SERIAL,
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, transport: _Transport) -> None:
    with patch("custom_components.solis._build_transport", return_value=transport):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _issues(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


# Setup, reload and transport lifetime ----------------------------------------


@pytest.mark.parametrize("error", [SolisAuthError("rejected"), RuntimeError("boom")])
async def test_first_refresh_failure_closes_the_transport(
    hass: HomeAssistant, error: Exception
) -> None:
    """A rejected key or an unexpected error at setup must not leak the logger socket."""
    entry = _local_entry(hass)
    transport = _Transport(error)
    await _setup(hass, entry, transport)
    assert entry.state in {ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY}
    transport.close.assert_awaited()


async def test_entry_data_writes_do_not_reload(hass: HomeAssistant) -> None:
    """A poll that learns a new address or framing keeps the running entry."""
    entry = _local_entry(hass)
    transport = _Transport(_local())
    await _setup(hass, entry, transport)
    coordinator = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "logger_host": "192.0.2.11", "logger_protocol": "v5"}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is coordinator
    transport.close.assert_not_awaited()


async def test_a_learned_serial_moves_ids_and_reloads_once(hass: HomeAssistant) -> None:
    """The placeholder serial becomes the real one; entities come back under the new id."""
    entry = _local_entry(hass, inverter_serial=PLACEHOLDER)
    placeholder = _local(serial=PLACEHOLDER, device_id=PLACEHOLDER, name=PLACEHOLDER)
    transport = _Transport(placeholder)
    await _setup(hass, entry, transport)
    registry = er.async_get(hass)
    old_entity = registry.async_get_entity_id("sensor", DOMAIN, f"{PLACEHOLDER}_ac_power")
    assert old_entity is not None
    first = entry.runtime_data
    transport.results = [_local()]
    with patch("custom_components.solis._build_transport", return_value=transport):
        _store_inverter_serial(hass, entry, SERIAL)
        await hass.async_block_till_done()
    assert entry.data["inverter_serial"] == SERIAL
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not first
    # History stays on the original entity id.
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_ac_power") == old_entity
    assert float(hass.states.get(old_entity).state) == 1200  # type: ignore[union-attr]


async def test_migrations_run_at_setup_and_for_new_serials_only(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Polls with the same inverters do not walk the entity registry again."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    with patch("custom_components.solis.async_migrate_entity_ids") as at_setup:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert at_setup.call_count == 1
    coordinator = entry.runtime_data
    with (
        patch("custom_components.solis.sensor.async_migrate_entity_ids") as per_poll,
        patch("custom_components.solis.sensor.async_migrate_devices") as devices,
    ):
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert per_poll.call_count == 0
        newcomer = replace(coordinator.data.inverters[0], serial="1999000000000009")
        coordinator.async_set_updated_data(
            replace(coordinator.data, inverters=(*coordinator.data.inverters, newcomer))
        )
        await hass.async_block_till_done()
        assert per_poll.call_count == 1
        assert per_poll.call_args.args[3] == {"1999000000000009"}
        assert devices.call_count == 1


# Repairs -----------------------------------------------------------------------


async def test_logger_repairs_are_not_fixable_and_link_the_docs(hass: HomeAssistant) -> None:
    """Ten minutes of a refusing gateway opens a learn-more repair, not a dead-end fix flow."""
    entry = _local_entry(hass)
    transport = _Transport(SolisLoggerBusy("refused", reason=REASON_GATEWAY))
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    coordinator._last_good = _local()
    await coordinator._async_update_data()
    assert not _issues(hass)
    coordinator._degraded_since = time.monotonic() - 601
    await coordinator._async_update_data()
    issue = _issues(hass)[f"logger_gateway_{entry.entry_id}"]
    assert issue.is_fixable is False
    assert issue.learn_more_url is not None
    assert issue.learn_more_url.endswith("docs/troubleshooting.md#logger-gateway")
    assert not (PACKAGE / "repairs.py").exists()


async def test_migrated_control_notice_survives_a_restart(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """The one-off migration notice is persistent, read-only, and linked to the docs."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "portal_domain": url,
            "portal_key_id": "key",
            "portal_secret": "secret",
            "portal_plant_id": "1900000000000001001",
            "portal_control_api": True,
            "name": "Roof",
        },
        version=1,
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue = _issues(hass)[f"legacy_control_{entry.entry_id}"]
    assert issue.is_fixable is False
    assert issue.is_persistent is True
    assert issue.learn_more_url is not None
    assert issue.learn_more_url.endswith("#legacy-control")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert f"legacy_control_{entry.entry_id}" in _issues(hass)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"legacy_control_{entry.entry_id}" not in _issues(hass)


async def test_a_failed_setup_leftover_repair_clears_on_the_first_good_poll(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A retired repair from an older release is deleted, not left open forever."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    for key in ("invalid_auth", "cloud_outage", "abnormal_data"):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{key}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
        )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not _issues(hass)


# Stick password ------------------------------------------------------------------


async def test_a_rejected_stick_password_under_cloud_cover_asks_once(
    hass: HomeAssistant,
) -> None:
    """SolisCloud keeps the values; reauth starts once and no repair repeats it."""
    entry = _local_entry(hass, source=SOURCE_BOTH, logger_kind="solis_web")
    covered = replace(_local(data_source=DATA_DEGRADED), source=DATA_DEGRADED, partial=True)
    transport = _Transport(covered)
    transport.degraded_reason = REASON_WEB_AUTH
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    with patch.object(entry, "async_start_reauth") as reauth:
        await coordinator._async_update_data()
        # SolisCloud covers the stick, so one rejection does not ask yet.
        reauth.assert_not_called()
        coordinator._degraded_since = time.monotonic() - 601
        coordinator._stick_rejected_since = time.monotonic() - 601
        await coordinator._async_update_data()
        reauth.assert_called_once_with(hass)
        await coordinator._async_update_data()
        reauth.assert_called_once_with(hass)
        assert not _issues(hass)
        # The password was fixed elsewhere and later rejected again: ask again, after a while.
        transport.degraded_reason = ""
        await coordinator._async_update_data()
        transport.degraded_reason = REASON_WEB_AUTH
        await coordinator._async_update_data()
        assert reauth.call_count == 1
        coordinator._stick_rejected_since = time.monotonic() - 601
        await coordinator._async_update_data()
        assert reauth.call_count == 2


async def test_a_local_stick_password_waits_for_three_rejections(hass: HomeAssistant) -> None:
    """One 401 holds the last values; three in a row ask for the new password."""
    entry = _local_entry(hass, logger_kind="solis_web")
    rejected = SolisLoggerAuthError("web password rejected")
    waiting = SolisLoggerAuthError("in backoff", waiting=True)
    transport = _Transport(_local(), rejected, waiting, rejected, _local(), rejected, rejected)
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    await coordinator._async_update_data()
    for _ in range(3):
        held = await coordinator._async_update_data()
        assert held.source == DATA_DEGRADED
    assert coordinator.auth_failed is False
    # A good read starts the count again.
    await coordinator._async_update_data()
    assert (await coordinator._async_update_data()).source == DATA_DEGRADED
    assert (await coordinator._async_update_data()).source == DATA_DEGRADED
    with pytest.raises(ConfigEntryAuthFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.translation_key == "update_logger_web_auth"
    assert coordinator.auth_failed is True
    assert not _issues(hass)


async def test_a_local_stick_password_asks_after_ten_minutes(hass: HomeAssistant) -> None:
    """Reads skipped by the backoff do not count, but ten minutes of rejection does."""
    entry = _local_entry(hass, logger_kind="solis_web")
    waiting = SolisLoggerAuthError("in backoff", waiting=True)
    transport = _Transport(_local(), SolisLoggerAuthError("rejected"), waiting)
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    coordinator._stick_rejected_since = time.monotonic() - 601
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_another_stick_fault_breaks_the_rejection_run(hass: HomeAssistant) -> None:
    """Rejections only count in a row."""
    entry = _local_entry(hass, logger_kind="solis_web")
    rejected = SolisLoggerAuthError("rejected")
    transport = _Transport(
        _local(), rejected, rejected, SolisLoggerBusy("odd page", reason="web_page"), rejected
    )
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    for _ in range(5):
        await coordinator._async_update_data()
    assert coordinator._stick_rejections == 1


# Pace ----------------------------------------------------------------------------


async def test_a_paused_logger_leaves_the_night_pace(hass: HomeAssistant) -> None:
    """After a night of slow polls, a logger that resets is polled at the base rate again."""
    entry = _local_entry(hass)
    good = _local()
    transport = _Transport(
        good,
        SolisLoggerOffline("night"),
        SolisLoggerOffline("night"),
        SolisLoggerOffline("night"),
        SolisLoggerBusy("reset", reason="locked"),
        SolisLoggerOffline("night"),
        SolisLoggerOffline("night"),
        replace(asleep_snapshot(good), source=DATA_DEGRADED),
    )
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    base = coordinator.update_interval
    await coordinator._async_update_data()
    for _ in range(3):
        held = await coordinator._async_update_data()
        assert held.source == "asleep"
    assert coordinator.update_interval is not None and coordinator.update_interval > base
    degraded = await coordinator._async_update_data()
    assert degraded.source == DATA_DEGRADED
    assert coordinator.update_interval == base
    assert coordinator._asleep_polls == 0
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert coordinator.update_interval > base
    await coordinator._async_update_data()
    assert coordinator.update_interval == base
    assert coordinator._asleep_polls == 0


# Devices -------------------------------------------------------------------------


async def test_local_entry_has_no_plant_device_and_a_mac_logger(hass: HomeAssistant) -> None:
    """No shared "SolisCloud plant" for local entries; the old one is detached."""
    entry = _local_entry(hass)
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"station:{LOCAL_STATION}")},
        name="Local",
        model="SolisCloud plant",
    )
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"inverter:{SERIAL}")},
        name="Local Solis 4G",
        via_device_id=stale.id,
    )
    await _setup(hass, entry, _Transport(_local()))
    assert entry.state is ConfigEntryState.LOADED
    assert registry.async_get(stale.id) is None
    devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
    assert all(device.model != "SolisCloud plant" for device in devices)
    inverter = registry.async_get_device_by_identifier(
        (DOMAIN, f"inverter:{SERIAL}"), entry.entry_id
    )
    assert inverter is not None and inverter.via_device_id is None
    logger = registry.async_get_device_by_identifier(
        (DOMAIN, f"collector:{LOGGER_SERIAL}"), entry.entry_id
    )
    assert logger is not None
    assert logger.name == f"Logger {LOGGER_SERIAL}"
    assert logger.connections == {(CONNECTION_NETWORK_MAC, "bc:54:f9:aa:bb:cc")}


async def test_single_phase_entities_start_disabled(hass: HomeAssistant) -> None:
    """Phase 1 repeats AC power on a single-phase inverter; three phases stay enabled."""
    entry = _local_entry(hass)
    three = (
        Phase(index=1, voltage_v=231.0, current_a=5.2, power_w=400.0),
        Phase(index=2, voltage_v=232.0, current_a=5.1, power_w=400.0),
        Phase(index=3, voltage_v=230.0, current_a=5.3, power_w=400.0),
    )
    transport = _Transport(_local())
    await _setup(hass, entry, transport)
    registry = er.async_get(hass)
    single = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_phase_1_voltage") or ""
    )
    assert single is not None
    assert single.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    other = "1031000000000002"
    entry.runtime_data.async_set_updated_data(
        replace(
            entry.runtime_data.data,
            inverters=(
                *entry.runtime_data.data.inverters,
                _inverter(serial=other, device_id=other, ac_output_type=1, phases=three),
            ),
        )
    )
    await hass.async_block_till_done()
    for index in (1, 2, 3):
        row = registry.async_get(
            registry.async_get_entity_id("sensor", DOMAIN, f"{other}_phase_{index}_power") or ""
        )
        assert row is not None and row.disabled_by is None


def test_subdevice_names_are_translated_and_never_doubled() -> None:
    """``EPM EPM1002`` reads as ``EPM1002``; a numeric serial gets the translated prefix."""
    doubled = epm_device(EpmSnapshot(serial="EPM1002", station_id="1", state=1, power_w=0.0))
    assert doubled.get("name") == "EPM1002"
    assert "translation_key" not in doubled
    plain = epm_device(EpmSnapshot(serial="1002", station_id="1", state=1, power_w=0.0))
    assert plain.get("translation_key") == "epm"
    assert plain.get("translation_placeholders") == {"serial": "1002"}
    logger = collector_device(
        CollectorSnapshot(serial="99", station_id="1", model=None, state=1, rssi=None)
    )
    assert logger.get("translation_key") == "logger"
    assert "connections" not in logger
    for name in ("strings.json", "translations/en.json", "translations/nl.json"):
        device = json.loads((PACKAGE / name).read_text(encoding="utf-8"))["device"]
        assert "{serial}" in device["logger"]["name"]
        assert "{serial}" in device["epm"]["name"]


# Entities ------------------------------------------------------------------------


def test_soc_limits_and_health_are_not_battery_level() -> None:
    """Only the charge level is a battery device class; lifetime totals show one decimal."""
    by_key = {item.key: item for item in (*_INVERTER, *_BATTERY, *_LIFETIME)}
    assert by_key["battery_soc"].device_class is SensorDeviceClass.BATTERY
    for key in ("battery_soh", "force_charge_soc", "force_discharge_soc"):
        assert by_key[key].device_class is None
        assert by_key[key].native_unit_of_measurement == "%"
    for key in ("energy_total", "battery_charge_total", "grid_import_total", "load_total"):
        assert by_key[key].suggested_display_precision == 1
    assert by_key["energy_today"].suggested_display_precision == 2


async def test_lifetime_grid_totals_appear_when_reported(hass: HomeAssistant) -> None:
    """Grid import/export and load totals are added once SolisCloud reports them."""
    entry = _local_entry(hass)
    await _setup(hass, entry, _Transport(_local()))
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_grid_import_total") is None
    coordinator = entry.runtime_data
    reported = replace(
        coordinator.data.inverters[0],
        grid_import_total_kwh=812.4,
        grid_export_total_kwh=5120.0,
        load_total_kwh=None,
    )
    coordinator.async_set_updated_data(replace(coordinator.data, inverters=(reported,)))
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_grid_import_total")
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "812.4"  # type: ignore[union-attr]
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_grid_export_total")
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SERIAL}_load_total") is None


async def test_energy_guard_advances_on_updates_not_on_reads(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Reading the state never moves the guard; a dip in a poll is still held."""
    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{NORTH_SERIAL}_energy_total")
    assert entity_id is not None
    sensor = hass.data["entity_components"]["sensor"].get_entity(entity_id)
    before = float(hass.states.get(entity_id).state)  # type: ignore[union-attr]
    guard = (sensor._energy_last, sensor._period_key, sensor._carried)
    coordinator = entry.runtime_data
    dipped = tuple(
        replace(item, energy_total_kwh=1.0) if item.serial == NORTH_SERIAL else item
        for item in coordinator.data.inverters
    )
    # Swap the data without an update: reads must not run the guard.
    coordinator.data = replace(coordinator.data, inverters=dipped)
    for _ in range(3):
        assert sensor.native_value == before
    assert (sensor._energy_last, sensor._period_key, sensor._carried) == guard
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id).state) == before  # type: ignore[union-attr]
    raised = tuple(
        replace(item, energy_total_kwh=before + 5) if item.serial == NORTH_SERIAL else item
        for item in coordinator.data.inverters
    )
    coordinator.async_set_updated_data(replace(coordinator.data, inverters=raised))
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id).state) == before + 5  # type: ignore[union-attr]


def test_alarm_text_is_not_recorded() -> None:
    """The long alarm message and advice stay out of the recorder."""
    assert {"message", "advice"} <= SolisAlarmBinarySensor._unrecorded_attributes


# Diagnostics ---------------------------------------------------------------------


async def test_diagnostics_hide_serials_plants_and_mac(hass: HomeAssistant) -> None:
    """Serials, plant ids and the logger MAC do not leave Home Assistant."""
    entry = _local_entry(hass, station_ids=["1900000000000001001"])
    await _setup(hass, entry, _Transport(_local()))
    payload = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(payload, default=str)
    for secret in (SERIAL, LOGGER_SERIAL, LOGGER_MAC, "1900000000000001001", "192.0.2.10"):
        assert secret not in text
    assert payload["entry"]["data"]["inverter_serial"] == "**REDACTED**"
    assert payload["snapshot"]["inverters"][0]["energy_total_kwh"] == 21970.0


# Icons ---------------------------------------------------------------------------


def test_icons_follow_the_icon_translation_schema() -> None:
    """Every icon is an mdi name; ranges are numeric; device-class icons are not overridden."""
    icons = json.loads((PACKAGE / "icons.json").read_text(encoding="utf-8"))["entity"]
    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))["entity"]
    classed = {
        item.key
        for item in (*_INVERTER, *_BATTERY, *_LIFETIME)
        if item.device_class not in (None, SensorDeviceClass.ENUM)
    }
    for platform, rows in icons.items():
        for key, row in rows.items():
            assert key in strings[platform], key
            assert set(row) <= {"default", "state", "range"}
            assert row["default"].startswith("mdi:")
            for value in row.get("state", {}).values():
                assert value.startswith("mdi:")
            if "range" in row:
                assert len(row["range"]) >= 2
                for threshold, value in row["range"].items():
                    float(threshold)
                    assert value.startswith("mdi:")
            if platform == "sensor" and key != "battery_power":
                assert key not in classed, key
    power = icons["sensor"]["battery_power"]["range"]
    ordered = sorted(power.items(), key=lambda item: float(item[0]))
    assert [icon for _threshold, icon in ordered] == [
        "mdi:battery-arrow-down",
        "mdi:battery",
        "mdi:battery-arrow-up",
    ]
    assert icons["binary_sensor"]["alarm"]["state"]["off"] == "mdi:check-circle-outline"


async def test_poll_learned_address_and_framing_are_stored_without_reload(
    hass: HomeAssistant,
) -> None:
    """A new DHCP address, MAC or framing is saved once; the running entry is kept."""
    entry = _local_entry(hass, logger_mac="")
    await _setup(hass, entry, _Transport(_local()))
    coordinator = entry.runtime_data
    _store_logger_host(hass, entry, "192.0.2.10", "")
    _store_text(hass, entry, "logger_protocol", "v5")
    _store_text(hass, entry, "logger_protocol", "v5")
    _store_logger_host(hass, entry, "192.0.2.20", LOGGER_MAC)
    _store_inverter_serial(hass, entry, SERIAL)
    await hass.async_block_till_done()
    assert entry.data["logger_host"] == "192.0.2.20"
    assert entry.data["logger_mac"] == LOGGER_MAC
    assert entry.data["logger_protocol"] == "v5"
    assert entry.runtime_data is coordinator
    assert entry.state is ConfigEntryState.LOADED
