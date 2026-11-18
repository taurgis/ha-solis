"""Cloud parser, client, transport and migration fixes against capture shapes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, web
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.solis.api import SolisCloudClient
from custom_components.solis.const import DOMAIN
from custom_components.solis.errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisServerError,
)
from custom_components.solis.migration import (
    LEGACY_LABELS,
    async_migrate_entity_ids,
    legacy_unique_id,
)
from custom_components.solis.models import snapshot_from_dict, snapshot_to_dict
from custom_components.solis.parser import (
    parse_epm,
    parse_inverter,
    parse_station,
    plant_zone_name,
    resolve_zone,
)
from custom_components.solis.transport import SolisCloudTransport
from custom_components.solis.units import to_identity
from mock_soliscloud.app import MockState, _details
from mock_soliscloud.catalog import (
    BARN,
    GRID_SERIAL,
    HYBRID_SERIAL,
    KEY_ID,
    NORTH,
    NORTH_SERIAL,
    SECRET,
    inverter_details,
    station_details,
    stations,
)

# Battery and grid fields as the single-phase hybrid capture
# (SolisCloud.Net fixtures, 2026-10-14) sends them in inverterDetail.
_CAPTURE_DISCHARGE = {
    "sn": "1031000000000001",
    "id": "1",
    "productModel": "2",
    "batteryPower": -1.364,
    "batteryPowerStr": "kW",
    "batteryPowerPec": "1",
    "batteryPowerZheng": 0,
    "batteryPowerFu": 1364.0,
    "storageBatteryCurrent": -26.5,
    "storageBatteryCurrentStr": "A",
    "psum": -0.373,
    "psumStr": "kW",
    "psumPec": "1",
    "familyLoadPower": 0.673,
    "familyLoadPowerStr": "kW",
    "familyLoadPowerPec": "1",
    "inverterTemperature": 44.8,
    "inverterTemperatureUnit": "℃",
    "gridPurchasedTotalEnergy": 521.88,
    "gridPurchasedTotalEnergyStr": "kWh",
    "gridSellTotalEnergy": 0.0,
    "gridSellTotalEnergyStr": "kWh",
    "homeLoadTotalEnergy": 4.712,
    "homeLoadTotalEnergyStr": "MWh",
}


def test_zheng_and_fu_are_watts_not_battery_power_str() -> None:
    """Fu 1364 W with batteryPower -1.364 kW is -1364 W, not -1.364 MW."""
    parsed = parse_inverter(_CAPTURE_DISCHARGE)
    assert parsed.battery_power_w == pytest.approx(-1364.0)
    assert parsed.grid_power_w == pytest.approx(-373.0)
    assert parsed.consumption_power_w == pytest.approx(673.0)
    assert parsed.temperature_c == pytest.approx(44.8)


def test_battery_sign_from_split_magnitude_from_battery_power() -> None:
    """Zheng/Fu give the direction; batteryPower with Str and Pec gives the size."""
    charging = parse_inverter(
        {
            **_CAPTURE_DISCHARGE,
            "batteryPower": 1364,
            "batteryPowerStr": "kW",
            "batteryPowerPec": "0.001",
            "batteryPowerZheng": 1364.0,
            "batteryPowerFu": 0,
            "storageBatteryCurrent": -3,
        }
    )
    assert charging.battery_power_w == pytest.approx(1364.0)
    unsigned = parse_inverter({**_CAPTURE_DISCHARGE, "batteryPower": 0.46})
    assert unsigned.battery_power_w == pytest.approx(-460.0)
    only_split = {
        key: value for key, value in _CAPTURE_DISCHARGE.items() if not key.startswith("battery")
    }
    only_split.update({"batteryPowerZheng": 250.0, "batteryPowerFu": 0})
    assert parse_inverter(only_split).battery_power_w == pytest.approx(250.0)
    idle = parse_inverter(
        {**_CAPTURE_DISCHARGE, "batteryPower": 0.0, "batteryPowerZheng": 0, "batteryPowerFu": 0}
    )
    assert idle.battery_power_w == 0.0
    assert str(idle.battery_power_w) == "0.0"
    rounding = parse_inverter({**_CAPTURE_DISCHARGE, "batteryPower": 0.0, "batteryPowerFu": 0.4})
    assert rounding.battery_power_w == pytest.approx(-0.4)
    none_at_all = {
        key: value for key, value in _CAPTURE_DISCHARGE.items() if not key.startswith("battery")
    }
    assert parse_inverter(none_at_all).battery_power_w is None


def test_lifetime_grid_and_load_totals() -> None:
    """Upstream's Total Energy Purchased / On-grid / Used come from inverter detail."""
    parsed = parse_inverter(_CAPTURE_DISCHARGE)
    assert parsed.grid_import_total_kwh == pytest.approx(521.88)
    assert parsed.grid_export_total_kwh == 0.0
    assert parsed.load_total_kwh == pytest.approx(4712.0)


