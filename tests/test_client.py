"""HTTP client against the signature-checking mock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiohttp import ClientSession
import pytest

from custom_components.solis.api import SolisCloudClient
from custom_components.solis.errors import (
    SolisAuthError,
    SolisDataError,
    SolisRateLimitError,
    SolisServerError,
    SolisTimeoutError,
)
from mock_soliscloud.catalog import KEY_ID, SECRET


def _client(url: str, session: ClientSession, **kwargs: object) -> SolisCloudClient:
    return SolisCloudClient(
        session,
        base_url=url,
        key_id=KEY_ID,
        secret=SECRET,
        timeout=float(kwargs.get("timeout", 5)),
        endpoint_interval=0,
        attempts=int(kwargs.get("attempts", 3)),
        clock=kwargs.get("clock"),  # type: ignore[arg-type]
    )


async def test_lists_stations(solis_server: tuple[object, str]) -> None:
    """A signed call returns both plants."""
    _state, url = solis_server
    async with ClientSession() as session:
        client = _client(url, session)
        stations = await client.user_station_list()
    assert {row["stationName"] for row in stations} == {"North Roof", "Barn"}


async def test_bad_secret_is_auth(solis_server: tuple[object, str]) -> None:
    """A wrong secret fails the mock's HMAC check."""
    _state, url = solis_server
    async with ClientSession() as session:
        client = SolisCloudClient(
            session,
            base_url=url,
            key_id=KEY_ID,
            secret="wrong",
            endpoint_interval=0,
            attempts=1,
        )
        with pytest.raises(SolisAuthError):
            await client.user_station_list()


async def test_clock_skew(solis_server: tuple[object, str]) -> None:
    """A skewed local clock is resigned against the Date header and then accepted."""
    _state, url = solis_server

    def past() -> datetime:
        return datetime.now(UTC) - timedelta(minutes=30)

    async with ClientSession() as session:
        client = _client(url, session, clock=past, attempts=1)
        stations = await client.user_station_list()
    assert {row["stationName"] for row in stations} == {"North Roof", "Barn"}


async def test_rate_limit_and_server_errors(solis_server: tuple[object, str]) -> None:
    """429 and 502 are retried and then raised."""
    state, url = solis_server
    state.mode = "rate_limit"  # type: ignore[attr-defined]
    async with ClientSession() as session:
        client = _client(url, session, attempts=2)
        with pytest.raises(SolisRateLimitError):
            await client.user_station_list()
        state.mode = "server_error"  # type: ignore[attr-defined]
        with pytest.raises(SolisServerError):
            await client.user_station_list()
        state.mode = "abnormal"  # type: ignore[attr-defined]
        with pytest.raises(SolisDataError):
            await client.user_station_list()


async def test_timeout(solis_server: tuple[object, str]) -> None:
    """A gateway timeout becomes SolisTimeoutError."""
    state, url = solis_server
    state.mode = "timeout"  # type: ignore[attr-defined]
    async with ClientSession() as session:
        client = _client(url, session, timeout=1, attempts=1)
        with pytest.raises(SolisTimeoutError):
            await client.user_station_list()
