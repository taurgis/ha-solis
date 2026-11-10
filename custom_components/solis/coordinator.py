"""Data update coordinator."""

from __future__ import annotations

from datetime import timedelta
import logging

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


def _failed(key: str) -> UpdateFailed:
    """Return a translated update failure. The coordinator logs it once."""
    return UpdateFailed(translation_domain=DOMAIN, translation_key=key)


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
        """Fetch a snapshot. Failures make entities unavailable.

        The coordinator logs the first failure and the recovery. This method
        does not log as well, or every poll would repeat the same message.
        """
        try:
            snapshot = await self.transport.async_read()
        except SolisAuthError as err:
            already = self._issue_id(ISSUE_AUTH) in self._open_issues
            self.auth_failed = True
            self._open(ISSUE_AUTH, ir.IssueSeverity.ERROR, fixable=True)
            if not already:
                self.config_entry.async_start_reauth(self.hass)
            raise _failed("update_auth") from err
        except SolisClockError as err:
            self._open(ISSUE_CLOCK, ir.IssueSeverity.ERROR, fixable=False)
            raise _failed("update_clock") from err
        except SolisDataError as err:
            self._open(ISSUE_ABNORMAL, ir.IssueSeverity.WARNING, fixable=False)
            raise _failed("update_abnormal") from err
        except SolisTimeoutError as err:
            raise _failed("update_timeout") from err
        except SolisCloudError as err:
            if "no inverters" in str(err) or "no stations" in str(err):
                self._open(ISSUE_NO_INVERTERS, ir.IssueSeverity.WARNING, fixable=False)
                raise _failed("update_no_inverters") from err
            raise _failed("update_failed") from err
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
