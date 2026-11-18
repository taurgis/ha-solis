"""Behaviour fixed by the quality audit: cloud pacing, rollover, parsing and housekeeping."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import time
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed

from .conftest import entry_data, entry_options
from .flow_helpers import logger_form, pick, start_user
from .test_coordinator import _entry, _issue_keys, _Scripted, _snapshot
from .test_web_stick import _Cloud, _cloud_snapshot, _transport
from custom_components.solis import async_migrate_entry, async_remove_entry, local_transport as lt
from custom_components.solis.cloud_fill import fill_from_cloud
from custom_components.solis.const import DOMAIN, LOGGER_WEB, SOURCE_BOTH, WEB_DEFAULT_PASSWORD
from custom_components.solis.coordinator import SolisCoordinator, snapshot_store
from custom_components.solis.energy import consider_energy
from custom_components.solis.errors import SolisAuthError, SolisClockError, SolisLoggerOffline
from custom_components.solis.local_transport import SolisPriorityTransport, asleep_snapshot
from custom_components.solis.loggers.web import _complete, parse_inverter
from custom_components.solis.parser import parse_epoch
from custom_components.solis.registers import _scale
from custom_components.solis.solarman import ModbusTcpClient, parse_discovery
from custom_components.solis.units import as_float
from mock_soliscloud.web_stick import WebStickEmulator

PAGE = "1031000000000001;0F000F;E4;38.4;382;2.800000;21986;NO;"


def test_page_values_must_be_plausible() -> None:
    """Power must be plausible. Implausible optional fields are dropped, not the page."""
    for bad in (PAGE.replace("382", "inf"), PAGE.replace("382", "nan"), PAGE.replace("382", "2e7")):
        with pytest.raises(ValueError):
            parse_inverter(bad)
    assert parse_inverter(PAGE.replace("38.4", "nan")).temperature_c is None
    assert parse_inverter(PAGE.replace("38.4", "900")).temperature_c is None
    assert parse_inverter(PAGE.replace("21986", "1e99")).energy_total_kwh is None
    assert parse_inverter(PAGE.replace("2.800000", "-1")).energy_today_kwh is None
    assert not parse_inverter(PAGE.replace(";NO;", ";;")).in_alarm
    assert parse_inverter(PAGE.replace("382", "-3")).power_w == -3.0


def test_a_reply_is_complete_at_its_declared_length() -> None:
    """The client stops at Content-Length instead of waiting for the stick to hang up."""
    head = b"HTTP/1.0 200 OK\r\nContent-Length: 4\r\n\r\n"
    assert not _complete(b"HTTP/1.0 200 OK\r\nContent-Le")
    assert not _complete(head + b"ab")
    assert _complete(head + b"abcd")
    assert not _complete(b"HTTP/1.0 200 OK\r\n\r\nabcd")


def test_numbers_and_times_from_the_cloud_are_bounded() -> None:
    """Non-finite numbers are absent, and a timestamp outside the calendar is ignored."""
    assert as_float("nan") is None
    assert as_float(float("inf")) is None
    assert as_float("12.5") == 12.5
    assert parse_epoch(9e15) is None
    assert parse_epoch(1_700_000_000) is not None


def test_register_scaling_has_no_float_noise() -> None:
    """28 tenths is 2.8 and not 2.8000000000000003."""
    assert _scale({1: 28}, 1, 0.1) == 2.8
    assert _scale({1: 272}, 1, 0.1) == 27.2
    assert _scale({1: 5001}, 1, 0.01) == 50.01


def test_discovery_reply_needs_an_ip_address() -> None:
    """Any LAN device can answer the broadcast, so the host must be an address."""
    assert parse_discovery(b"192.168.0.10,BC54F9F57B5C,2200001234")
    assert parse_discovery(b"not-an-ip,BC54F9F57B5C,2200001234") is None
    assert parse_discovery(b",BC54F9F57B5C,2200001234") is None


def test_a_day_that_starts_from_yesterdays_number_resets_once() -> None:
    """The poll that crosses midnight may still carry yesterday's total."""
    stale = consider_energy(12.3, 12.3, monotonic=False, period_changed=True)
    assert stale == 12.3
    morning = consider_energy(stale, 0.0, monotonic=False, period_changed=False, carried=True)
    assert morning == 0.0
    # Once the day has moved on, a lower number is a glitch again.
    assert consider_energy(3.0, 0.0, monotonic=False, period_changed=False) == 3.0
    assert consider_energy(12.3, 0.0, monotonic=True, period_changed=False, carried=True) == 12.3


