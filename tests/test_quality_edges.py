"""Branches the quality-scale coverage rule still has to see."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.solis.const import DOMAIN
from custom_components.solis.entity import _attach_via, inverter_device
from custom_components.solis.errors import SolisAuthError, SolisCloudError
from custom_components.solis.migration import async_migrate_entity_ids, device_for_identifier
from custom_components.solis.models import InverterSnapshot
from custom_components.solis.parser import (
    extract_records,
    local_date,
    page_count,
    parse_alarm,
    parse_collector,
    parse_epm,
    parse_inverter,
    parse_offset_hours,
    parse_station,
    plant_period_start,
    resolve_zone,
    unit_was_power_on_energy,
)
from custom_components.solis.transport import SolisCloudTransport


class _Client:
    """Scripted SolisCloud client for transport branches."""

    def __init__(self) -> None:
        self.stations: list[dict[str, Any]] = [{"id": "s1", "stationName": "North", "timeZone": 0}]
        self.detail: dict[str, Any] | None = {"id": "s1", "timeZone": 0}
        self.day: dict[str, Any] | None = None
        self.inverters: list[dict[str, Any]] = [{"id": "d1", "sn": "SN", "name": "Inv"}]
        self.inv_detail: dict[str, Any] = {
            "id": "d1",
            "sn": "SN",
            "state": 1,
            "pac": 1,
            "pacStr": "kW",
        }
        self.alarms: list[dict[str, Any]] = [{"sn": "SN", "state": "0", "alarmCode": "1"}]
        self.collectors: list[dict[str, Any]] = [{"sn": "C1", "model": "S2", "rssi": -40}]
        self.epms: list[dict[str, Any]] = [{"sn": "E1", "pEpmTotal": 1, "pEpmTotalStr": "kW"}]
        self.fail: dict[str, Exception] = {}

    def _raise(self, name: str) -> None:
        if name in self.fail:
            raise self.fail[name]

    async def user_station_list(self) -> list[dict[str, Any]]:
        self._raise("stations")
        return self.stations

    async def station_detail(self, station_id: str) -> dict[str, Any]:
        del station_id
        self._raise("detail")
        assert self.detail is not None
        return self.detail

    async def station_day_energy(self, station_id: str, day: str) -> dict[str, Any] | None:
        del station_id, day
        self._raise("day")
        return self.day

    async def inverter_list(self, station_id: str) -> list[dict[str, Any]]:
        del station_id
        self._raise("list")
        return self.inverters

    async def inverter_detail(self, device_id: str, serial: str) -> dict[str, Any]:
        del device_id, serial
        self._raise("inverter")
        return self.inv_detail

    async def alarm_list(self) -> list[dict[str, Any]]:
        self._raise("alarms")
        return self.alarms

    async def collector_list(self) -> list[dict[str, Any]]:
        self._raise("collectors")
        return self.collectors

    async def epm_list(self) -> list[dict[str, Any]]:
        self._raise("epms")
        return self.epms


def _transport(client: _Client, stations: set[str] | None = None) -> SolisCloudTransport:
    return SolisCloudTransport(client, stations if stations is not None else {"s1"})  # type: ignore[arg-type]


async def test_transport_reads_and_keeps_a_failed_detail() -> None:
    """A failed detail falls back, and a later failure reuses the cached detail."""
    client = _Client()
    transport = _transport(client)
    snapshot = await transport.async_read()
    assert snapshot.inverters[0].serial == "SN"
    assert snapshot.alarms and snapshot.collectors and snapshot.epms
    client.fail["inverter"] = SolisCloudError("detail down")
    again = await transport.async_read()
    assert again.partial
    assert again.inverters[0].serial == "SN"


async def test_transport_rejects_an_unknown_station_and_blank_serial() -> None:
    """A filter with no match fails, and a row without a serial is skipped."""
    client = _Client()
    with pytest.raises(SolisCloudError, match="no stations"):
        await _transport(client, {"other"}).async_read()
    client.inverters = [{"id": "", "sn": "", "name": "Nope"}]
    with pytest.raises(SolisCloudError, match="no inverters"):
        await _transport(client, {"s1"}).async_read()


async def test_transport_propagates_auth_and_uses_summary_timezone() -> None:
    """Auth failures are not swallowed. A missing detail uses the list timezone."""
    client = _Client()
    client.fail["detail"] = SolisAuthError("no")
    with pytest.raises(SolisAuthError):
        await _transport(client).async_read()
    client.fail = {"detail": SolisCloudError("detail down"), "day": SolisCloudError("day down")}
    snapshot = await _transport(client).async_read()
    assert snapshot.partial
    assert snapshot.stations[0].timezone_offset_hours == 0


async def test_inverter_list_without_a_cache_is_fatal() -> None:
    """The first failed inverter list cannot invent a previous list."""
    client = _Client()
    client.fail["list"] = SolisCloudError("down")
    with pytest.raises(SolisCloudError):
        await _transport(client).async_read()


async def test_empty_inverter_list_is_ignored_twice() -> None:
    """Two empty lists keep the previous inverters. The third is believed."""
    client = _Client()
    transport = _transport(client)
    await transport.async_read()
    client.inverters = []
    kept = await transport.async_read()
    assert kept.partial
    assert kept.inverters
    await transport.async_read()
    with pytest.raises(SolisCloudError, match="no inverters"):
        await transport.async_read()


async def test_every_station_is_read_when_the_filter_is_empty() -> None:
    """An empty station filter follows the whole account."""
    client = _Client()
    snapshot = await _transport(client, set()).async_read()
    assert snapshot.stations[0].station_id == "s1"


async def test_prune_drops_a_station_that_left_the_account() -> None:
    """A station that disappears is removed from the transport cache."""
    client = _Client()
    client.stations.append({"id": "s2", "stationName": "Barn", "timeZone": 8})
    transport = _transport(client, set())
    await transport.async_read()
    client.stations = [client.stations[0]]
    client.inverters = [{"id": "d1", "sn": "SN", "name": "Inv", "stationId": "s1"}]
    await transport.async_read()
    assert "s2" not in transport._lists


def test_parser_edges() -> None:
    """Cover the quiet parser branches: bad offsets, blank pages, and alarms."""
    assert extract_records({"data": {"page": {"records": "nope"}}}) == []
    assert page_count({"data": {"pages": "nope"}}) is None
    assert parse_offset_hours(None) is None
    assert parse_offset_hours("UTC+nope") is None
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    assert resolve_zone(None, "", "Europe/London", now) is not None
    assert resolve_zone(0, "", "Not/AZone", now) is None
    assert resolve_zone(0, "", "Europe/London", datetime(2026, 7, 15, 12)) is not None
    assert local_date(1, datetime(2026, 1, 2, 23)) == "2026-01-03"
    assert local_date(0, now, resolve_zone(None, "Europe/London", None, now)) == "2026-07-15"
    assert unit_was_power_on_energy("kW")
    assert not unit_was_power_on_energy("kWh")
    alarm = parse_alarm({"sn": "SN", "state": 0, "alarmCode": "9"})
    assert alarm.serial == "SN"
    collector = parse_collector({"sn": "C", "rssiLevel": "-70"})
    assert collector.rssi == -70
    epm = parse_epm({"sn": "E", "pEpmTotal": 1, "pEpmTotalStr": "kW"})
    assert epm.power_w == 1000
    parsed = parse_inverter({"sn": "SN", "dcInputType": 1, "uPv1": None, "state": 1})
    assert parsed.strings == ()
    assert parse_inverter({"sn": "SN", "batteryPower": None}).battery_power_w is None
    station = parse_station({"id": "s"}, {}, {"energy": 2, "energyStr": "kWh"})
    assert station.day_energy_kwh == 2
    assert station.capacity_kw is None
    start = plant_period_start(
        0,
        now,
        "month",
        resolve_zone(None, "Europe/London", None, now),
    )
    assert start == datetime(2026, 6, 30, 23, tzinfo=UTC)


def test_old_home_assistant_uses_via_device_tuple() -> None:
    """2025.10 links a subdevice with via_device, not via_device_id."""
    inverter = parse_inverter({"sn": "SN", "stationId": "s1", "name": "Inv"})
    assert isinstance(inverter, InverterSnapshot)
    with (
        patch("custom_components.solis.entity.MAJOR_VERSION", 2025),
        patch("custom_components.solis.entity.MINOR_VERSION", 10),
    ):
        info = inverter_device(inverter, "North", "device-id")
    assert info["via_device"] == (DOMAIN, "station:s1")  # type: ignore[typeddict-item]
    bare: dict[str, Any] = {}
    with (
        patch("custom_components.solis.entity.MAJOR_VERSION", 2026),
        patch("custom_components.solis.entity.MINOR_VERSION", 8),
    ):
        _attach_via(bare, "s1", None)  # type: ignore[arg-type]
    assert "via_device_id" not in bare


async def test_migration_ignores_other_platforms_and_unmapped_plants(
    hass: HomeAssistant,
) -> None:
    """Foreign platforms and plant sensors without a station id stay put."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=2)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        domain="sensor",
        platform="other",
        unique_id="SN1Roof_Energy_Today",
        config_entry=entry,
    )
    from custom_components.solis.migration import legacy_unique_id

    plant = legacy_unique_id("SN1", "Roof", "Daily On-grid Energy")
    registry.async_get_or_create(
        domain="sensor", platform=DOMAIN, unique_id=plant, config_entry=entry
    )
    async_migrate_entity_ids(hass, entry.entry_id, "Roof", {"SN1"})
    assert registry.async_get_entity_id("sensor", "other", "SN1Roof_Energy_Today") is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, plant) is not None


