"""Parser behaviour for the documented Solis quirks."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.solis.parser import (
    parse_epoch,
    parse_inverter,
    parse_offset_hours,
    plant_period_start,
)
from mock_soliscloud.catalog import HYBRID_SERIAL, inverter_details


def test_hybrid_battery_sign_and_strings() -> None:
    """Discharge current makes battery power negative, and dcInputType is count-1."""
    detail = inverter_details()[HYBRID_SERIAL]
    detail["dataTimestamp"] = 1_700_000_000_000
    parsed = parse_inverter(detail)
    assert parsed.battery_power_w == -1200
    assert parsed.battery_soc == 72
    assert len(parsed.strings) == 2
    assert len(parsed.phases) == 3
    assert parsed.condition(datetime.fromtimestamp(1_700_000_000, UTC)) == "online"


def test_epoch_and_timezone_offset() -> None:
    """Epoch milliseconds parse, and the numeric plant offset ignores DST."""
    assert parse_epoch(1_687_846_773_000) == datetime.fromtimestamp(1_687_846_773, UTC)
    assert parse_epoch("nope") is None
    assert parse_offset_hours("UTC+08:00") == 8
    assert parse_offset_hours(-5) == -5
    now = datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    midnight = plant_period_start(8, now, "day")
    assert midnight == datetime(2026, 6, 1, 16, 0, tzinfo=UTC)


def test_null_fields_stay_absent() -> None:
    """Null power is not coerced to zero."""
    parsed = parse_inverter(
        {
            "sn": "SN",
            "id": "1",
            "state": 1,
            "pac": None,
            "pacStr": "kW",
            "eTotal": "null",
            "dataTimestamp": 1_700_000_000_000,
        }
    )
    assert parsed.ac_power_w is None
    assert parsed.energy_total_kwh is None