def test_a_night_snapshot_clears_voltages_and_temperature() -> None:
    """Power is zero at night, and so are the readings that need a running inverter."""
    asleep = asleep_snapshot(_cloud_snapshot())
    inverter = asleep.inverters[0]
    assert inverter.temperature_c is None
    assert inverter.strings and all(pv.voltage_v is None for pv in inverter.strings)
    assert inverter.phases and all(phase.voltage_v is None for phase in inverter.phases)


async def test_the_cloud_is_read_once_while_the_stick_is_paused(
    stick: WebStickEmulator,
) -> None:
    """A wrong password reaches the coordinator (reauth) and is not hidden by the cloud."""
    from custom_components.solis.errors import SolisLoggerAuthError, SolisLoggerBusy
    from custom_components.solis.web_transport import SolisWebTransport

    cloud = _Cloud()
    wrong = SolisWebTransport("127.0.0.1", "nope", port=stick.port, timeout=2)
    transport = SolisPriorityTransport(SOURCE_BOTH, wrong, cloud)
    with pytest.raises(SolisLoggerAuthError):
        await transport.async_read()
    for _ in range(2):
        try:
            await transport.async_read()
        except SolisLoggerBusy:
            pass
    assert cloud.reads <= 1


async def test_a_rejected_cloud_key_is_reported_and_retried(stick: WebStickEmulator) -> None:
    """The local read still works. The transport keeps the cloud error for the coordinator."""

    class _Rejecting(_Cloud):
        error: Exception | None = SolisAuthError("bad key")

        async def async_read(self) -> Any:
            if self.error is not None:
                raise self.error
            return await super().async_read()

    cloud = _Rejecting()
    transport = SolisPriorityTransport(SOURCE_BOTH, _transport(stick), cloud)
    first = await transport.async_read()
    assert first.source == "local"
    assert first.partial is True
    assert isinstance(transport.cloud_error, SolisAuthError)
    assert 0 < transport._fill_due - time.monotonic() <= 60
    cloud.error = SolisClockError("skew")
    transport._fill_due = 0.0
    await transport.async_read()
    assert isinstance(transport.cloud_error, SolisClockError)
    cloud.error = None
    transport._fill_due = 0.0
    good = await transport.async_read()
    assert transport.cloud_error is None
    assert good.partial is False


