"""Regression tests for the third audit round."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from .test_web_stick import _cloud_snapshot
from custom_components.solis import local_transport as lt
from custom_components.solis.const import LOCAL_STATION
from custom_components.solis.models import SolisSnapshot


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


def test_degraded_modbus_read_keeps_the_local_device_set() -> None:
    """A Modbus entry never gains the cloud plant devices that its next read would prune."""
    cloud, donor = _cloud()
    last = lt.local_snapshot(
        replace(donor, station_id=LOCAL_STATION), "1902137671", datetime.now(UTC)
    )
    merged = lt._degraded_cloud(_ModbusSide(last), cloud, donor)  # type: ignore[arg-type]
    assert [s.station_id for s in merged.stations] == [LOCAL_STATION]
    assert [c.serial for c in merged.collectors] == ["1902137671"]
    assert merged.epms == ()
    assert merged.inverters[0].station_id == LOCAL_STATION
    assert merged.partial is True
    assert merged.source == "degraded"


def test_degraded_modbus_read_without_a_local_sample() -> None:
    """Before any local read only the placeholder plant is published."""
    cloud, donor = _cloud()
    merged = lt._degraded_cloud(_ModbusSide(None), cloud, donor)  # type: ignore[arg-type]
    assert [s.station_id for s in merged.stations] == [LOCAL_STATION]
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
