"""Assertions added after a mutation audit: they pin behaviour that no test asserted."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant

from .test_coordinator import _entry, _issue_keys, _Scripted, _snapshot
from .test_web_stick import _cloud_snapshot
from custom_components.solis import local_transport as lt
from custom_components.solis.cloud_fill import fill_from_cloud
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.energy import consider_energy, sanitize_kwh
from custom_components.solis.errors import SolisServerError, SolisTimeoutError
from custom_components.solis.loggers.web import parse_inverter as parse_page
from custom_components.solis.parser import _plausible
from custom_components.solis.registers import decode_hybrid, decode_string
from custom_components.solis.units import to_watts

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _web_and_cloud() -> tuple[Any, Any]:
    cloud = _cloud_snapshot()
    donor = replace(
        cloud.inverters[0],
        state=1,
        hmi_version="CLOUD",
        battery_soc=50.0,
        grid_power_w=100.0,
        unreachable=True,
    )
    cloud = replace(
        cloud,
        inverters=(donor,),
        alarms=(object(),),  # type: ignore[arg-type]
        epms=(object(),),  # type: ignore[arg-type]
        collectors=(object(),),  # type: ignore[arg-type]
        stations=(object(),),  # type: ignore[arg-type]
        partial=False,
    )
    page = replace(donor, state=3, hmi_version="PAGE", unreachable=False)
    web = replace(cloud, inverters=(page,), alarms=(), epms=(), collectors=(), stations=())
    return web, cloud


def test_cloud_fill_keeps_page_values_and_drops_stale_live_data() -> None:
    """The page's state and version win. Old cloud data adds names and totals only."""
    web, cloud = _web_and_cloud()
    serial = web.inverters[0].serial
    stale = fill_from_cloud(web, cloud, serial=serial, fresh=False)
    inverter = stale.inverters[0]
    assert inverter.state == 3 and inverter.hmi_version == "PAGE"
    assert inverter.unreachable is False
    assert inverter.battery_soc is None and inverter.grid_power_w is None
    assert stale.alarms == () and stale.epms == ()
    assert stale.partial is True
    live = fill_from_cloud(web, cloud, serial=serial, fresh=True)
    assert live.inverters[0].state == 3 and live.inverters[0].battery_soc == 50.0
    assert live.alarms == cloud.alarms and live.epms == cloud.epms
    assert live.collectors == cloud.collectors and live.stations == cloud.stations
    assert live.partial is False


def test_the_hybrid_map_scales() -> None:
    """Voltage, today's energy, power and totals each use their own scale."""
    words = {
        33079: 0,
        33080: 4200,
        33035: 123,
        33133: 5120,
        33139: 80,
        33140: 99,
        33029: 0,
        33030: 21970,
    }
    parsed = decode_hybrid(words, serial="X", logger_serial="Y", now=NOW)
    assert parsed.ac_power_w == 4200
    assert parsed.energy_today_kwh == pytest.approx(12.3)
    assert parsed.battery_voltage_v == pytest.approx(512.0)
    assert parsed.battery_soc == 80 and parsed.battery_soh == 99
    assert parsed.energy_total_kwh == 21970


def test_the_string_map_with_production() -> None:
    """A producing single-phase inverter. 3003 = 1 with an idle PV2 is one input."""
    words = {
        3002: 0,
        3003: 1,
        3004: 0,
        3005: 1500,
        3014: 59,
        3021: 3000,
        3022: 0,
    }
    parsed = decode_string(words, serial="X", logger_serial="Y", now=NOW)
    assert parsed.energy_today_kwh == pytest.approx(5.9)
    assert parsed.ac_output_type == 0
    assert len(parsed.strings) == 1
    assert parsed.strings[0].power_w == 0.0


def test_plausibility_bounds_are_inclusive() -> None:
    """A full battery, 70 Hz and 150 °C are real readings."""
    base = _cloud_snapshot().inverters[0]
    top = _plausible(
        replace(
            base,
            battery_soc=100.0,
            battery_soh=100.0,
            force_charge_soc=100.0,
            force_discharge_soc=0.0,
            frequency_hz=70.0,
            temperature_c=150.0,
        )
    )
    assert (top.battery_soc, top.battery_soh, top.force_charge_soc) == (100.0, 100.0, 100.0)
    assert (top.force_discharge_soc, top.frequency_hz, top.temperature_c) == (0.0, 70.0, 150.0)
    low = _plausible(replace(base, frequency_hz=40.0, temperature_c=-40.0))
    assert (low.frequency_hz, low.temperature_c) == (40.0, -40.0)
    out = _plausible(replace(base, frequency_hz=39.9, battery_soc=100.1, temperature_c=150.1))
    assert (out.frequency_hz, out.battery_soc, out.temperature_c) == (None, None, None)