async def test_a_slow_cloud_does_not_hold_up_the_local_read(
    stick: WebStickEmulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cloud read has a time budget. The page values are published without it."""

    class _Slow(_Cloud):
        async def async_read(self) -> Any:
            await asyncio.sleep(5)
            return await super().async_read()

    monkeypatch.setattr(lt, "_CLOUD_BUDGET", 0.05)
    transport = SolisPriorityTransport(SOURCE_BOTH, _transport(stick), _Slow())
    snapshot = await transport.async_read()
    assert snapshot.source == "local"
    assert snapshot.partial is True
    assert snapshot.inverters[0].ac_power_w == 382.0
    assert transport.cloud_error is None


async def test_cloud_fill_is_judged_by_the_reading_time(stick: WebStickEmulator) -> None:
    """A cloud sample fetched a second ago but measured an hour ago is not live data."""
    web = await _transport(stick).async_read()
    cloud = _cloud_snapshot()
    old = replace(cloud.inverters[0], data_timestamp=datetime.now(UTC) - timedelta(hours=1))
    sample = replace(cloud, inverters=(old,))
    assert lt._donor_is_fresh(cloud, cloud.inverters[0])
    assert not lt._donor_is_fresh(sample, old)
    assert not lt._donor_is_fresh(
        replace(cloud, fetched_at=datetime.now(UTC) - timedelta(hours=1)), cloud.inverters[0]
    )
    naive = replace(cloud.inverters[0], data_timestamp=datetime.now())
    assert lt._donor_is_fresh(replace(cloud, inverters=(naive,)), naive) in {True, False}
    undated = replace(cloud.inverters[0], data_timestamp=None)
    assert lt._donor_is_fresh(cloud, undated)
    stale = fill_from_cloud(web, sample, serial="1031000000000001", fresh=False)
    assert stale.inverters[0].frequency_hz is None
    assert stale.alarms == () and stale.epms == ()


async def test_a_degraded_cloud_sample_brings_todays_own_counter(
    stick: WebStickEmulator,
) -> None:
    """Yesterday's number from the last local read must not outvote the cloud after midnight."""
    local = _transport(stick)
    await local.async_read()
    previous = local.last_snapshot
    assert previous is not None
    cloud = _cloud_snapshot()
    donor = replace(cloud.inverters[0], energy_today_kwh=0.4, energy_total_kwh=100.0)
    merged = lt._degraded_cloud(local, replace(cloud, inverters=(donor,)), donor)
    inverter = merged.inverters[0]
    assert inverter.energy_today_kwh == 0.4
    assert inverter.energy_total_kwh == 21986.0
    assert lt._period_value(5.0, None) == 5.0


class _CloudSide:
    """A transport that read locally while SolisCloud rejected the key or the clock."""

    def __init__(self, error: Exception | None) -> None:
        self.cloud_error = error

    async def async_read(self) -> Any:
        return replace(_snapshot(), inverters=(_cloud_snapshot().inverters[0],))


async def test_a_rejected_cloud_key_opens_the_repair_and_one_reauth(hass: HomeAssistant) -> None:
    """Local data keeps flowing, and one reauth tells the user the API key no longer works."""
    entry = _entry(hass)
    transport = _CloudSide(SolisAuthError("rejected"))
    coordinator = SolisCoordinator(hass, entry, transport)  # type: ignore[arg-type]
    from unittest.mock import patch

    with patch.object(entry, "async_start_reauth") as reauth:
        await coordinator._async_update_data()
        await coordinator._async_update_data()
        reauth.assert_called_once_with(hass)
    # The reauth card is the notice: no repair duplicates it.
    assert not any(key.startswith("invalid_auth") for key in _issue_keys(hass))
    transport.cloud_error = SolisClockError("skew")
    await coordinator._async_update_data()
    assert any(key.startswith("clock_skew") for key in _issue_keys(hass))
    assert not any(key.startswith("invalid_auth") for key in _issue_keys(hass))
    transport.cloud_error = None
    await coordinator._async_update_data()
    assert not any(key.startswith(("invalid_auth", "clock_skew")) for key in _issue_keys(hass))


async def test_a_rejected_key_stops_polling_through_reauth(hass: HomeAssistant) -> None:
    """SolisCloud's own rejection raises ConfigEntryAuthFailed so Home Assistant starts reauth."""
    coordinator = SolisCoordinator(hass, _entry(hass), _Scripted([SolisAuthError("bad")]))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.auth_failed


async def test_snapshot_writes_are_spaced_and_flushed_on_shutdown(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flash storage is written every few minutes, and once more when the entry unloads."""
    coordinator = SolisCoordinator(hass, _entry(hass), _Scripted([_snapshot()] * 3))
    saves: list[dict[str, Any]] = []

    async def record(data: dict[str, Any]) -> None:
        saves.append(data)

    monkeypatch.setattr(coordinator._store, "async_save", record)
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert len(saves) == 1
    coordinator._saved_at -= 600
    await coordinator._async_update_data()
    assert len(saves) == 2
    await coordinator.async_shutdown()
    assert len(saves) == 3

    async def broken(data: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(coordinator._store, "async_save", broken)
    await coordinator._persist(_snapshot(), force=True)


async def test_removing_an_entry_deletes_its_snapshot_file(hass: HomeAssistant) -> None:
    """Nothing is left in .storage once the entry is gone."""
    entry = _entry(hass)
    store = snapshot_store(hass, entry.entry_id)
    await store.async_save({"kept": True})
    assert await store.async_load() is not None
    await async_remove_entry(hass, entry)  # type: ignore[arg-type]
    assert await snapshot_store(hass, entry.entry_id).async_load() is None


async def test_an_entry_from_a_newer_release_is_not_loaded(hass: HomeAssistant) -> None:
    """A downgrade must not run new data through old code."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=9)
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry) is False


async def test_reauth_works_for_an_entry_keyed_by_its_logger(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Local-plus-cloud entries have a logger id as unique id. The key id still identifies them."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), "source": SOURCE_BOTH},
        options=entry_options(),
        version=3,
        unique_id="web:1031000000000001",
    )
    entry.add_to_hass(hass)
    flow = await entry.start_reauth_flow(hass)
    done = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"key_id": "test-key", "key_secret": "test-secret"}
    )
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    other = await entry.start_reauth_flow(hass)
    bad = await hass.config_entries.flow.async_configure(
        other["flow_id"], {"key_id": "another-key", "key_secret": "test-secret"}
    )
    assert bad["type"] is FlowResultType.FORM


