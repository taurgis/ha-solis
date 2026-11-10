"""Coordinator failure, repair, and recovery behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.solis.const import DOMAIN
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisTimeoutError,
)
from custom_components.solis.models import SolisSnapshot


class _Scripted:
    def __init__(self, results: list[object]) -> None:
        self._results = list(results)

    async def async_read(self) -> SolisSnapshot:
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, SolisSnapshot)
        return item


def _snapshot() -> SolisSnapshot:
    return SolisSnapshot(
        stations=(),
        inverters=(),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
    )


def _entry(hass: HomeAssistant, scan: int = 120) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"api_url": "http://example", "key_id": "k", "key_secret": "s", "station_ids": []},
        options={"scan_interval": scan},
        version=2,
        unique_id="k",
    )
    entry.add_to_hass(hass)
    return entry


def _issue_keys(hass: HomeAssistant) -> set[str]:
    from homeassistant.helpers import issue_registry as ir

    return {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}


async def test_scan_interval_comes_from_options(hass: HomeAssistant) -> None:
    """The coordinator polls at the configured interval."""
    entry = _entry(hass, 180)
    coordinator = SolisCoordinator(hass, entry, _Scripted([]))
    assert coordinator.update_interval == timedelta(seconds=180)


async def test_auth_starts_reauth_once_then_clears(hass: HomeAssistant) -> None:
    """A repeated auth failure does not stack reauth flows."""
    entry = _entry(hass)
    coordinator = SolisCoordinator(
        hass,
        entry,
        _Scripted([SolisAuthError("bad"), SolisAuthError("still bad"), _snapshot()]),
    )
    with patch.object(entry, "async_start_reauth") as start_reauth:
        with pytest.raises(UpdateFailed) as first:
            await coordinator._async_update_data()
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        start_reauth.assert_called_once_with(hass)
    assert first.value.translation_key == "update_auth"
    assert any(key.startswith("invalid_auth") for key in _issue_keys(hass))
    assert await coordinator._async_update_data()
    assert coordinator.auth_failed is False
    assert not any(key.startswith("invalid_auth") for key in _issue_keys(hass))


@pytest.mark.parametrize(
    ("error", "prefix"),
    [
        (SolisClockError("skew"), "clock_skew"),
        (SolisDataError("bad data"), "abnormal_data"),
        (SolisCloudError("no inverters returned"), "no_inverters"),
        (SolisCloudError("no stations returned for this account"), "no_inverters"),
    ],
)
async def test_repairs_for_clock_data_and_empty(
    hass: HomeAssistant, error: Exception, prefix: str
) -> None:
    """Clock, abnormal data, and an empty plant open a repair."""
    entry = _entry(hass)
    coordinator = SolisCoordinator(hass, entry, _Scripted([error]))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert any(key.startswith(prefix) for key in _issue_keys(hass))


async def test_timeout_and_generic_error_do_not_open_repairs(hass: HomeAssistant) -> None:
    """A timeout or an ordinary API error only makes the update fail."""
    entry = _entry(hass)
    coordinator = SolisCoordinator(
        hass,
        entry,
        _Scripted([SolisTimeoutError("slow"), SolisCloudError("gateway")]),
    )
    with pytest.raises(UpdateFailed) as timeout:
        await coordinator._async_update_data()
    assert timeout.value.translation_key == "update_timeout"
    with pytest.raises(UpdateFailed) as generic:
        await coordinator._async_update_data()
    assert generic.value.translation_key == "update_failed"
    assert _issue_keys(hass) == set()


async def test_coordinator_logs_failure_and_recovery_once(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """The coordinator logs the first miss and the return, not every poll."""
    entry = _entry(hass)
    coordinator = SolisCoordinator(
        hass,
        entry,
        _Scripted([SolisTimeoutError("slow"), SolisTimeoutError("slow"), _snapshot()]),
    )
    with caplog.at_level(logging.INFO):
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        await coordinator.async_refresh()
    failures = [
        record for record in caplog.records if "Error fetching solis data" in record.message
    ]
    recovered = [record for record in caplog.records if "recovered" in record.message]
    assert len(failures) == 1
    assert len(recovered) == 1
