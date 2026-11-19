"""What an inverter's live readings show while it is off.

SolisCloud still answers for an inverter that switched off for the night, so
the entities stay available. What the inverter produces is known to be zero;
what it only measures (the grid, the house, the panels' voltage) carries on
without it and becomes unknown. A failed or stale read is unavailable.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .test_fix_runtime_entities import (
    SERIAL,
    _inverter,
    _local,
    _local_entry,
    _setup,
    _Transport,
)
from custom_components.solis.const import DOMAIN
from custom_components.solis.models import InverterSnapshot, Phase, PvString
from custom_components.solis.sensor import _inverter_value, _phase_value, _string_value

PRODUCED = ("ac_power", "battery_power", "battery_current", "backup_power", "smart_load_power")
MEASURED = (
    "frequency",
    "temperature",
    "grid_power",
    "consumption_power",
    "battery_voltage",
)
TOTALS = ("energy_today", "energy_total")


def _live() -> InverterSnapshot:
    return _inverter(
        battery_power_w=-400.0,
        battery_current_a=-8.0,
        battery_voltage_v=51.2,
        grid_power_w=-300.0,
        consumption_power_w=900.0,
        backup_power_w=120.0,
        smart_load_power_w=60.0,
        strings=(PvString(index=1, voltage_v=320.0, current_a=4.0, power_w=1280.0),),
        phases=(Phase(index=1, voltage_v=230.0, current_a=5.0, power_w=1150.0),),
    )


@pytest.mark.parametrize("age", [timedelta(minutes=1), timedelta(days=2)])
def test_an_offline_inverter_produces_zero_and_measures_nothing(age: timedelta) -> None:
    """Offline holds however old the last sample is: the inverter is off, not lost."""
    now = datetime.now(UTC)
    live = _live()
    offline = replace(live, state=2, data_timestamp=now - age)
    assert offline.condition(now) == "offline"
    for key in PRODUCED:
        assert _inverter_value(offline, key, now) == 0.0, key
    for key in MEASURED:
        assert _inverter_value(offline, key, now) is None, key
    for key in TOTALS:
        assert _inverter_value(offline, key, now) == _inverter_value(live, key, now), key
    assert _string_value(offline, "string_power", 1, now) == 0.0
    assert _string_value(offline, "string_current", 1, now) == 0.0
    assert _string_value(offline, "string_voltage", 1, now) is None
    assert _phase_value(offline, "phase_current", 1, now) == 0.0
    assert _phase_value(offline, "phase_voltage", 1, now) is None
    assert _phase_value(offline, "phase_power", 1, now) is None


def test_a_stale_or_unreachable_read_hides_everything_live() -> None:
    """An old sample that is not offline, or a failed read, is not a zero."""
    now = datetime.now(UTC)
    live = _live()
    for inverter in (
        replace(live, data_timestamp=now - timedelta(days=1)),
        replace(live, state=2, unreachable=True),
    ):
        assert inverter.condition(now) == "stale"
        for key in (*PRODUCED, *MEASURED):
            assert _inverter_value(inverter, key, now) is None, key
        assert _string_value(inverter, "string_power", 1, now) is None
        assert _inverter_value(inverter, "energy_total", now) == 21970.0


def test_a_live_inverter_shows_its_readings() -> None:
    now = datetime.now(UTC)
    live = _live()
    assert _inverter_value(live, "ac_power", now) == 1200.0
    assert _inverter_value(live, "consumption_power", now) == 900.0
    assert _string_value(live, "string_voltage", 1, now) == 320.0


async def test_battery_charging_turns_off_while_offline(hass: HomeAssistant) -> None:
    """A charging reading from before the inverter went off is not current."""
    now = datetime.now(UTC)
    charging = _local(battery_power_w=500.0, battery_soc=80.0)
    offline = _local(
        battery_power_w=500.0, battery_soc=80.0, state=2, data_timestamp=now - timedelta(hours=6)
    )
    entry = _local_entry(hass)
    await _setup(hass, entry, _Transport(charging, offline))
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{SERIAL}_battery_charging"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "on"  # type: ignore[union-attr]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"  # type: ignore[union-attr]
    await hass.config_entries.async_unload(entry.entry_id)
