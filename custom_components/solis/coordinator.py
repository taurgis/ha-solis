"""Data update coordinator."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import inspect
import logging
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_ABNORMAL,
    ISSUE_AUTH,
    ISSUE_CLOCK,
    ISSUE_NO_INVERTERS,
    ISSUE_OUTAGE,
    OUTAGE_POLLS,
    POLL_REPAIRS,
    STALE_AFTER,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisRateLimitError,
    SolisServerError,
    SolisTimeoutError,
)
from .models import SolisSnapshot, snapshot_from_dict, snapshot_to_dict
from .transport import SolisTransport

_LOGGER = logging.getLogger(__name__)

_BACKOFF_CAP = 900.0
_SUPPORTS_RETRY_AFTER = "retry_after" in inspect.signature(UpdateFailed.__init__).parameters
_TRANSIENT = (SolisTimeoutError, SolisServerError, SolisRateLimitError)


def _failed(key: str, retry_after: float | None = None) -> UpdateFailed:
    """Return a translated update failure. The coordinator logs it once."""
    if retry_after is not None and _SUPPORTS_RETRY_AFTER:
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=key,
            retry_after=retry_after,
        )
    return UpdateFailed(translation_domain=DOMAIN, translation_key=key)


def _has_live_inverter(snapshot: SolisSnapshot) -> bool:
    """Return whether this poll produced a reading we can treat as success.

    An empty inverter tuple is not a dead poll: the transport raises when the
    cloud really returned none. Every inverter flagged unreachable means the
    cloud answered but no device could be read.
    """
    if not snapshot.inverters:
        return True
    return any(not inverter.unreachable for inverter in snapshot.inverters)


def _fresh(snapshot: SolisSnapshot, now: datetime) -> bool:
    fetched = snapshot.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    return now - fetched <= STALE_AFTER


def _degrade(snapshot: SolisSnapshot) -> SolisSnapshot:
    """Hide live readings while keeping the last energy totals."""
    return replace(
        snapshot,
        partial=True,
        inverters=tuple(replace(item, unreachable=True) for item in snapshot.inverters),
    )


class SolisCoordinator(DataUpdateCoordinator[SolisSnapshot]):
    """Poll a Solis transport and surface auth, clock and outage repairs."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: SolisTransport,
    ) -> None:
        """Set the poll interval from the config entry options."""
        scan = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        self._base_interval = timedelta(seconds=scan)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self._base_interval,
            config_entry=entry,
        )
        self.transport = transport
        self.auth_failed = False
        self._open_issues: set[str] = set()
        self._transient_failures = 0
        self._asleep_polls = 0
        self._last_good: SolisSnapshot | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass,
            1,
            f"{DOMAIN}.snapshot.{entry.entry_id}",
        )

    async def _async_update_data(self) -> SolisSnapshot:
        """Fetch a snapshot.

        A blip keeps the last good values. A sustained outage hides live
        readings, holds energy totals, and opens a repair that clears on the
        next real success. Auth, clock and abnormal-data failures still fail
        the update immediately. This method does not log every miss: the
        coordinator logs an UpdateFailed once, and the outage repair is logged
        once when it opens.
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
        except _TRANSIENT as err:
            return await self._hold(err)
        except SolisCloudError as err:
            self._recover_pace()
            if "no inverters" in str(err) or "no stations" in str(err):
                self._open(ISSUE_NO_INVERTERS, ir.IssueSeverity.WARNING, fixable=False)
                raise _failed("update_no_inverters") from err
            raise _failed("update_failed") from err
        if not _has_live_inverter(snapshot):
            return await self._hold(SolisServerError("no live inverter readings"))
        return await self._accept(snapshot)

    async def _accept(self, snapshot: SolisSnapshot) -> SolisSnapshot:
        if snapshot.source == "asleep":
            self._slow_for_sleep()
            self._last_good = snapshot
            await self._persist(snapshot)
            return snapshot
        self._asleep_polls = 0
        self._recover_pace()
        self._last_good = snapshot
        self.auth_failed = False
        if self._issue_id(ISSUE_OUTAGE) in self._open_issues:
            _LOGGER.info("SolisCloud is reachable again")
        self._clear_recovered()
        await self._persist(snapshot)
        return snapshot

    def _slow_for_sleep(self) -> None:
        """Back off while the logger is powered down. Do not open a repair."""
        self._asleep_polls += 1
        base = self._base_interval.total_seconds()
        delay = min(900.0, max(base, 60.0 * float(2 ** min(self._asleep_polls, 4))))
        self.update_interval = timedelta(seconds=delay)

    def _recover_pace(self) -> None:
        self._transient_failures = 0
        self.update_interval = self._base_interval

    async def _hold(self, err: SolisCloudError) -> SolisSnapshot:
        """Keep the last snapshot, or fail setup when there has never been one."""
        self._transient_failures += 1
        self._apply_backoff()
        held = self._last_good
        if held is None:
            held = await self._load_stored()
            self._last_good = held
        if held is None:
            key = "update_timeout" if isinstance(err, SolisTimeoutError) else "update_failed"
            raise _failed(key, retry_after=self._backoff_seconds()) from err
        if self._transient_failures < OUTAGE_POLLS and _fresh(held, dt_util.utcnow()):
            return replace(held, partial=True)
        if self._issue_id(ISSUE_OUTAGE) not in self._open_issues:
            _LOGGER.warning(
                "SolisCloud has been unreachable; live readings are hidden "
                "and energy totals are held"
            )
        self._open(ISSUE_OUTAGE, ir.IssueSeverity.WARNING, fixable=False)
        return _degrade(held)

    def _apply_backoff(self) -> None:
        self.update_interval = timedelta(seconds=self._backoff_seconds())

    def _backoff_seconds(self) -> float:
        """Exponential backoff with jitter, never faster than the user's poll."""
        steps = min(max(self._transient_failures - 1, 0), 4)
        raw = min(_BACKOFF_CAP, 60.0 * (2**steps))
        jittered = raw * random.uniform(0.8, 1.2)
        paced = max(self._base_interval.total_seconds(), jittered)
        delay: float = min(_BACKOFF_CAP, paced)
        return delay

    async def _persist(self, snapshot: SolisSnapshot) -> None:
        try:
            await self._store.async_save(snapshot_to_dict(snapshot))
        except (OSError, TypeError, ValueError):
            _LOGGER.debug("Could not store the last Solis snapshot", exc_info=True)

    async def _load_stored(self) -> SolisSnapshot | None:
        try:
            raw = await self._store.async_load()
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return snapshot_from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return None

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
        for key in POLL_REPAIRS:
            issue_id = self._issue_id(key)
            if issue_id in self._open_issues:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._open_issues.discard(issue_id)
