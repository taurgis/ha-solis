"""Parser branches for phase count, offline, stale, and battery sign."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.solis.parser import (
    extract_records,
    page_count,
    parse_epoch,
    parse_inverter,
    parse_offset_hours,
    plant_period_start,
)


def _detail(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sn": "SN",
        "id": "1",
        "stationId": "P",
        "name": "Inv",
        "state": 1,
        "acOutputType": 0,
        "dcInputType": 0,
        "pac": 1,
        "pacStr": "kW",
        "dataTimestamp": 1_700_000_000_000,
        "uAc1": 230,
        "uAc1Str": "V",
        "uAc2": 0,
        "uAc2Str": "V",
        "uPv1": 300,
        "uPv1Str": "V",
        "iPv1": 1,
        "iPv1Str": "A",
        "pow1": 300,
        "pow1Str": "W",
    }
    base.update(overrides)
    return base


def test_single_phase_ignores_zero_extra_phases() -> None:
    """acOutputType 0 keeps one phase even when Solis still sends uAc2 as 0."""
    parsed = parse_inverter(_detail())
    assert len(parsed.phases) == 1
    assert len(parsed.strings) == 1
    assert parsed.ac_power_w == 1000


def test_offline_and_stale_conditions() -> None:
    """Offline is a fresh state 2. A two-hour-old timestamp is stale."""
    now = datetime.fromtimestamp(1_700_000_000, UTC)
    offline = parse_inverter(_detail(state=2))
    assert offline.condition(now) == "offline"
    stale = parse_inverter(_detail(dataTimestamp=1_699_000_000_000))
    assert stale.condition(now) == "stale"
    unknown = parse_inverter(_detail(state=9))
    assert unknown.condition(now) == "unknown"
    assert parse_inverter(_detail(state=3)).condition(now) == "alarm"


def test_battery_discharge_without_current_uses_fu() -> None:
    """batteryPowerFu is the discharge half when current is missing."""
    parsed = parse_inverter(
        _detail(
            productModel="2",
            batteryPower=0,
            batteryPowerStr="kW",
            batteryPowerFu=0.8,
            storageBatteryCurrent=0,
            storageBatteryCurrentStr="A",
        )
    )
    assert parsed.battery_power_w == -800
    assert parsed.has_battery


def test_epoch_seconds_and_month_boundary() -> None:
    """Second-resolution timestamps parse, and month start uses the plant offset."""
    assert parse_epoch(1_700_000_000) == datetime.fromtimestamp(1_700_000_000, UTC)
    assert parse_epoch(10) is None
    assert parse_offset_hours("UTC-05:30") == -5.5
    start = plant_period_start(0, datetime(2026, 3, 15, tzinfo=UTC), "month")
    assert start == datetime(2026, 3, 1, tzinfo=UTC)
    year = plant_period_start(0, datetime(2026, 3, 15, tzinfo=UTC), "year")
    assert year == datetime(2026, 1, 1, tzinfo=UTC)
    assert extract_records({"data": {"page": {"records": [{"sn": "A"}, "skip"]}}}) == [{"sn": "A"}]
    assert page_count({"data": {"page": {"pages": 3}}}) == 3
    assert page_count({}) is None
