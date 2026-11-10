"""Async SolisCloud Platform API v2 client.

Uses the Home Assistant aiohttp session when called from the integration.
The body bytes that are signed are the bytes that are sent. aiohttp's ``json=``
encoder inserts spaces, which would make Content-MD5 disagree with the body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisRateLimitError,
    SolisServerError,
    SolisTimeoutError,
)
from .parser import extract_records, page_count
from .signing import signed_headers

_AUTH_CODES = {"R0000", "I0012", "B0011"}
_MAX_PAGES = 20
_PAGE_SIZE = 100


class _EndpointLimiter:
    """Hold each path to one in-flight call and a minimum gap afterwards."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_at: dict[str, float] = {}

    async def wait(self, path: str) -> None:
        """Sleep until this path may be called again."""
        if self._interval <= 0:
            return
        lock = self._locks.setdefault(path, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            delay = self._next_at.get(path, 0.0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at[path] = loop.time() + self._interval


class SolisCloudClient:
    """Minimal SolisCloud v2 reader."""

    def __init__(
        self,
        session: ClientSession,
        *,
        base_url: str,
        key_id: str,
        secret: str,
        timeout: float = 30.0,
        endpoint_interval: float = 2.0,
        attempts: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Store credentials. ``endpoint_interval`` is seconds per path."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._key_id = key_id
        self._secret = secret
        self._timeout = timeout
        self._attempts = attempts
        self._clock = clock or (lambda: datetime.now(UTC))
        self._limiter = _EndpointLimiter(endpoint_interval)
        self._clock_offset = timedelta(0)

    async def user_station_list(self) -> list[dict[str, Any]]:
        """Return every station on the account."""
        return await self._collect("/v1/api/userStationList", {})

    async def inverter_list(self, station_id: str) -> list[dict[str, Any]]:
        """Return inverters for one station."""
        return await self._collect("/v1/api/inverterList", {"stationId": station_id})

    async def inverter_detail(self, device_id: str, serial: str) -> dict[str, Any]:
        """Return one inverter detail object."""
        payload = await self.post("/v1/api/inverterDetail", {"id": device_id, "sn": serial})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SolisCloudError("inverterDetail returned no data object")
        return data

    async def station_detail(self, station_id: str) -> dict[str, Any]:
        """Return one station detail object."""
        payload = await self.post("/v1/api/stationDetail", {"id": station_id})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SolisCloudError("stationDetail returned no data object")
        return data

    async def station_day_energy(self, station_id: str, day: str) -> dict[str, Any] | None:
        """Return the day-energy row for ``day`` (``YYYY-MM-DD``), if any."""
        rows = await self._collect(
            "/v1/api/stationDayEnergyList",
            {"time": day, "stationIds": station_id},
        )
        exact = [
            row
            for row in rows
            if station_id in {str(row.get("id") or ""), str(row.get("stationId") or "")}
        ]
        if exact:
            return exact[0]
        # A single unlabeled row is the station we asked for. A list that
        # starts with a blank id must not steal another plant's energy.
        if len(rows) == 1:
            return rows[0]
        return None

    async def alarm_list(self) -> list[dict[str, Any]]:
        """Return alarm rows for the account."""
        return await self._collect("/v1/api/alarmList", {})

    async def collector_list(self) -> list[dict[str, Any]]:
        """Return data-logger rows for the account."""
        return await self._collect("/v1/api/collectorList", {})

    async def epm_list(self) -> list[dict[str, Any]]:
        """Return export-power-manager rows for the account."""
        return await self._collect("/v1/api/epmList", {})

    async def _collect(self, path: str, extra: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            body = {"pageNo": str(page), "pageSize": str(_PAGE_SIZE), **extra}
            payload = await self.post(path, body)
            batch = extract_records(payload)
            rows.extend(batch)
            reported = page_count(payload)
            if reported is not None and page >= reported:
                break
            if len(batch) < _PAGE_SIZE:
                break
        return rows

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``path`` and return a successful JSON object.

        HTTP 429, timeouts and 5xx are retried. A clock rejection is retried
        once against the Date header the platform sent. Other 4xx responses
        are not retried.
        """
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        delay = 1.0
        attempt = 0
        clock_retried = False
        while attempt < self._attempts:
            await self._limiter.wait(path)
            try:
                return await self._post_once(path, body)
            except SolisClockError as err:
                if clock_retried or err.server_date is None:
                    raise
                self._align_clock(err.server_date)
                clock_retried = True
            except SolisRateLimitError as err:
                attempt += 1
                if attempt >= self._attempts:
                    raise
                await asyncio.sleep(err.retry_after)
            except (SolisServerError, SolisTimeoutError):
                attempt += 1
                if attempt >= self._attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise SolisCloudError(f"{path} failed")

    def _align_clock(self, server_date: datetime) -> None:
        """Sign later requests with the platform's clock."""
        server = server_date if server_date.tzinfo else server_date.replace(tzinfo=UTC)
        self._clock_offset = server - self._clock()

    async def _post_once(self, path: str, body: bytes) -> dict[str, Any]:
        headers = signed_headers(
            key_id=self._key_id,
            secret=self._secret,
            path=path,
            body=body,
            moment=self._clock() + self._clock_offset,
        )
        timeout = ClientTimeout(total=self._timeout)
        try:
            async with self._session.post(
                f"{self._base_url}{path}",
                data=body,
                headers=headers,
                timeout=timeout,
            ) as response:
                raw = await response.read()
                status = response.status
                retry_after = _retry_after(response.headers.get("Retry-After"), now=self._clock())
                server_date = _http_date(response.headers.get("Date"))
        except TimeoutError as err:
            raise SolisTimeoutError(f"{path} timed out after {self._timeout}s") from err
        except ClientError as err:
            raise SolisServerError(f"{path} connection failed: {err}") from err
        return _interpret(path, status, raw, retry_after, server_date)


def _retry_after(header: str | None, *, now: datetime | None = None) -> float:
    if header is None:
        return 2.0
    try:
        return max(0.0, float(header))
    except ValueError:
        parsed = _http_date(header)
        if parsed is None or now is None:
            return 2.0
        current = now if now.tzinfo else now.replace(tzinfo=UTC)
        return max(0.0, (parsed - current).total_seconds())


def _http_date(header: str | None) -> datetime | None:
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _interpret(
    path: str,
    status: int,
    raw: bytes,
    retry_after: float,
    server_date: datetime | None,
) -> dict[str, Any]:
    if status == 429:
        raise SolisRateLimitError(f"{path} rate limited", retry_after=retry_after or 2.0)
    if status == 408:
        raise SolisClockError(
            f"{path} rejected the Date header",
            code="408",
            server_date=server_date,
        )
    if status in {401, 403}:
        raise SolisAuthError(f"{path} rejected the API key", code=str(status))
    if status >= 500:
        raise SolisServerError(f"{path} returned HTTP {status}", code=str(status))
    if status != 200:
        raise SolisCloudError(f"{path} returned HTTP {status}", code=str(status))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SolisServerError(f"{path} returned invalid JSON") from err
    if not isinstance(payload, dict):
        raise SolisServerError(f"{path} returned a non-object body")
    _raise_for_code(path, payload)
    _raise_for_clock_message(path, payload, server_date)
    return payload


def _raise_for_code(path: str, payload: dict[str, Any]) -> None:
    code = str(payload.get("code", "0"))
    if code == "0":
        return
    message = str(payload.get("msg") or code)
    if code in _AUTH_CODES:
        raise SolisAuthError(f"{path} {code}: {message}", code=code)
    if code == "1" or "数据异常" in message:
        raise SolisDataError(f"{path} {code}: {message}", code=code)
    raise SolisCloudError(f"{path} {code}: {message}", code=code)


def _raise_for_clock_message(
    path: str,
    payload: dict[str, Any],
    server_date: datetime | None,
) -> None:
    message = str(payload.get("msg") or "")
    if "time" in message.lower() and "15" in message:
        raise SolisClockError(f"{path}: {message}", server_date=server_date)


def date_is_skewed(date_header: str, *, now: datetime, window: timedelta) -> bool:
    """Return whether a Date header is outside Solis's acceptance window."""
    try:
        parsed = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return abs(now - parsed) > window
