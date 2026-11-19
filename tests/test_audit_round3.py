"""Regression tests for the third audit round."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .test_web_stick import _cloud_snapshot
from custom_components.solis import local_transport as lt
from custom_components.solis.const import LOCAL_STATION
from custom_components.solis.errors import SolisLoggerBusy
from custom_components.solis.models import EpmSnapshot, SolisSnapshot


class _ModbusSide:
    """The parts of a Modbus or Solarman reader that a degraded cloud read touches."""

    cloud_fill = False

    def __init__(self, last: SolisSnapshot | None) -> None:
        self.last_snapshot = last
        self.inverter_serial = "1031000000000001"
        self.logger_serial = 1902137671

    def adopt_serial(self, serial: str) -> None:
        self.inverter_serial = serial


def _cloud() -> tuple[SolisSnapshot, Any]:
    cloud = _cloud_snapshot()
    donor = cloud.inverters[0]
    # Give the account a plant and a logger of its own, as SolisCloud does.
    plant = lt.local_snapshot(
        replace(donor, station_id="1298491919449"), "CLOUDLOGGER", cloud.fetched_at
    )
    station = replace(plant.stations[0], station_id="1298491919449", name="North roof")
    collector = replace(plant.collectors[0], station_id="1298491919449")
    cloud = replace(cloud, stations=(station,), collectors=(collector,))
    return cloud, replace(donor, station_id="1298491919449")


def test_degraded_modbus_read_publishes_the_cloud_plant_but_not_its_loggers() -> None:
    """The plant keeps updating while the stick is out; the stick stays the only logger.

    A local read keeps plant and EPM devices but prunes loggers it does not see, so
    SolisCloud's loggers would come and go.
    """
    cloud, donor = _cloud()
    epm = EpmSnapshot(serial="EPM1", station_id="1298491919449", state=1, power_w=5)
    cloud = replace(cloud, epms=(epm,))
    last = lt.local_snapshot(
        replace(donor, station_id=LOCAL_STATION), "1902137671", datetime.now(UTC)
    )
    merged = lt._degraded_cloud(_ModbusSide(last), cloud, donor)  # type: ignore[arg-type]
    assert [s.station_id for s in merged.stations] == [LOCAL_STATION, "1298491919449"]
    assert [c.serial for c in merged.collectors] == ["1902137671"]
    assert merged.epms == (epm,)
    assert merged.inverters[0].station_id == LOCAL_STATION
    assert merged.partial is True
    assert merged.source == "degraded"


def test_degraded_modbus_read_without_a_local_sample() -> None:
    """Before any local read the cloud plant is published beside the placeholder."""
    cloud, donor = _cloud()
    merged = lt._degraded_cloud(_ModbusSide(None), cloud, donor)  # type: ignore[arg-type]
    assert [s.station_id for s in merged.stations] == [LOCAL_STATION, "1298491919449"]
    assert merged.collectors == ()
    assert merged.inverters[0].station_id == LOCAL_STATION
    assert merged.partial is True


async def test_a_logger_repair_from_before_a_reload_is_cleared(hass: Any) -> None:
    """A repair opened by an earlier coordinator is cleared once the stick sleeps."""
    from homeassistant.helpers import issue_registry as ir

    from .test_coordinator import _entry, _issue_keys
    from custom_components.solis.const import DOMAIN, ISSUE_LOGGER_LOCKED
    from custom_components.solis.coordinator import SolisCoordinator

    entry = _entry(hass)
    issue_id = f"{ISSUE_LOGGER_LOCKED}_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_LOGGER_LOCKED,
    )
    coordinator = SolisCoordinator(hass, entry, object())  # type: ignore[arg-type]
    coordinator._clear_logger_lock()
    assert not any(key.startswith(ISSUE_LOGGER_LOCKED) for key in _issue_keys(hass))


def test_a_pending_alarm_that_ended_is_not_active() -> None:
    """The API's own sample: state 0 (pending) with an end time in the past."""
    from custom_components.solis.parser import parse_alarm

    row = {
        "id": "-1",
        "stationId": "1298491919448631809",
        "alarmDeviceSn": "00FFC0011557001",
        "alarmCode": "2129",
        "alarmLevel": "1",
        "alarmBeginTime": 1687918458326,
        "alarmEndTime": 1687918484635,
        "state": "0",
    }
    assert parse_alarm(row).active is False
    assert parse_alarm({**row, "alarmEndTime": 0}).active is True
    assert parse_alarm({**row, "alarmEndTime": None}).active is True
    future = int(datetime.now(UTC).timestamp() * 1000) + 3_600_000
    assert parse_alarm({**row, "alarmEndTime": future}).active is True
    assert parse_alarm({**row, "state": "2", "alarmEndTime": None}).active is False


