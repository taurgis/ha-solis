"""Convert SolisCloud numbers using the companion unit string.

SolisCloud Platform API V2.0 says every power and energy figure is meaningless
without its unit. The platform also ships samples where an energy field is
labelled ``kW`` and a current field is misspelled ``bstteryCurrent``. Missing
unit strings fall back to the unit the V2.0 field tables use for that family.
"""

from __future__ import annotations

from typing import Any

_POWER_TO_W = {"w": 1.0, "kw": 1_000.0, "mw": 1_000_000.0}
_ENERGY_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1_000.0, "gwh": 1_000_000.0}
_IDENTITY = {"v": 1.0, "a": 1.0, "hz": 1.0, "c": 1.0, "%": 1.0, "h": 1.0}

_MISSING_STRINGS = frozenset({"", "null", "none", "Null", "None"})


def is_missing(value: Any) -> bool:
    """Return whether Solis sent an empty or literal-null field."""
    if value is None:
        return True
    return isinstance(value, str) and value in _MISSING_STRINGS


def as_float(value: Any) -> float | None:
    """Parse a Solis number, treating null-like tokens as absent."""
    if is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def unit_key(value: Any) -> str | None:
    """Normalize ``kW``, ``kWh`` and ``UTC+08:00`` style tokens."""
    if is_missing(value):
        return None
    text = str(value).strip().lower().replace(" ", "")
    if text.endswith("p") and text[:-1] in {"w", "kw", "mw"}:
        text = text[:-1]
    return text or None


def apply_pec(number: float, pec: Any) -> float:
    """Apply a Solis ``*Pec`` multiplier when it is present and not 1."""
    factor = as_float(pec)
    if factor is None or factor == 1:
        return number
    return number * factor


def _convert(
    number: float, unit: str | None, table: dict[str, float], default: str
) -> float | None:
    key = unit or default
    factor = table.get(key)
    if factor is None:
        return None
    return number * factor


def to_watts(value: Any, unit: Any, pec: Any = None, *, default: str = "kw") -> float | None:
    """Return watts. An energy unit on a power field is refused."""
    number = as_float(value)
    if number is None:
        return None
    number = apply_pec(number, pec)
    return _convert(number, unit_key(unit), _POWER_TO_W, default)


def to_kilowatt_hours(
    value: Any,
    unit: Any,
    pec: Any = None,
    *,
    default: str = "kwh",
) -> float | None:
    """Return kWh. Negative totals are dropped so they cannot poison statistics.

    The V2.0 station sample labels ``generatorTodayEnergyStr`` as ``kW``. When
    the caller asked for energy and the only unit on the field is ``kW``, treat
    it as ``kWh`` rather than dropping the reading.
    """
    number = as_float(value)
    if number is None:
        return None
    number = apply_pec(number, pec)
    key = unit_key(unit) or default
    if key == "kw":
        key = "kwh"
    converted = _convert(number, key, _ENERGY_TO_KWH, default)
    if converted is None or converted < 0:
        return None
    return converted


def to_identity(value: Any, unit: Any, pec: Any = None, *, default: str) -> float | None:
    """Return a value whose unit is V, A, Hz, °C or percent."""
    number = as_float(value)
    if number is None:
        return None
    number = apply_pec(number, pec)
    converted = _convert(number, unit_key(unit), _IDENTITY, default)
    if converted is None:
        return None
    return converted
