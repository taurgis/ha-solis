"""More unit-conversion cases, including the documented quirks."""

from __future__ import annotations

import pytest

from custom_components.solis.units import (
    apply_pec,
    as_float,
    to_identity,
    to_kilowatt_hours,
    to_watts,
    unit_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (" 3.5 ", 3.5),
        ("nope", None),
        ({"n": 1}, None),
        ("", None),
    ],
)
def test_as_float_rejects_non_numbers(value: object, expected: float | None) -> None:
    """Booleans, blank strings and other objects are missing, not zero."""
    assert as_float(value) == expected


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("kW", "kw"),
        (" kWh ", "kwh"),
        ("MWp", "mw"),
        ("", None),
        (None, None),
    ],
)
def test_unit_key(unit: object, expected: str | None) -> None:
    """Unit strings are case-insensitive, and peak suffixes fold into the base unit."""
    assert unit_key(unit) == expected


def test_pec_and_refused_units() -> None:
    """A pec of 1 is a no-op, and an energy unit on a power field is dropped."""
    assert apply_pec(10, None) == 10
    assert apply_pec(10, "1") == 10
    assert apply_pec(10, "0.1") == 1
    assert to_watts(3, "kWh") is None
    assert to_watts(2, "kW", pec="2") == 4000
    assert to_kilowatt_hours(2, "Wh") == 0.002
    assert to_kilowatt_hours(2, "MW") == 2_000
    assert to_kilowatt_hours(1, "GW") == 1_000_000
    assert to_kilowatt_hours(1, "not-a-unit") is None
    assert to_identity(50, "Hz", default="hz") == 50
    assert to_identity(50, "kW", default="hz") is None
    assert to_identity(10, "%", pec="2", default="%") == 20
