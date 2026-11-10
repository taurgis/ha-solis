"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_ABNORMAL,
    ISSUE_AUTH,
    ISSUE_CLOCK,
    ISSUE_NO_INVERTERS,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisTimeoutError,
)
from .models import SolisSnapshot
from .transport import SolisTransport

_LOGGER = logging.getLogger(__name__)

_CLEAR_ON_SUCCESS = (ISSUE_AUTH, ISSUE_CLOCK, ISSUE_ABNORMAL, ISSUE_NO_INVERTERS)


class SolisCoordinator(DataUpdateCoordinator[SolisSnapshot]):
    """Poll a Solis transport and surface auth and clock failures as repairs."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: SolisTransport,
    ) -> None:
        """Set the poll interval from the config entry options."""
        scan = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan),
            config_entry=entry,
        )
        self.transport = transport
        self.auth_failed = False
        self._open_issues: set[str] = set()

    async def _async_update_data(self) -> SolisSnapshot:
        """Fetch a snapshot. Failures make entities unavailable."""
        try:
            snapshot = await self.transport.async_read()
        except SolisAuthError as err:
            already = self._issue_id(ISSUE_AUTH) in self._open_issues
            self.auth_failed = True
            self._open(ISSUE_AUTH, ir.IssueSeverity.ERROR, fixable=True)
            if not already:
                self.config_entry.async_start_reauth(self.hass)
            _LOGGER.warning("Solis Cloud rejected the API credentials: %s", err)
            raise UpdateFailed(str(err)) from err
        except SolisClockError as err:
            self._open(ISSUE_CLOCK, ir.IssueSeverity.ERROR, fixable=False)
            _LOGGER.warning("Solis Cloud rejected the clock: %s", err)
            raise UpdateFailed(str(err)) from err
        except SolisDataError as err:
            self._open(ISSUE_ABNORMAL, ir.IssueSeverity.WARNING, fixable=False)
            _LOGGER.warning("Solis Cloud reported abnormal data: %s", err)
            raise UpdateFailed(str(err)) from err
        except SolisTimeoutError as err:
            _LOGGER.warning("Solis Cloud timed out; entities will be unavailable: %s", err)
            raise UpdateFailed(str(err)) from err
        except SolisCloudError as err:
            if "no inverters" in str(err) or "no stations" in str(err):
                self._open(ISSUE_NO_INVERTERS, ir.IssueSeverity.WARNING, fixable=False)
            _LOGGER.warning("Solis Cloud update failed; entities will be unavailable: %s", err)
            raise UpdateFailed(str(err)) from err
        self.auth_failed = False
        self._clear_recovered()
        return snapshot

    def _issue_id(self, key: str) -> str:
        return f"{key}_{self.config_entry.entry_id}"

    def _open(self, key: str, severity: ir.IssueSeverity, *, fixable: bool) -> None:
        issue_id = self._issue_id(key)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=fixable,
            severity=severity,
            translation_key=key,
            data={"entry_id": self.config_entry.entry_id},
        )
        self._open_issues.add(issue_id)

    def _clear_recovered(self) -> None:
        for key in _CLEAR_ON_SUCCESS:
            issue_id = self._issue_id(key)
            if issue_id in self._open_issues:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._open_issues.discard(issue_id)


def runtime_options(entry: ConfigEntry) -> dict[str, Any]:
    """Return options with defaults filled in."""
    scan = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    return {"scan_interval": scan}
