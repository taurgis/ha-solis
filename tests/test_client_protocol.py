"""Signing edge cases and HTTP error mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import ClientSession, web
import pytest

from custom_components.solis.api import (
    SolisCloudClient,
    _retry_after,
    date_is_skewed,
)
from custom_components.solis.errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisServerError,
)
from custom_components.solis.signing import content_md5, signed_headers, string_to_sign


def test_string_to_sign_is_five_lines() -> None:
    """The canonical string is the five fields Solis documents, in order."""
    signed = string_to_sign(content_md5_value="md5", date_value="Date", path="/v1/api/alarmList")
    assert signed.split("\n") == ["POST", "md5", "application/json", "Date", "/v1/api/alarmList"]


def test_wrong_path_or_secret_does_not_match() -> None:
    """A signature for one path is not valid for another."""
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    body = b"{}"
    headers = signed_headers(
        key_id="id",
        secret="secret",
        path="/v1/api/inverterList",
        body=body,
        moment=moment,
    )
    from custom_components.solis.signing import signature_matches

    assert not signature_matches(
        secret="other",
        key_id="id",
        path="/v1/api/inverterList",
        body=body,
        headers=headers,
    )
    assert not signature_matches(
        secret="secret",
        key_id="id",
        path="/v1/api/alarmList",
        body=body,
        headers=headers,
    )
    assert headers["Content-MD5"] == content_md5(body)


def test_retry_after_and_clock_window() -> None:
    """Retry-After and Date parsing cover the documented failure modes."""
    assert _retry_after(None) == 2.0
    assert _retry_after("4") == 4.0
    assert _retry_after("soon") == 2.0
    assert _retry_after("-3") == 0.0
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    window = timedelta(minutes=15)
    assert date_is_skewed("not a date", now=now, window=window)
    assert not date_is_skewed("Mon, 01 Jun 2026 12:05:00 GMT", now=now, window=window)
    assert date_is_skewed("Mon, 01 Jun 2026 10:00:00 GMT", now=now, window=window)


@pytest.fixture
async def raw_api(socket_enabled: None) -> Any:
    """A tiny Solis-shaped server that does not check signatures."""
    app = web.Application()
    app["responder"] = None

    async def handle(request: web.Request) -> web.StreamResponse:
        responder = app["responder"]
        assert responder is not None
        return await responder(request)

    app.router.add_post("/v1/api/{name}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        yield app, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def _client(url: str, session: ClientSession, **kwargs: object) -> SolisCloudClient:
    return SolisCloudClient(
        session,
        base_url=url,
        key_id="id",
        secret="secret",
        timeout=2,
        endpoint_interval=0,
        attempts=int(kwargs.pop("attempts", 1)),  # type: ignore[arg-type]
    )


async def _post(raw_api: tuple[Any, str], responder: Any, **kwargs: object) -> Any:
    app, url = raw_api
    app["responder"] = responder
    async with ClientSession() as session:
        return await _client(url, session, **kwargs).user_station_list()


async def test_http_status_mapping(raw_api: tuple[Any, str]) -> None:
    """401, 400 and a non-object body become the matching client errors."""

    async def unauthorized(_request: web.Request) -> web.Response:
        return web.Response(status=401, text="no")

    with pytest.raises(SolisAuthError):
        await _post(raw_api, unauthorized)

    async def bad_request(_request: web.Request) -> web.Response:
        return web.Response(status=400, text="no")

    with pytest.raises(SolisServerError):
        await _post(raw_api, bad_request)

    async def not_object(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"[1]", content_type="application/json")

    with pytest.raises(SolisServerError, match="non-object"):
        await _post(raw_api, not_object)

    async def broken(_request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"{", content_type="application/json")

    with pytest.raises(SolisServerError, match="invalid JSON"):
        await _post(raw_api, broken)


async def test_api_code_mapping(raw_api: tuple[Any, str]) -> None:
    """Platform codes map to auth, abnormal data, clock, or a generic error."""

    async def coded(request: web.Request, code: str, message: str) -> web.Response:
        del request
        return web.json_response({"success": False, "code": code, "msg": message, "data": None})

    async def auth(request: web.Request) -> web.Response:
        return await coded(request, "R0000", "No authority")

    with pytest.raises(SolisAuthError):
        await _post(raw_api, auth)

    async def other(request: web.Request) -> web.Response:
        return await coded(request, "B0099", "nope")

    with pytest.raises(SolisCloudError):
        await _post(raw_api, other)

    async def clock(request: web.Request) -> web.Response:
        return await coded(request, "0", "time differs by 15 minutes")

    with pytest.raises(SolisClockError):
        await _post(raw_api, clock)

    async def chinese(request: web.Request) -> web.Response:
        return await coded(request, "2", "数据异常")

    with pytest.raises(SolisDataError):
        await _post(raw_api, chinese)


async def test_retries_then_reads_every_page(raw_api: tuple[Any, str]) -> None:
    """A 502 is retried, and a two-page list is concatenated."""
    state = {"calls": 0}

    async def flaky(request: web.Request) -> web.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return web.Response(status=502, text="down")
        page = int((await request.json())["pageNo"])
        rows = [{"id": f"p{page}", "stationName": f"Plant {page}"}]
        pages = 2 if page == 1 else 1
        return web.json_response(
            {
                "success": True,
                "code": "0",
                "msg": "success",
                "data": {"page": {"records": rows, "pages": pages}},
            }
        )

    rows = await _post(raw_api, flaky, attempts=2)
    # A short page stops pagination. The 502 is still retried before that page is kept.
    assert rows == [{"id": "p1", "stationName": "Plant 1"}]
    assert state["calls"] == 2