def test_night_and_degraded_snapshots_keep_the_devices() -> None:
    """Both are partial, so no device is pruned every night."""
    cloud = _cloud_snapshot()
    inverter = replace(cloud.inverters[0], grid_power_w=None, battery_power_w=5.0)
    snapshot = replace(cloud, inverters=(inverter,))
    asleep = lt.asleep_snapshot(snapshot)
    assert asleep.partial is True and asleep.source == "asleep"
    assert asleep.inverters[0].state == 2 and asleep.inverters[0].data_source == "asleep"
    assert asleep.inverters[0].grid_power_w is None
    assert asleep.inverters[0].battery_power_w == 0.0
    degraded = lt._mark_degraded(snapshot)
    assert degraded.partial is True and degraded.source == "degraded"


def test_energy_bounds_are_exact() -> None:
    """The ceiling and the jump limit are inclusive, and a bool is no reading."""
    assert sanitize_kwh(10_000_000.0) == 10_000_000.0
    assert sanitize_kwh(10_000_000.01) is None
    assert sanitize_kwh(True) is None
    assert consider_energy(10, 100_010, monotonic=True, period_changed=False) == 100_010
    assert consider_energy(10, 100_011, monotonic=True, period_changed=False) == 10
    assert consider_energy(100, 5, monotonic=False, period_changed=True) == 5
    assert consider_energy(100, 5, monotonic=True, period_changed=True) == 100


def test_page_parser_edges() -> None:
    """Four fields is too few, and a lowercase alarm word still means no alarm."""
    with pytest.raises(ValueError):
        parse_page("a;b;c;1")
    assert not parse_page("a;b;c;1;2;3;4").in_alarm
    assert not parse_page("a;b;c;1;2;3;4;no;").in_alarm
    assert parse_page("a;b;c;1;2;3;4;E01;").in_alarm


def test_units_and_backoff_caps() -> None:
    """Megawatts, and the cap holds after the jitter."""
    assert to_watts(2, "MW") == 2_000_000
    with patch("custom_components.solis.local_transport.random.uniform", return_value=1.1):
        assert lt.backoff_seconds(10) == 600
        assert lt.backoff_seconds(0) == pytest.approx(11)
        assert lt.backoff_seconds(1) == pytest.approx(22)
        assert lt.backoff_seconds(2) == pytest.approx(44)


def test_donor_freshness_reads_a_naive_time_as_utc() -> None:
    """A naive timestamp is UTC, not local time."""
    cloud = _cloud_snapshot()
    recent = replace(cloud.inverters[0], data_timestamp=datetime.now(UTC).replace(tzinfo=None))
    old = replace(
        cloud.inverters[0],
        data_timestamp=(datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None),
    )
    assert lt._donor_is_fresh(replace(cloud, inverters=(recent,)), recent) is True
    assert lt._donor_is_fresh(replace(cloud, inverters=(old,)), old) is False


async def test_an_old_stored_snapshot_is_not_served_as_live(hass: HomeAssistant) -> None:
    """After a restart a snapshot older than 15 minutes hides live readings at once."""
    entry = _entry(hass)
    old = replace(_snapshot(), fetched_at=datetime.now(UTC) - timedelta(minutes=20))
    old = replace(old, inverters=(_cloud_snapshot().inverters[0],))
    coordinator = SolisCoordinator(hass, entry, _Scripted([SolisTimeoutError("slow")]))
    coordinator._last_good = old
    held = await coordinator._async_update_data()
    assert held.inverters[0].unreachable is True
    assert held.partial is True
    assert not any(key.startswith("cloud_outage") for key in _issue_keys(hass))


async def test_cloud_failures_back_off_and_a_good_poll_resets(hass: HomeAssistant) -> None:
    """The interval doubles from a minute and is capped, then returns to the base."""
    entry = _entry(hass, 30)
    good = _snapshot()
    failures = [SolisServerError("500") for _ in range(6)]
    coordinator = SolisCoordinator(hass, entry, _Scripted([good, *failures, good]))
    coordinator._last_good = None
    await coordinator._async_update_data()
    seen = []
    with patch("custom_components.solis.coordinator.random.uniform", return_value=1.0):
        for _ in failures:
            await coordinator._async_update_data()
            assert coordinator.update_interval is not None
            seen.append(coordinator.update_interval.total_seconds())
        await coordinator._async_update_data()
    assert seen[:4] == [60.0, 120.0, 240.0, 480.0]
    assert max(seen) <= 900.0
    assert coordinator.update_interval == coordinator._base_interval