def test_pec_applies_to_every_unit_field() -> None:
    """A *Pec factor scales power, energy, capacity and EPM values."""
    detail = {
        "sn": "1031000000000001",
        "pac": 1154,
        "pacStr": "kW",
        "pacPec": "0.001",
        "psum": -373,
        "psumStr": "kW",
        "psumPec": "0.001",
        "familyLoadPower": 673,
        "familyLoadPowerStr": "kW",
        "familyLoadPowerPec": "0.001",
        "eToday": 4770.9,
        "eTodayStr": "kWh",
        "eTodayPec": "0.001",
        "eTotal": 4770.9,
        "eTotalStr": "MWh",
        "eTotalPec": "0.001",
        "batteryTodayChargeEnergy": 2500,
        "batteryTodayChargeEnergyStr": "kWh",
        "batteryTodayChargeEnergyPec": "0.001",
    }
    parsed = parse_inverter(detail)
    assert parsed.ac_power_w == pytest.approx(1154.0)
    assert parsed.grid_power_w == pytest.approx(-373.0)
    assert parsed.consumption_power_w == pytest.approx(673.0)
    assert parsed.energy_today_kwh == pytest.approx(4.7709)
    assert parsed.energy_total_kwh == pytest.approx(4770.9)
    assert parsed.battery_charge_today_kwh == pytest.approx(2.5)
    station = parse_station(
        {"id": "P"},
        {
            "capacity": 8000,
            "capacityStr": "kWp",
            "capacityPec": "0.001",
            "dayEnergy": 21620,
            "dayEnergyStr": "kWh",
            "dayEnergyPec": "0.001",
        },
        {"gridSellEnergy": 300, "gridSellEnergyStr": "kWh", "gridSellEnergyPec": "0.01"},
    )
    assert station.capacity_kw == pytest.approx(8.0)
    assert station.day_energy_kwh == pytest.approx(21.62)
    assert station.grid_export_today_kwh == pytest.approx(3.0)
    epm = parse_epm({"sn": "E1", "pEpmTotal": 800, "pEpmTotalStr": "kW", "pEpmTotalPec": "0.001"})
    assert epm.power_w == pytest.approx(800.0)


def test_alias_reads_its_own_unit() -> None:
    """The misspelt current key does not borrow another key's unit."""
    parsed = parse_inverter(
        {"sn": "S", "bstteryCurrent": -4.0, "bstteryCurrentStr": "A", "batteryCurrentStr": "kA"}
    )
    assert parsed.battery_current_a == -4.0


@pytest.mark.parametrize("unit", ["℃", "°C", "C", "c", "ºC", None])
def test_temperature_unit_variants(unit: str | None) -> None:
    """Every spelling of degrees Celsius is accepted."""
    assert to_identity(44.8, unit, default="c") == pytest.approx(44.8)
    parsed = parse_inverter(
        {"sn": "S", "inverterTemperature": 44.8, "inverterTemperatureStr": unit}
    )
    assert parsed.temperature_c == pytest.approx(44.8)


def test_unknown_temperature_unit_is_dropped() -> None:
    """Fahrenheit is not converted silently."""
    assert to_identity(100, "°F", default="c") is None


def test_station_detail_home_load_energy() -> None:
    """Station detail's homeLoadEnergy fills today's load without the day list."""
    station = parse_station(stations()[1], station_details()[BARN], None)
    assert station.load_today_kwh == pytest.approx(6.5)
    assert station.timezone_name == "Asia/Shanghai"


def test_plant_zone_name() -> None:
    """timeZoneStandardId is read from detail and blanks are ignored."""
    assert plant_zone_name({"timeZoneStandardId": " Africa/Harare "}) == "Africa/Harare"
    assert plant_zone_name({"timeZoneStandardId": ""}) is None
    assert plant_zone_name({}) is None
    assert plant_zone_name(None) is None


