"""Fixtures for the Solis integration tests."""

from __future__ import annotations

from typing import Any

from aiohttp import web
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solis.const import DOMAIN
from mock_soliscloud.app import MockState, create_app
from mock_soliscloud.catalog import KEY_ID, SECRET


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom components from this repository."""


@pytest.fixture
async def solis_server(socket_enabled: None) -> Any:
    """Start the mock SolisCloud API on an ephemeral port."""
    state = MockState()
    application = create_app(state)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = getattr(site._server, "sockets", None)
    assert sockets
    port = sockets[0].getsockname()[1]
    try:
        yield state, f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def entry_data(url: str, stations: list[str] | None = None) -> dict[str, Any]:
    """Return config-entry data aimed at the mock."""
    return {
        "api_url": url,
        "key_id": KEY_ID,
        "key_secret": SECRET,
        "station_ids": stations or ["1900000000000001001", "1900000000000001002"],
    }


def entry_options() -> dict[str, Any]:
    """Return options that do not pace tests at the production rate limit."""
    return {
        "scan_interval": 3600,
        "request_timeout": 10,
        "endpoint_interval": 0,
    }


def mock_entry(url: str) -> MockConfigEntry:
    """Return a version-2 config entry for the mock account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Solis",
        data=entry_data(url),
        options=entry_options(),
        version=2,
        unique_id=KEY_ID,
    )
