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
    """HTTP 5xx, an HTML error page, or an equivalent gateway failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store a Retry-After hint when the gateway sent one."""
        super().__init__(message, code=code)
        self.retry_after = retry_after


class SolisTimeoutError(SolisCloudError):
    """The platform did not answer before the configured timeout."""


class SolisLoggerOffline(SolisCloudError):
    """The logger did not answer. At night the inverter powers the stick off.

    ``connected`` is true when TCP opened and the read then timed out or the
    peer closed it. ``refused`` is true when the port rejected the handshake.
    A reset after a successful handshake is ``SolisLoggerBusy``, not this.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        connected: bool = False,
        refused: bool = False,
        reason: str = "",
    ) -> None:
        """Record whether a socket was established before the failure."""
        super().__init__(message, code=code)
        self.connected = connected
        self.refused = refused
        self.reason = reason


class SolisLoggerBusy(SolisCloudError):
    """TCP connected and the logger reset it, or the port refused new sessions.

    The inverter can still be producing and SolisCloud can still be online.
    ``waiting`` means a backoff is already running, so the caller must not
    open another socket or extend the pause.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        waiting: bool = False,
        reason: str = "locked",
    ) -> None:
        """Record whether this is a new reset or the quiet period after one."""
        super().__init__(message, code=code)
        self.waiting = waiting
        self.reason = reason


def logger_failure_text(host: str, err: BaseException) -> str:
    """Return the sentence a manual read should print for this failure."""
    reason = str(getattr(err, "reason", "") or "")
    if reason == "tcp_disabled":
        return (
            f"{host} refused port 502. Modbus TCP on the S2-WL-ST starts with "
            "firmware 100101B4. Some later updates close the port again. On older "
            "firmware, enabling TCP disconnects SolisCloud."
        )
    if reason == "silent":
        return (
            f"{host} accepted TCP and sent no Modbus reply. The stick often allows "
            "one local client. On older firmware that client is SolisCloud or "
            "Home Assistant, not both. Firmware 100121cf, 100121d0 and the "
            "100141dx releases are the ones reported to allow both."
        )
    if reason == "gateway_down":
        return (
            f"{host} is not accepting TCP. Check the gateway address and port. "
            "A gateway that stays powered does not disappear with the inverter."
        )
    if reason == "wrong_protocol":
        return (
            f"{host} answered, but the bytes were not the framing selected for "
            "this logger. A transparent serial tunnel needs Modbus RTU over TCP. "
            "A gateway in Modbus TCP mode needs the Modbus TCP gateway option."
        )
    if reason == "wrong_slave":
        return f"{host} replied from a different Modbus slave than the one requested."
    if reason == "unsupported_map":
        return f"{host} speaks Modbus and does not serve the Solis 3000 or 33000 register map."
    if isinstance(err, SolisLoggerBusy):
        return (
            f"{host} reset or refused the local session. The inverter can still "
            "be producing, and SolisCloud can still be online. Wait at least 10 "
            "seconds before trying again; the pause grows up to about 10 minutes. "
            "Do not read registers at 3050 or above, or the 33000 hybrid range, "
            "once the 3000-range map is known."
        )
    return (
        f"{host} did not answer (no route or the connection timed out). At night "
        "the inverter powers the stick off. Try again while it is producing."
    )


class SolisDataError(SolisCloudError):
    """The platform returned its 'abnormal data' failure (often code ``1``)."""
