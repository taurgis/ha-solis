"""Cloud outages, bad payloads, and per-inverter failures."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from aiohttp import ClientConnectionError, ClientSession, web
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .test_runtime import _number, _refresh, _setup, _state
from custom_components.solis.api import SolisCloudClient
from custom_components.solis.const import DOMAIN
from custom_components.solis.errors import SolisServerError
from custom_components.solis.models import (
    SolisSnapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)
from custom_components.solis.parser import parse_inverter, parse_station
from mock_soliscloud.app import MockState, create_app
from mock_soliscloud.catalog import (
    HYBRID_SERIAL,
    KEY_ID,
    NORTH_SERIAL,
    SECRET,
    inverter_details,
    station_details,
    stations,
)

POWER = "sensor.north_roof_s6_gr1p5k_ac_power"
TOTAL = "sensor.north_roof_s6_gr1p5k_energy_total"
_SLEEP = "custom_components.solis.api.asyncio.sleep"


def test_snapshot_roundtrip_drops_location() -> None:
    """Stored snapshots rebuild energy and do not keep plant coordinates."""
    summary = stations()[0]
    detail = station_details()[str(summary["id"])]
    station = parse_station(summary, detail, None)
    inverter = parse_inverter(next(iter(inverter_details().values())))
    snapshot = SolisSnapshot(
        stations=(station,),
        inverters=(inverter,),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
    )
    restored = snapshot_from_dict(snapshot_to_dict(snapshot))
    assert restored.inverters[0].serial == inverter.serial
    assert restored.inverters[0].energy_total_kwh == inverter.energy_total_kwh
    assert restored.inverters[0].phases
    assert restored.stations[0].name == station.name
    assert restored.stations[0].latitude is None
    assert restored.stations[0].address is None
    with pytest.raises((KeyError, TypeError, ValueError)):
        snapshot_from_dict({"stations": []})


def test_out_of_range_samples_are_dropped() -> None:
    """Impossible frequency, temperature and state of charge become unknown."""
    parsed = parse_inverter(
        {
            "sn": "X",
            "id": "1",
            "fac": 10,
            "facStr": "Hz",
            "inverterTemperature": 400,
            "inverterTemperatureStr": "C",
            "batteryCapacitySoc": 250,
            "batteryHealthSoh": -1,
            "socChargingSet": 140,
            "eTotal": -5,
            "eTotalStr": "kWh",
        }
    )
    assert parsed.frequency_hz is None
    assert parsed.temperature_c is None
    assert parsed.battery_soc is None
    assert parsed.battery_soh is None
    assert parsed.force_charge_soc is None
    assert parsed.energy_total_kwh is None


async def test_circuit_breaker_stops_calling(solis_server: tuple[MockState, str]) -> None:
    """Repeated gateway failures open the circuit and skip the network."""
    state, url = solis_server
    state.mode = "server_error"
    async with ClientSession() as session:
        client = SolisCloudClient(
            session,
            base_url=url,
            key_id=KEY_ID,
            secret=SECRET,
            endpoint_interval=0,
            attempts=1,
        )
        for _ in range(4):
            with pytest.raises(SolisServerError):
                await client.user_station_list()
        calls = len(state.calls)
        with pytest.raises(SolisServerError, match="circuit is open"):
            await client.user_station_list()
    assert len(state.calls) == calls


async def test_html_and_garbage_are_not_auth_errors(solis_server: tuple[MockState, str]) -> None:
    """An HTML 403 and a garbage body are gateway failures."""
    state, url = solis_server
    async with ClientSession() as session:
        client = SolisCloudClient(
            session,
            base_url=url,
            key_id=KEY_ID,
            secret=SECRET,
            endpoint_interval=0,
            attempts=1,
        )
        state.mode = "html"
        with pytest.raises(SolisServerError):
            await client.user_station_list()
        state.mode = "garbage"
        with pytest.raises(SolisServerError):
            await client.user_station_list()
        state.mode = "partial_json"
        with pytest.raises(SolisServerError):
            await client.user_station_list()


async def test_connection_errors_and_cancellation(solis_server: tuple[MockState, str]) -> None:
    """DNS or TLS failures are transient, and cancellation is not retried."""
    _state, url = solis_server

    class _Boom:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        async def __aenter__(self) -> None:
            raise self._exc

        async def __aexit__(self, *_args: object) -> bool:
            return False

    class _Session:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc

        def post(self, *_args: object, **_kwargs: object) -> _Boom:
            return _Boom(self._exc)

    dns = SolisCloudClient(
        _Session(ClientConnectionError("dns lookup failed")),  # type: ignore[arg-type]
        base_url=url,
        key_id=KEY_ID,
        secret=SECRET,
        endpoint_interval=0,
        attempts=1,
    )
    with pytest.raises(SolisServerError, match="dns"):
        await dns.user_station_list()
    tls = SolisCloudClient(
        _Session(ClientConnectionError("certificate verify failed")),  # type: ignore[arg-type]
        base_url=url,
        key_id=KEY_ID,
        secret=SECRET,
        endpoint_interval=0,
        attempts=1,
    )
    with pytest.raises(SolisServerError, match="certificate"):
        await tls.user_station_list()
    cancelled = SolisCloudClient(
        _Session(asyncio.CancelledError()),  # type: ignore[arg-type]
        base_url=url,
        key_id=KEY_ID,
        secret=SECRET,
        endpoint_interval=0,
        attempts=3,
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled.post("/v1/api/userStationList", {})


async def test_partial_inverter_failure_leaves_the_others(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """One inverter detail can fail without blanking the rest of the account."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.fail_serials = {NORTH_SERIAL}
    with patch(_SLEEP, new=AsyncMock()):
        await _refresh(hass, entry)
    assert _state(hass, POWER) == "unavailable"
    assert _number(hass, TOTAL) == 12500
    assert _number(hass, "sensor.barn_hybrid_battery_power") == -1200
    state.fail_serials = set()
    await _refresh(hass, entry)
    assert _number(hass, POWER) == 1250
    assert _number(hass, TOTAL) == 12500