def test_resolve_zone_prefers_the_plant_iana_zone() -> None:
    """The plant zone beats Home Assistant's when it agrees with the Solis offset."""
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC)
    zone = resolve_zone(0.0, "", "Europe/Amsterdam", summer, "Europe/London")
    assert zone == ZoneInfo("Europe/London")
    # A plant zone that disagrees with the numeric offset is not trusted.
    zone = resolve_zone(2.0, "", "Africa/Johannesburg", summer, "Asia/Tokyo")
    assert zone == ZoneInfo("Africa/Johannesburg")
    # An unloadable name falls back to the old heuristic.
    assert resolve_zone(2.0, "", "Africa/Johannesburg", summer, "Not/AZone") == ZoneInfo(
        "Africa/Johannesburg"
    )
    assert resolve_zone(2.0, "", None, summer, "../etc/passwd") is None
    # "plant" keeps the fixed numeric offset; an explicit name still wins.
    assert resolve_zone(0.0, "plant", "Europe/London", summer, "Europe/London") is None
    assert resolve_zone(0.0, "Asia/Tokyo", None, summer, "Europe/London") == ZoneInfo("Asia/Tokyo")
    # Without a numeric offset the plant zone is still used.
    naive = datetime(2026, 1, 1, 12)
    assert resolve_zone(None, None, None, naive, "Europe/London") == ZoneInfo("Europe/London")


def test_mock_catalog_matches_the_capture_shapes() -> None:
    """The mock now sends Zheng/Fu in watts, Pec factors and the plant zone."""
    details = inverter_details()
    hybrid = parse_inverter(details[HYBRID_SERIAL])
    assert hybrid.battery_power_w == pytest.approx(-1200.0)
    assert hybrid.grid_power_w == pytest.approx(-500.0)
    assert hybrid.load_total_kwh == pytest.approx(4712.0)
    grid = parse_inverter(details[GRID_SERIAL])
    assert grid.ac_power_w == pytest.approx(1500.0)
    north = parse_inverter(details[NORTH_SERIAL])
    assert north.temperature_c == pytest.approx(42.5)
    assert north.grid_import_total_kwh == pytest.approx(1204.0)
    charged = _details(MockState(force_charge=True), datetime.now(UTC))[HYBRID_SERIAL]
    assert parse_inverter(charged).battery_power_w == pytest.approx(1200.0)
    assert charged["batteryPowerZheng"] == 1200.0


def test_new_fields_survive_storage() -> None:
    """Stored snapshots keep the totals and the plant zone."""
    from custom_components.solis.models import SolisSnapshot

    snapshot = SolisSnapshot(
        stations=(parse_station(stations()[0], station_details()[NORTH], None),),
        inverters=(parse_inverter(_CAPTURE_DISCHARGE),),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime(2026, 10, 14, tzinfo=UTC),
        partial=False,
    )
    restored = snapshot_from_dict(snapshot_to_dict(snapshot))
    assert restored.inverters[0].load_total_kwh == pytest.approx(4712.0)
    assert restored.inverters[0].grid_import_total_kwh == pytest.approx(521.88)
    assert restored.stations[0].timezone_name == "Europe/London"


# --- HTTP client --------------------------------------------------------------