def test_device_lookup_falls_back_without_the_new_helper() -> None:
    """Home Assistant 2025.10 resolves a device from its identifier set."""

    class _Device:
        def __init__(self, entries: set[str]) -> None:
            self.config_entries = entries

    class _Registry:
        def __init__(self, device: object) -> None:
            self._device = device

        def async_get_device(self, *, identifiers: set[tuple[str, str]]) -> object:
            del identifiers
            return self._device

    owned = _Device({"entry"})
    assert (
        device_for_identifier(_Registry(owned), (DOMAIN, "inverter:SN"), "entry") is owned  # type: ignore[arg-type]
    )
    assert device_for_identifier(_Registry(None), (DOMAIN, "inverter:SN"), "entry") is None  # type: ignore[arg-type]
    assert (
        device_for_identifier(_Registry(_Device(set())), (DOMAIN, "inverter:SN"), "entry")  # type: ignore[arg-type]
        is None
    )


async def test_user_can_remove_a_device_the_cloud_dropped(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A device that is still reporting cannot be deleted. A stranger can."""
    from .conftest import mock_entry

    _state, url = solis_server
    entry = mock_entry(url)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    from custom_components.solis import async_remove_config_entry_device

    devices = dr.async_get(hass)
    live = device_for_identifier(devices, (DOMAIN, "inverter:1011A021B150001"), entry.entry_id)
    assert live is not None
    loaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert loaded is not None
    assert not await async_remove_config_entry_device(hass, loaded, live)  # type: ignore[arg-type]
    stranger = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "inverter:missing")},
    )
    assert await async_remove_config_entry_device(hass, loaded, stranger)  # type: ignore[arg-type]