async def test_bad_bodies_do_not_start_reauth(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """HTML, garbage and HTTP 503 keep the last values and do not reauthenticate."""
    state, url = solis_server
    entry = await _setup(hass, url)
    for mode in ("html", "garbage", "partial_json", "unavailable"):
        state.mode = mode
        with patch(_SLEEP, new=AsyncMock()):
            await _refresh(hass, entry)
        assert _number(hass, POWER) == 1250
        assert _number(hass, TOTAL) == 12500
        flows = hass.config_entries.flow.async_progress()
        assert not any(flow["context"].get("source") == "reauth" for flow in flows)
        state.mode = "ok"
        await _refresh(hass, entry)


async def test_stale_snapshot_and_energy_spikes(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """A sample older than the last poll is ignored. Resets and spikes do not move the total."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.mode = "stale"
    await _refresh(hass, entry)
    assert _number(hass, POWER) == 1250
    assert _number(hass, TOTAL) == 12500
    state.mode = "ok"
    await _refresh(hass, entry)
    state.timestamp_offset_s = -7200
    state.pac_scale = 2
    await _refresh(hass, entry)
    assert _number(hass, POWER) == 1250
    assert _number(hass, TOTAL) == 12500
    state.timestamp_offset_s = 3 * 24 * 3600
    await _refresh(hass, entry)
    assert _number(hass, POWER) == 1250
    state.timestamp_offset_s = 0
    state.pac_scale = 1
    state.energy_glitch = "spike"
    await _refresh(hass, entry)
    assert _number(hass, TOTAL) == 12500
    state.energy_glitch = "reset"
    await _refresh(hass, entry)
    assert _number(hass, TOTAL) == 12500
    assert _number(hass, "sensor.north_roof_s6_gr1p5k_energy_today") == 4.321


async def test_flapping_gateway_still_returns_the_mock(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """A 502 on every other attempt is absorbed by the retry."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.mode = "flap"
    with patch(_SLEEP, new=AsyncMock()):
        await _refresh(hass, entry)
    assert _number(hass, POWER) == 1250
    assert _number(hass, TOTAL) == 12500
    flows = hass.config_entries.flow.async_progress()
    assert not any(flow["context"].get("source") == "reauth" for flow in flows)


async def test_restart_during_outage_restores_energy(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """A reload while the cloud is down comes back with the last energy total."""
    state, url = solis_server
    entry = await _setup(hass, url)
    state.mode = "server_error"
    with patch(_SLEEP, new=AsyncMock()):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert _number(hass, TOTAL) == 12500
        assert _number(hass, POWER) == 1250
        await _refresh(hass, entry)
        await _refresh(hass, entry)
    assert _state(hass, POWER) == "unavailable"
    assert _number(hass, TOTAL) == 12500
    assert any(
        issue_id.startswith("cloud_outage") for (_domain, issue_id) in ir.async_get(hass).issues
    )


async def test_removed_hybrid_drops_the_charging_sensor(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """The charging sensor uses a new unique id and leaves with its inverter."""
    state, url = solis_server
    entry = await _setup(hass, url)
    registry = er.async_get(hass)
    charging = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{HYBRID_SERIAL}_battery_charging"
    )
    assert charging is not None
    assert _state(hass, charging) == "off"
    state.force_charge = True
    await _refresh(hass, entry)
    assert _state(hass, charging) == "on"
    state.force_charge = False
    state.skip_serials = {HYBRID_SERIAL}
    await _refresh(hass, entry)
    assert hass.states.get(charging) is None


async def test_reconfigure_switches_host(
    hass: HomeAssistant, solis_server: tuple[MockState, str]
) -> None:
    """Reconfigure follows a new API URL, and a dead port does not reauthenticate."""
    _state, url = solis_server
    entry = await _setup(hass, url)
    other = MockState()
    other.pac_scale = 2
    application = create_app(other)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    other_url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
    try:
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "api_url": other_url,
                "station_ids": ["1900000000000001001", "1900000000000001002"],
                "scan_interval": 120,
                "request_timeout": 30,
                "endpoint_interval": 0.5,
            },
        )
        assert result["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        assert entry.data["api_url"] == other_url
        assert _number(hass, POWER) == 2500
        assert _number(hass, TOTAL) == 12500
    finally:
        await runner.cleanup()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "api_url": "http://127.0.0.1:1"}
    )
    with patch(_SLEEP, new=AsyncMock()):
        assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _number(hass, TOTAL) == 12500
    flows = hass.config_entries.flow.async_progress()
    assert not any(flow["context"].get("source") == "reauth" for flow in flows)