@pytest.fixture
async def raw_api(socket_enabled: None) -> AsyncIterator[tuple[web.Application, str]]:
    """A bare server whose responder each test swaps."""
    app = web.Application()
    app["responder"] = None

    async def handle(request: web.Request) -> web.StreamResponse:
        return await app["responder"](request)  # type: ignore[no-any-return]

    app.router.add_post("/v1/api/{name}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        yield app, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def _call(raw_api: tuple[web.Application, str], responder: Any) -> Any:
    app, url = raw_api
    app["responder"] = responder
    async with ClientSession() as session:
        client = SolisCloudClient(
            session, base_url=url, key_id="id", secret="s", endpoint_interval=0, attempts=1
        )
        return await client.user_station_list()


async def test_403_is_auth_unless_html(raw_api: tuple[web.Application, str]) -> None:
    """A JSON or plain 403 rejects the key; an HTML 403 is a gateway fault."""

    async def plain(_request: web.Request) -> web.Response:
        return web.Response(status=403, text="Forbidden")

    async def json_other(_request: web.Request) -> web.Response:
        return web.json_response({"code": "B0001", "msg": "denied"}, status=403)

    async def html(_request: web.Request) -> web.Response:
        return web.Response(status=403, text="<html>waf</html>", content_type="text/html")

    async def html_no_type(_request: web.Request) -> web.Response:
        return web.Response(status=403, body=b"<!DOCTYPE html><p>blocked</p>")

    with pytest.raises(SolisAuthError) as raised:
        await _call(raw_api, plain)
    assert raised.value.code == "403"
    with pytest.raises(SolisAuthError) as raised:
        await _call(raw_api, json_other)
    assert raised.value.code == "B0001"
    for responder in (html, html_no_type):
        with pytest.raises(SolisServerError):
            await _call(raw_api, responder)


async def test_success_false_and_message_key(raw_api: tuple[web.Application, str]) -> None:
    """success: false fails even with code 0, and ``message`` is read like ``msg``."""

    async def failed(_request: web.Request) -> web.Response:
        return web.json_response(
            {"success": False, "code": "0", "message": "collector missing", "data": None}
        )

    async def failed_text(_request: web.Request) -> web.Response:
        return web.json_response({"success": "false", "code": "0", "data": None})

    async def clock(_request: web.Request) -> web.Response:
        return web.json_response(
            {"success": False, "code": "0", "message": "request time exceeds 15 minutes"}
        )

    async def ok(_request: web.Request) -> web.Response:
        return web.json_response(
            {"success": True, "code": "0", "msg": "success", "data": {"page": {"records": []}}}
        )

    with pytest.raises(SolisCloudError, match="collector missing"):
        await _call(raw_api, failed)
    with pytest.raises(SolisCloudError, match=" 0: 0"):
        await _call(raw_api, failed_text)
    with pytest.raises(SolisClockError):
        await _call(raw_api, clock)
    assert await _call(raw_api, ok) == []


async def test_mock_wrong_secret_is_a_bare_403(solis_server: tuple[MockState, str]) -> None:
    """The mock answers a wrong secret like SolisCloud: HTTP 403, not R0000."""
    _state, url = solis_server
    async with ClientSession() as session:
        response = await session.post(f"{url}/v1/api/userStationList", data=b"{}")
        assert response.status == 403
        assert await response.text() == "Forbidden"
        client = SolisCloudClient(
            session, base_url=url, key_id=KEY_ID, secret="wrong", endpoint_interval=0, attempts=1
        )
        with pytest.raises(SolisAuthError):
            await client.user_station_list()


# --- Transport ----------------------------------------------------------------


async def test_transport_skips_day_list_and_rejects_a_ghost_detail(
    solis_server: tuple[MockState, str],
) -> None:
    """Station detail covers today, and a placeholder detail is not published."""
    state, url = solis_server
    async with ClientSession() as session:
        client = SolisCloudClient(
            session, base_url=url, key_id=KEY_ID, secret=SECRET, endpoint_interval=0
        )
        original = client.inverter_list

        async def with_ghost(station_id: str) -> list[dict[str, Any]]:
            rows = await original(station_id)
            if station_id == NORTH:
                rows.append({"id": "2999", "sn": "1031000000000001", "name": "Ghost"})
            return rows

        client.inverter_list = with_ghost  # type: ignore[method-assign]
        transport = SolisCloudTransport(client, set(), hass_time_zone="UTC")
        snapshot = await transport.async_read()
    assert "/v1/api/stationDayEnergyList" not in state.calls
    assert snapshot.partial
    ghost = next(inv for inv in snapshot.inverters if inv.serial == "1031000000000001")
    assert ghost.unreachable
    assert ghost.data_timestamp is None
    assert all(inv.serial != "000000000000000" for inv in snapshot.inverters)
    north = snapshot.station(NORTH)
    assert north is not None
    assert north.timezone_name == "Europe/London"
    assert north.load_today_kwh == pytest.approx(3.2)
    assert transport._zones["Europe/London"] == ZoneInfo("Europe/London")


async def test_ghost_detail_keeps_the_cached_copy(solis_server: tuple[MockState, str]) -> None:
    """A placeholder after a good read keeps serving the last good detail."""
    _state, url = solis_server
    async with ClientSession() as session:
        client = SolisCloudClient(
            session, base_url=url, key_id=KEY_ID, secret=SECRET, endpoint_interval=0
        )
        transport = SolisCloudTransport(client, {NORTH})
        first = await transport.async_read()
        original = client.inverter_detail

        async def placeholder(device_id: str, serial: str) -> dict[str, Any]:
            data = await original(device_id, serial)
            return {**data, "sn": "000000000000000", "dataTimestamp": "0"}

        client.inverter_detail = placeholder  # type: ignore[method-assign]
        second = await transport.async_read()
    before = next(inv for inv in first.inverters if inv.serial == NORTH_SERIAL)
    after = next(inv for inv in second.inverters if inv.serial == NORTH_SERIAL)
    assert second.partial
    assert after.unreachable
    assert after.energy_total_kwh == before.energy_total_kwh
    assert after.data_timestamp == before.data_timestamp


async def test_transport_falls_back_to_day_list(solis_server: tuple[MockState, str]) -> None:
    """Without today's counters in station detail the day list is still asked."""
    state, url = solis_server
    async with ClientSession() as session:
        client = SolisCloudClient(
            session, base_url=url, key_id=KEY_ID, secret=SECRET, endpoint_interval=0
        )
        original = client.station_detail

        async def thin(station_id: str) -> dict[str, Any]:
            data = await original(station_id)
            return {key: value for key, value in data.items() if not key.startswith("homeLoad")} | {
                "timeZoneStandardId": "Not/AZone"
            }

        client.station_detail = thin  # type: ignore[method-assign]
        transport = SolisCloudTransport(client, {BARN})
        snapshot = await transport.async_read()
        await transport._warm_zone("Not/AZone")
    assert "/v1/api/stationDayEnergyList" in state.calls
    barn = snapshot.station(BARN)
    assert barn is not None
    assert barn.load_today_kwh == pytest.approx(6.5)
    assert transport._zones["Not/AZone"] is None


async def test_zone_warmup_survives_a_bad_key() -> None:
    """A name the zone loader refuses is cached as unusable."""
    transport = SolisCloudTransport(object(), set())  # type: ignore[arg-type]
    await transport._warm_zone("/abs/path")
    await transport._warm_zone(None)
    assert transport._zones == {"/abs/path": None}


# --- Migration ----------------------------------------------------------------


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=2)
    entry.add_to_hass(hass)
    return entry


