"""HMAC-SHA1 Content-MD5 request signing for SolisCloud Platform API v2.

The string-to-sign is the one in SolisCloud Platform API Document V2.0
section 2.2. The ``API {key}:{signature}`` spacing matches the client that
has been working in hultenvp/solis-sensor (Apache-2.0). Weekday and month
names are written in English so a non-C locale cannot change the signature.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac

CONTENT_TYPE = "application/json"
VERB = "POST"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def content_md5(body: bytes) -> str:
    """Return the Base64 MD5 of the exact request body."""
    return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")


def format_http_date(moment: datetime) -> str:
    """Return a GMT date in the shape ``Fri, 26 Jul 2019 06:00:46 GMT``."""
    moment = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return (
        f"{_WEEKDAYS[moment.weekday()]}, {moment.day:02d} {_MONTHS[moment.month - 1]} "
        f"{moment.year:04d} {moment.strftime('%H:%M:%S')} GMT"
    )


def string_to_sign(*, content_md5_value: str, date_value: str, path: str) -> str:
    """Build the canonical string Solis hashes."""
    return "\n".join((VERB, content_md5_value, CONTENT_TYPE, date_value, path))


def sign(*, secret: str, content_md5_value: str, date_value: str, path: str) -> str:
    """Return the Base64 HMAC-SHA1 signature."""
    payload = string_to_sign(
        content_md5_value=content_md5_value,
        date_value=date_value,
        path=path,
    )
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def authorization_value(key_id: str, signature: str) -> str:
    """Return the Authorization header value."""
    return f"API {key_id}:{signature}"


def signed_headers(
    *,
    key_id: str,
    secret: str,
    path: str,
    body: bytes,
    moment: datetime,
) -> dict[str, str]:
    """Return the four headers SolisCloud requires."""
    md5_value = content_md5(body)
    date_value = format_http_date(moment)
    signature = sign(
        secret=secret,
        content_md5_value=md5_value,
        date_value=date_value,
        path=path,
    )
    return {
        "Content-MD5": md5_value,
        "Content-Type": CONTENT_TYPE,
        "Date": date_value,
        "Authorization": authorization_value(key_id, signature),
    }


def signature_matches(
    *,
    secret: str,
    key_id: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
) -> bool:
    """Return whether the headers sign ``body`` for this key."""
    provided_md5 = headers.get("Content-MD5", "")
    date_value = headers.get("Date", "")
    provided_auth = headers.get("Authorization", "")
    if not provided_md5 or not date_value or not provided_auth:
        return False
    if not hmac.compare_digest(provided_md5, content_md5(body)):
        return False
    expected = authorization_value(
        key_id,
        sign(
            secret=secret,
            content_md5_value=provided_md5,
            date_value=date_value,
            path=path,
        ),
    )
    return hmac.compare_digest(provided_auth, expected)