async def test_a_saved_web_password_is_not_sent_to_another_host(
    hass: HomeAssistant, solis_server: tuple[Any, str], stick: WebStickEmulator
) -> None:
    """Changing the host on Reconfigure asks for the password again."""
    _state, url = solis_server
    stick.state.password = "hunter2"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **entry_data(url),
            "source": SOURCE_BOTH,
            "logger_kind": LOGGER_WEB,
            "logger_host": "192.0.2.1",
            "logger_port": stick.port,
            "logger_password": "hunter2",
            "inverter_serial": "1031000000000001",
        },
        options=entry_options(),
        version=3,
        unique_id="web:1031000000000001",
    )
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    assert started["menu_options"] == ["reconfigure_cloud", "stations", "logger", "connection"]
    opened = await pick(hass, started["flow_id"], "logger")
    form = logger_form(logger_kind=LOGGER_WEB, logger_host="127.0.0.1", logger_port=stick.port)
    result = await hass.config_entries.flow.async_configure(opened["flow_id"], form)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "logger_password"
    assert result["errors"]["base"] == "logger_web_password"
    assert stick.state.password != WEB_DEFAULT_PASSWORD
    again = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert again["errors"]["base"] == "logger_web_password"
    retyped = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"logger_password": "hunter2"}
    )
    assert retyped["reason"] == "reconfigure_successful"
    assert entry.data["logger_host"] == "127.0.0.1"
    assert entry.data["logger_password"] == "hunter2"
    menu = await entry.start_reconfigure_flow(hass)
    shown = await pick(hass, menu["flow_id"], "connection")
    switched = await hass.config_entries.flow.async_configure(shown["flow_id"], {"source": "cloud"})
    assert switched["type"] is FlowResultType.ABORT
    assert entry.data["source"] == "cloud"
    assert entry.data["logger_host"] == "127.0.0.1"


async def test_the_key_form_keeps_what_was_typed(hass: HomeAssistant) -> None:
    """A wrong key does not clear the other fields."""
    result = await start_user(hass, "cloud")
    again = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": "abc"},
    )
    assert again["errors"]["base"] == "key_required"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in again["data_schema"].schema
        if key.description
    }
    assert suggested["key_secret"] == "abc"
    assert suggested["api_url"] == "http://127.0.0.1:9"


async def test_modbus_tcp_garbage_is_a_named_fault(socket_enabled: None) -> None:
    """A reply that is not a read response closes the socket and names the fault."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readexactly(12)
        writer.write(request[:2] + b"\x00\x00\x00\x02\x01\x04")
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=1, gap=0)
        with pytest.raises(SolisLoggerOffline) as garbage:
            await client.read_input(1, 3004, 1)
        assert garbage.value.reason == "garbage"
        await client.close()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()
