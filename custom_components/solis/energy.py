"""Keep energy statistics monotonic when SolisCloud omits or zeroes a counter.

Home Assistant ``total_increasing`` adds every upward step and treats a
decrease as a new meter cycle. A null or zero sample must not be published
as a dip, or the next real reading is counted twice.
"""

from __future__ import annotations

import math

# 10 GWh on one counter. A 100 kW inverter running 20 years is about 3 GWh.
MAX_PLAUSIBLE_KWH = 10_000_000.0
# Headroom for a long Home Assistant outage, and still below a unit-scale bug
# (kWh published as MWh, or a stuck counter jumping by orders of magnitude).
MAX_JUMP_KWH = 100_000.0


def sanitize_kwh(value: float | None) -> float | None:
    """Drop non-finite, negative and absurd energy readings."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > MAX_PLAUSIBLE_KWH:
        return None
    return number


def consider_energy(
    last: float | None,
    value: float | None,
    *,
    monotonic: bool,
    period_changed: bool,
) -> float | None:
    """Return the reading to publish.

    Lifetime counters never move backwards. A daily, monthly or yearly counter
    may drop only when its plant-local period has rolled. Inside a period, a
    null, a zero or a smaller number keeps the last good reading.
    """
    value = sanitize_kwh(value)
    if period_changed and not monotonic:
        return value
    if value is None:
        return last
    if last is None:
        return value
    if value < last or value - last > MAX_JUMP_KWH:
        return last
    return value