async def test_new_upstream_labels_migrate(hass: HomeAssistant) -> None:
    """Grid phase power, the measurement timestamp and lifetime totals keep history."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    cases = {
        "Grid Phase1 Power": "SN1_phase_1_power",
        "Grid Phase3 Power": "SN1_phase_3_power",
        "Timestamp Measurements Received": "SN1_data_timestamp",
        "Total Energy Purchased": "SN1_grid_import_total",
        "Total On-grid Energy": "SN1_grid_export_total",
        "Total Energy Used": "SN1_load_total",
    }
    rows = {
        label: registry.async_get_or_create(
            "sensor", DOMAIN, legacy_unique_id("SN1", "Roof", label), config_entry=entry
        )
        for label in cases
    }
    async_migrate_entity_ids(hass, entry.entry_id, "Roof", {"SN1"}, {"SN1": "P"})
    for label, new in cases.items():
        assert registry.async_get_entity_id("sensor", DOMAIN, new) == rows[label].entity_id
    assert LEGACY_LABELS["phase_2_power"] == "Grid Phase2 Power"


async def test_ginlong_consumption_alias(hass: HomeAssistant) -> None:
    """The Ginlong label for lifetime load maps onto the same key."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        legacy_unique_id("SN1", "Solis", "Total Consumption Energy"),
        config_entry=entry,
    )
    async_migrate_entity_ids(hass, entry.entry_id, "Solis", {"SN1"})
    assert registry.async_get_entity_id("sensor", DOMAIN, "SN1_load_total") == old.entity_id


async def test_multi_inverter_plant_keeps_every_history_row(hass: HomeAssistant) -> None:
    """Two inverters' copies of a plant counter both survive; the first gets the id."""
    entry = _entry(hass)
    registry = er.async_get(hass)
    first = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        legacy_unique_id("SN1", "Roof", "Daily On-grid Energy"),
        config_entry=entry,
    )
    second = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        legacy_unique_id("SN2", "Roof", "Daily On-grid Energy"),
        config_entry=entry,
    )
    stations_map = {"SN1": "plant", "SN2": "plant"}
    for _ in range(3):
        async_migrate_entity_ids(hass, entry.entry_id, "Roof", {"SN1", "SN2"}, stations_map)
    assert registry.async_get(first.entity_id) is not None
    assert registry.async_get(second.entity_id) is not None
    holder = registry.async_get_entity_id("sensor", DOMAIN, "plant_grid_export_today")
    assert holder in {first.entity_id, second.entity_id}
    other = second.entity_id if holder == first.entity_id else first.entity_id
    remaining = registry.async_get(other)
    assert remaining is not None
    assert remaining.unique_id.endswith("Daily_On-grid_Energy")
