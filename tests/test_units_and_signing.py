"""Unit conversion and request signing."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_components.solis.signing import (
    content_md5,
    signature_matches,
    signed_headers,
)
from custom_components.solis.units import to_identity, to_kilowatt_hours, to_watts


def test_power_and_energy_units() -> None:
    """Scale the unit strings Solis actually sends."""
    assert to_watts(1.25, "kW") == 1250
    assert to_watts(1500, "W") == 1500
    assert to_watts(1.25, None) == 1250
    assert to_watts(2, "kW", "0.5") == 1000
    assert to_watts("null", "kW") is None
    assert to_watts(5, "kWp") == 5000
    assert to_kilowatt_hours(12.5, "MWh") == 12500
    assert to_kilowatt_hours(1.5, "GWh") == 1_500_000
    assert to_kilowatt_hours(4.321, "kWh") == 4.321
    assert to_kilowatt_hours(4, "kW") == 4
    assert to_kilowatt_hours(-3, "kWh") is None
    assert to_identity(230.5, "V", default="v") == 230.5
    assert to_identity(None, "A", default="a") is None


def test_signature_uses_exact_body_bytes() -> None:
    """Content-MD5 and the HMAC cover the body that will be sent."""
    moment = datetime(2019, 7, 26, 6, 0, 46, tzinfo=UTC)
    body = b'{"pageNo":"1","pageSize":"10"}'
    headers = signed_headers(
        key_id="key",
        secret="secret",
        path="/v1/api/userStationList",
        body=body,
        moment=moment,
    )
    assert headers["Date"] == "Fri, 26 Jul 2019 06:00:46 GMT"
    assert headers["Content-MD5"] == content_md5(body)
    assert headers["Content-Type"] == "application/json"
    assert headers["Authorization"].startswith("API key:")
    assert signature_matches(
        secret="secret",
        key_id="key",
        path="/v1/api/userStationList",
        body=body,
        headers=headers,
    )
    assert not signature_matches(
        secret="secret",
        key_id="key",
        path="/v1/api/userStationList",
        body=body + b" ",
        headers=headers,
    )
