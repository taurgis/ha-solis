"""Energy holds and daylight-saving period boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.solis.energy import consider_energy
from custom_components.solis.parser import local_date, plant_period_start, resolve_zone


def test_lifetime_total_ignores_null_zero_and_dips() -> None:
    """A null or a smaller lifetime total keeps the last published reading."""
    assert consider_energy(None, 12.5, monotonic=True, period_changed=False) == 12.5
    assert consider_energy(12.5, None, monotonic=True, period_changed=False) == 12.5
    assert consider_energy(12.5, 0, monotonic=True, period_changed=False) == 12.5
    assert consider_energy(12.5, 12.4, monotonic=True, period_changed=False) == 12.5
    assert consider_energy(12.5, 12.6, monotonic=True, period_changed=False) == 12.6
    assert consider_energy(12.5, -1, monotonic=True, period_changed=False) == 12.5
    assert consider_energy(10, 200_000, monotonic=True, period_changed=False) == 10


def test_daily_counter_drops_only_on_a_new_period() -> None:
    """Inside one plant day a zero is a glitch. Across midnight it is a reset."""
    assert consider_energy(4.3, None, monotonic=False, period_changed=False) == 4.3
    assert consider_energy(4.3, 0, monotonic=False, period_changed=False) == 4.3
    assert consider_energy(4.3, 0.1, monotonic=False, period_changed=True) == 0.1
    assert consider_energy(4.3, None, monotonic=False, period_changed=True) is None


def test_london_summer_uses_home_assistant_not_the_winter_offset() -> None:
    """Issue 533: a fixed UTC offset rolls 'today' at 23:00 during British Summer Time."""
    now = datetime(2026, 7, 15, 23, 30, tzinfo=UTC)
    zone = resolve_zone(0.0, "", "Europe/London", now)
    assert zone is not None
    assert local_date(0.0, now, zone) == "2026-07-16"
    assert local_date(0.0, now, None) == "2026-07-15"
    assert plant_period_start(0.0, now, "day", zone) == datetime(2026, 7, 15, 23, 0, tzinfo=UTC)


def test_distant_plant_keeps_the_solis_offset() -> None:
    """A plant hours away from Home Assistant is not pulled into the house zone."""
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert resolve_zone(8.0, "", "America/New_York", now) is None
    assert resolve_zone(0.0, "plant", "Europe/London", now) is None
    forced = resolve_zone(8.0, "Europe/London", "America/New_York", now)
    assert forced is not None
    assert str(forced) == "Europe/London"


def test_invalid_zone_name_is_ignored() -> None:
    """A stored name that is not a timezone falls back."""
    now = datetime(2026, 1, 2, tzinfo=UTC)
    assert resolve_zone(8.0, "Not/AZone", None, now) is None