def test_platform_error_codes_map_to_the_right_failure() -> None:
    """Code 1 is a retryable hiccup, 数据异常 the account fault, 429 a rate limit."""
    import pytest

    from custom_components.solis.api import _raise_for_code
    from custom_components.solis.errors import (
        SolisDataError,
        SolisRateLimitError,
        SolisServerError,
    )

    with pytest.raises(SolisServerError):
        _raise_for_code("/p", {"success": True, "code": "1", "msg": "Communication error"})
    with pytest.raises(SolisDataError):
        _raise_for_code("/p", {"success": True, "code": "1", "msg": "数据异常"})
    with pytest.raises(SolisRateLimitError):
        _raise_for_code("/p", {"success": False, "code": "429", "msg": "Too Many Requests"})


def test_bracketed_time_zone_string() -> None:
    """stationDetail sends timeZoneStr as "(UTC+10:00)"."""
    from custom_components.solis.parser import parse_offset_hours

    assert parse_offset_hours("(UTC+10:00)") == 10.0
    assert parse_offset_hours("(UTC-03:30)") == -3.5
    assert parse_offset_hours("( UTC )") == 0.0
    assert parse_offset_hours("UTC+08:00") == 8.0


async def test_the_first_entry_keeps_a_shared_plant(hass: Any) -> None:
    """A second backup entry on the same account leaves out what the first registered."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from homeassistant.helpers import entity_registry as er

    from custom_components.solis.const import DOMAIN
    from custom_components.solis.sensor import _owned

    cloud, _donor = _cloud()
    station = cloud.stations[0]
    collector = cloud.collectors[0]
    first = MockConfigEntry(domain=DOMAIN, data={}, unique_id="first")
    first.add_to_hass(hass)
    second = MockConfigEntry(domain=DOMAIN, data={}, unique_id="second")
    second.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{station.station_id}_day_energy", config_entry=first
    )
    registry.async_get_or_create("sensor", DOMAIN, f"{collector.serial}_rssi", config_entry=first)

    assert _owned(hass, first.entry_id, cloud) is cloud
    kept = _owned(hass, second.entry_id, cloud)
    assert kept.stations == () and kept.collectors == ()
    assert kept.inverters == cloud.inverters

    # Once the first entry is gone, the second one takes the plant over.
    await hass.config_entries.async_remove(first.entry_id)
    assert _owned(hass, second.entry_id, cloud) is cloud


async def test_v5_frames_that_are_not_inverter_data_are_skipped() -> None:
    """A reply with frame type 1 (the logger itself) is skipped, like pysolarmanv5."""
    from custom_components.solis.solarman import SolarmanClient, build_response, crc16
    from mock_soliscloud.stick import SERIAL

    body = bytes((1, 4, 2)) + (1234).to_bytes(2, "big")
    modbus = body + crc16(body).to_bytes(2, "little")
    other = bytearray(build_response(SERIAL, 7, modbus))
    other[11] = 0x01
    good = build_response(SERIAL, 7, modbus)
    reader = asyncio.StreamReader()
    reader.feed_data(bytes(other) + good)
    client = SolarmanClient("127.0.0.1", SERIAL, timeout=1, gap=0)
    assert await client._read_matching(reader, None, 7, 1, 1) == modbus  # type: ignore[arg-type]


async def test_modbus_tcp_header_close_and_socket_error() -> None:
    """EOF mid-header is a closed logger; an OSError goes through classify_io."""
    from custom_components.solis.errors import SolisLoggerOffline
    from custom_components.solis.solarman import ModbusTcpClient

    client = ModbusTcpClient("127.0.0.1", port=502, timeout=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x00\x01\x00")
    reader.feed_eof()
    with pytest.raises(SolisLoggerOffline) as closed:
        await client._read_response(reader)
    assert closed.value.reason == "closed"

    broken = MagicMock()
    broken.readexactly = AsyncMock(side_effect=ConnectionResetError())
    with pytest.raises((SolisLoggerOffline, SolisLoggerBusy)):
        await client._read_response(broken)


def test_detach_device_before_2026_8_removes_the_entry_only() -> None:
    """Older cores keep the device and drop this entry from it."""
    from custom_components.solis import migration

    registry = MagicMock()
    with (
        patch.object(migration, "MAJOR_VERSION", 2026),
        patch.object(migration, "MINOR_VERSION", 7),
    ):
        migration.detach_device(registry, "device-1", "entry-1")
    registry.async_update_device.assert_called_once_with(
        "device-1", remove_config_entry_id="entry-1"
    )
    registry.async_remove_device.assert_not_called()

    registry = MagicMock()
    migration.detach_device(registry, "device-1", "entry-1")
    registry.async_remove_device.assert_called_once_with("device-1")
