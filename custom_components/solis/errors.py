"""Errors raised by the SolisCloud client.

These stay free of Home Assistant imports so the client and the mock can
share them.
"""

from __future__ import annotations

from datetime import datetime


class SolisCloudError(Exception):
    """Base error for a SolisCloud call that did not yield data."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Store a human-readable message and the optional API code."""
        super().__init__(message)
        self.code = code


class SolisAuthError(SolisCloudError):
    """Key, secret, or signature was rejected."""


class SolisClockError(SolisCloudError):
    """The Date header was outside the platform's acceptance window."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        server_date: datetime | None = None,
    ) -> None:
        """Store the platform clock when the response carried a Date header."""
        super().__init__(message, code=code)
        self.server_date = server_date


class SolisRateLimitError(SolisCloudError):
    """The platform asked us to slow down."""

    def __init__(self, message: str, *, retry_after: float, code: str | None = None) -> None:
        """Store how long to wait before the next attempt."""
        super().__init__(message, code=code)
        self.retry_after = retry_after


class SolisServerError(SolisCloudError):
    """HTTP 5xx or an equivalent gateway failure."""


class SolisTimeoutError(SolisCloudError):
    """The platform did not answer before the configured timeout."""


class SolisDataError(SolisCloudError):
    """The platform returned its 'abnormal data' failure (often code ``1``)."""
