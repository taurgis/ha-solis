"""Data update coordinator."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import inspect
import logging
import random
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LOGGER_DETECT,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    DATA_DEGRADED,
    DEFAULT_LOCAL_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_CLOCK,
    ISSUE_LEGACY_CONTROL,
    ISSUE_LOGGER_GATEWAY,
    ISSUE_LOGGER_LOCKED,
    ISSUE_LOGGER_PROTOCOL,
    ISSUE_LOGGER_S5,
    ISSUE_LOGGER_SILENT,
    ISSUE_LOGGER_TCP,
    ISSUE_LOGGER_WEB,
    ISSUE_NO_INVERTERS,
    MIN_CLOUD_SCAN_INTERVAL,
    MIN_LOCAL_SCAN_INTERVAL,
    OUTAGE_POLLS,
    POLL_REPAIRS,
    REASON_GATEWAY,
    REASON_LOCKED,
    REASON_S5_CLOSED,
    REASON_SILENT,
    REASON_TCP_DISABLED,
    REASON_UNSUPPORTED,
    REASON_WEB_AUTH,
    REASON_WEB_PAGE,
    REASON_WRONG_PROTOCOL,
    REASON_WRONG_SLAVE,
    SOURCE_CLOUD,
    STALE_AFTER,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisDataError,
    SolisLoggerAuthError,
    SolisLoggerBusy,
    SolisLoggerOffline,
    SolisRateLimitError,
    SolisServerError,
    SolisTimeoutError,
)
from .local_transport import asleep_snapshot
from .loggers import detection_why
from .models import SolisSnapshot, snapshot_from_dict, snapshot_to_dict
from .transport import SolisTransport

_LOGGER = logging.getLogger(__name__)

_BACKOFF_CAP = 900.0
_LOCK_REPAIR_AFTER = 600.0
# A stick that rejects its web password three times in a row, or for
# _LOCK_REPAIR_AFTER, needs the new one. A single 401 can be a stick rebooting.
_STICK_REJECTIONS = 3
_SAVE_EVERY = 300.0
DOCS_TROUBLESHOOTING = "https://github.com/taurgis/ha-solis/blob/main/docs/troubleshooting.md"
# Repairs whose fix is a step the user takes outside Home Assistant. The page
# explains it; nothing in Home Assistant can confirm the fix, so none is fixable.
LEARN_MORE = {
    ISSUE_LOGGER_GATEWAY: f"{DOCS_TROUBLESHOOTING}#logger-gateway",
    ISSUE_LOGGER_PROTOCOL: f"{DOCS_TROUBLESHOOTING}#wrong-protocol",
    ISSUE_LOGGER_S5: f"{DOCS_TROUBLESHOOTING}#s5-port-closed",
    ISSUE_LEGACY_CONTROL: f"{DOCS_TROUBLESHOOTING}#legacy-control",
}
_LOGGER_ISSUES = (
    ISSUE_LOGGER_LOCKED,
    ISSUE_LOGGER_TCP,
    ISSUE_LOGGER_SILENT,
    ISSUE_LOGGER_GATEWAY,
    ISSUE_LOGGER_PROTOCOL,
    ISSUE_LOGGER_S5,
    ISSUE_LOGGER_WEB,
)
_LOGGER_ISSUE = {
    REASON_TCP_DISABLED: ISSUE_LOGGER_TCP,
    REASON_SILENT: ISSUE_LOGGER_SILENT,
    REASON_GATEWAY: ISSUE_LOGGER_GATEWAY,
    REASON_WRONG_PROTOCOL: ISSUE_LOGGER_PROTOCOL,
    REASON_WRONG_SLAVE: ISSUE_LOGGER_PROTOCOL,
    REASON_UNSUPPORTED: ISSUE_LOGGER_PROTOCOL,
    REASON_S5_CLOSED: ISSUE_LOGGER_S5,
    REASON_WEB_AUTH: ISSUE_LOGGER_WEB,
    REASON_WEB_PAGE: ISSUE_LOGGER_WEB,
}
_BUSY_UPDATE = {
    REASON_TCP_DISABLED: "update_logger_tcp_disabled",
    REASON_SILENT: "update_logger_modbus_silent",
    REASON_GATEWAY: "update_logger_gateway",
    REASON_WRONG_PROTOCOL: "update_logger_wrong_protocol",
    REASON_WRONG_SLAVE: "update_logger_wrong_protocol",
    REASON_UNSUPPORTED: "update_logger_wrong_protocol",
    REASON_S5_CLOSED: "update_logger_s5_closed",
    REASON_WEB_AUTH: "update_logger_web",
    REASON_WEB_PAGE: "update_logger_web",
}
_DEGRADED_INFO = {
    REASON_TCP_DISABLED: (
        "S2-WL-ST port 502 is closed. Modbus TCP needs firmware 100101B4 or newer, "
        "and some later updates close the port again."
    ),
    REASON_SILENT: (
        "S2-WL-ST accepted TCP and sent no Modbus. Another client may hold the "
        "only connection, or this firmware stops local reads while SolisCloud is connected."
    ),
    REASON_LOCKED: (
        "Solarman local reads are paused after a connection reset. "
        "A fresh SolisCloud sample is used when one is available"
    ),
    REASON_GATEWAY: (
        "The Modbus gateway is not accepting connections. The last local "
        "sample is held. This is not the inverter powering a stick off."
    ),
    REASON_WRONG_PROTOCOL: (
        "The gateway answered, but the bytes were not the framing selected "
        "for this entry. A Modbus TCP gateway that is still in transparent "
        "mode needs the RTU-over-TCP option, and the reverse is also true."
    ),
    REASON_WRONG_SLAVE: (
        "A Modbus reply arrived from a different slave than the one stored on this entry."
    ),
    REASON_UNSUPPORTED: (
        "The device speaks Modbus and does not serve the Solis 3000 or 33000 register map."
    ),
    REASON_S5_CLOSED: (
        "S5-WiFi-ST port 502 is closed. Solis does not officially offer local "
        "Modbus on this stick, and newer firmware closes the port."
    ),
    REASON_WEB_AUTH: (
        "The stick web page rejected the password. The last local sample is held, "
        "or a fresh SolisCloud sample is used. Home Assistant asks for the new password."
    ),
    REASON_WEB_PAGE: (
        "The stick web page did not return the Solis inverter page. The last local "
        "sample is held, or a fresh SolisCloud sample is used."
    ),
}
_DEGRADED_WARN = {
    REASON_TCP_DISABLED: (
        "S2-WL-ST is still not serving Modbus TCP. Ask Solis for a firmware that "
        "keeps port 502 open, such as 100121cf or a 100141dx build."
    ),
    REASON_SILENT: (
        "S2-WL-ST is still accepting TCP without answering Modbus. "
        "Stop the other Modbus client, or ask Solis for firmware that allows "
        "SolisCloud and Modbus at the same time."
    ),
    REASON_LOCKED: (
        "Solarman logger is still refusing local Modbus. "
        "It usually recovers within 10 minutes, or after the inverter is power-cycled"
    ),
    REASON_GATEWAY: (
        "The Modbus gateway is still refusing connections. Check the IP, "
        "the port, and that the gateway is powered."
    ),
    REASON_WRONG_PROTOCOL: (
        "The gateway is still not speaking the framing selected for this "
        "entry. Change the logger type, or the mode on the gateway."
    ),
    REASON_WRONG_SLAVE: (
        "Modbus replies are still coming from a different slave. Set the "
        "slave to the address configured on the inverter."
    ),
    REASON_UNSUPPORTED: (
        "The device is still not serving a Solis register map. Confirm it "
        "is the inverter and not another Modbus device on that address."
    ),
    REASON_S5_CLOSED: (
        "S5-WiFi-ST is still not serving port 502. Solis does not officially "
        "support local Modbus on this stick. A firmware update often closes it."
    ),
    REASON_WEB_AUTH: (
        "The stick web page is still rejecting the password. Enter the current one "
        "when Home Assistant asks to reauthenticate (user admin, factory password 123456789)."
    ),
    REASON_WEB_PAGE: (
        "The stick web page is still not returning the Solis inverter page. "
        "Open the stick address in a browser and check the inverter status."
    ),
}
_DEGRADED_DETAIL = {
    REASON_TCP_DISABLED: "Port 502 is closed. Modbus TCP is disabled on this firmware.",
    REASON_SILENT: (
        "TCP connected and no Modbus reply arrived. The stick may be out of connections."
    ),
    REASON_LOCKED: (
        "Local Modbus was reset. A fresh SolisCloud sample is used when "
        "one exists. The next local try waits at least 10 seconds and "
        "at most about 10 minutes."
    ),
    REASON_GATEWAY: "The gateway is not accepting TCP. The last power sample is held.",
    REASON_WRONG_PROTOCOL: "The bytes on the socket are not the framing selected for this entry.",
    REASON_WRONG_SLAVE: "The Modbus reply came from a different slave.",
    REASON_UNSUPPORTED: "Modbus answered, and neither Solis register map matched.",
    REASON_S5_CLOSED: (
        "Port 502 is closed on an S5-WiFi-ST. Local access is experimental "
        "and newer firmware removes it. Use Reconfigure to switch to a gateway."
    ),
    REASON_WEB_AUTH: "The stick web page rejected the password.",
    REASON_WEB_PAGE: "The stick web page did not return the Solis inverter page.",
}


def poll_seconds(entry: ConfigEntry) -> int:
    """Return the poll period. Local can be 30 seconds. Cloud stays at 60 or slower."""
    source = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
    default = DEFAULT_SCAN_INTERVAL if source == SOURCE_CLOUD else DEFAULT_LOCAL_SCAN_INTERVAL
    scan = int(entry.options.get(CONF_SCAN_INTERVAL, default))
    floor = MIN_CLOUD_SCAN_INTERVAL if source == SOURCE_CLOUD else MIN_LOCAL_SCAN_INTERVAL
    return max(floor, min(3600, scan))


_SUPPORTS_RETRY_AFTER = "retry_after" in inspect.signature(UpdateFailed.__init__).parameters
_TRANSIENT = (SolisTimeoutError, SolisServerError, SolisRateLimitError)


def snapshot_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The file that keeps the last good snapshot across restarts."""
    return Store(hass, 1, f"{DOMAIN}.snapshot.{entry_id}")


def _failed(key: str, retry_after: float | None = None) -> UpdateFailed:
    """Return a translated update failure. The coordinator logs it once."""
    if retry_after is not None and _SUPPORTS_RETRY_AFTER:
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key=key,
            retry_after=retry_after,
        )
    return UpdateFailed(translation_domain=DOMAIN, translation_key=key)


def _is_web_auth(err: SolisLoggerBusy) -> bool:
    """Return whether the stick web page rejected the stored password."""
    return isinstance(err, SolisLoggerAuthError) or err.reason == REASON_WEB_AUTH


def _is_degraded(snapshot: SolisSnapshot) -> bool:
    if snapshot.source == DATA_DEGRADED:
        return True
    return any(inverter.data_source == DATA_DEGRADED for inverter in snapshot.inverters)


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
        self._base_interval = timedelta(seconds=poll_seconds(entry))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self._base_interval,
            config_entry=entry,
        )
        self.transport = transport
        self.auth_failed = False
        self._reauth_started = False
        self._stick_reauth_started = False
        self._stick_rejections = 0
        self._stick_rejected_since: float | None = None
        self._outage_logged = False
        self._open_issues: set[str] = set()
        self._transient_failures = 0
        self._asleep_polls = 0
        self._degraded_since: float | None = None
        self._degraded_logged = False
        self._reason = ""
        self._last_good: SolisSnapshot | None = None
        self._saved_at = float("-inf")
        self._cleared_stale = False
        self._store = snapshot_store(hass, entry.entry_id)

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
            self.auth_failed = True
            # Home Assistant starts reauth and stops polling with the rejected key.
            # The reauth card is the notice, so no repair is opened as well.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="update_auth"
            ) from err
        except SolisClockError as err:
            self._open(ISSUE_CLOCK, ir.IssueSeverity.ERROR)
            raise _failed("update_clock") from err
        except SolisDataError as err:
            # An account-side fault with no step in Home Assistant: the coordinator
            # logs the failed update once and entities go unavailable.
            raise _failed("update_abnormal") from err
        except _TRANSIENT as err:
            return await self._hold(err)
        except SolisLoggerBusy as err:
            if not _is_web_auth(err):
                self._clear_stick_rejections()
            elif self._stick_password_due(rejected=not err.waiting) or not await self._has_sample():
                # A password that keeps failing does not fix itself, so ask for
                # the new one. With nothing to show yet there is nothing to hold.
                self.auth_failed = True
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="update_logger_web_auth"
                ) from err
            return await self._hold_degraded(err)
        except SolisLoggerOffline as err:
            return await self._hold_offline(err)
        except SolisCloudError as err:
            self._recover_pace()
            if "no inverters" in str(err) or "no stations" in str(err):
                self._open(ISSUE_NO_INVERTERS, ir.IssueSeverity.WARNING)
                raise _failed("update_no_inverters") from err
            raise _failed("update_failed") from err
        if not _has_live_inverter(snapshot):
            return await self._hold(SolisServerError("no live inverter readings"))
        accepted = await self._accept(snapshot)
        self._flag_cloud_side()
        return accepted

    def _cloud_side_keys(self) -> set[str]:
        """Repairs the cloud half of a local-plus-cloud entry still needs open."""
        error = getattr(self.transport, "cloud_error", None)
        if isinstance(error, SolisClockError):
            return {ISSUE_CLOCK}
        return set()

    def _flag_cloud_side(self) -> None:
        """A local read worked while SolisCloud rejected the key or the clock.

        A rejected key starts reauth once and keeps local polling. The reauth
        card is the notice; a repair for the same key would only duplicate it.
        """
        error = getattr(self.transport, "cloud_error", None)
        if isinstance(error, SolisAuthError):
            if not self._reauth_started:
                self._reauth_started = True
                self.config_entry.async_start_reauth(self.hass)
        else:
            self._reauth_started = False
        for key in self._cloud_side_keys():
            self._open(key, ir.IssueSeverity.ERROR)

    async def _accept(self, snapshot: SolisSnapshot) -> SolisSnapshot:
        if snapshot.source == "asleep":
            self._slow_for_sleep()
            self._clear_logger_lock()
            self._last_good = snapshot
            await self._persist(snapshot)
            return snapshot
        if _is_degraded(snapshot):
            # A degraded read still answered, so drop any night-time slowdown.
            self._asleep_polls = 0
            self._recover_pace()
            self._note_degraded()
            self._flag_stick_password()
            self._last_good = snapshot
            await self._persist(snapshot)
            return snapshot
        self._asleep_polls = 0
        self._clear_logger_lock()
        self._recover_pace()
        self._last_good = snapshot
        self.auth_failed = False
        if self._outage_logged:
            _LOGGER.info("SolisCloud is reachable again")
            self._outage_logged = False
        self._clear_recovered()
        await self._persist(snapshot)
        return snapshot

    def _flag_stick_password(self) -> None:
        """SolisCloud covers a stick that rejected its password: ask for it once.

        The cloud keeps the sensors going meanwhile, so this waits until the
        password has been rejected for ``_LOCK_REPAIR_AFTER``.
        """
        if str(getattr(self.transport, "degraded_reason", "") or "") != REASON_WEB_AUTH:
            self._stick_reauth_started = False
            self._clear_stick_rejections()
            return
        if not self._stick_reauth_started and self._stick_password_due(rejected=False):
            self._stick_reauth_started = True
            self.config_entry.async_start_reauth(self.hass)

    def _stick_password_due(self, *, rejected: bool) -> bool:
        """Count a rejected stick password. True after three in a row or 10 minutes.

        ``rejected`` is a fresh 401; a read skipped during the backoff only
        counts toward the time.
        """
        now = time.monotonic()
        if self._stick_rejected_since is None:
            self._stick_rejected_since = now
        if rejected:
            self._stick_rejections += 1
        return (
            self._stick_rejections >= _STICK_REJECTIONS
            or now - self._stick_rejected_since >= _LOCK_REPAIR_AFTER
        )

    def _clear_stick_rejections(self) -> None:
        self._stick_rejections = 0
        self._stick_rejected_since = None

    async def _has_sample(self) -> bool:
        """True when there is a last or stored snapshot to hold."""
        return self._last_good is not None or await self._load_stored() is not None

    def _slow_for_sleep(self) -> None:
        """Back off while the logger is powered down. Do not open a repair."""
        self._asleep_polls += 1
        base = self._base_interval.total_seconds()
        delay = min(900.0, max(base, 60.0 * float(2 ** min(self._asleep_polls, 4))))
        self.update_interval = timedelta(seconds=delay)

    def _note_degraded(self) -> None:
        """Remember a local fault. Open a repair only after it has lasted 10 minutes."""
        reported = str(getattr(self.transport, "degraded_reason", "") or "")
        if reported:
            self._reason = reported
        reason = self._reason or REASON_LOCKED
        now = time.monotonic()
        if self._degraded_since is None:
            self._degraded_since = now
        if not self._degraded_logged:
            _LOGGER.info("%s", _DEGRADED_INFO.get(reason, _DEGRADED_INFO[REASON_LOCKED]))
            self._degraded_logged = True
        if now - self._degraded_since < _LOCK_REPAIR_AFTER:
            return
        if reason == REASON_WEB_AUTH:
            # Reauthentication asks for the password; a repair would repeat it.
            for key in _LOGGER_ISSUES:
                self._drop_issue(key)
            return
        issue_key = _LOGGER_ISSUE.get(reason, ISSUE_LOGGER_LOCKED)
        for key in _LOGGER_ISSUES:
            if key != issue_key:
                self._drop_issue(key)
        issue_id = self._issue_id(issue_key)
        if issue_id not in self._open_issues:
            _LOGGER.warning("%s", _DEGRADED_WARN.get(reason, _DEGRADED_WARN[REASON_LOCKED]))
        self._open(issue_key, ir.IssueSeverity.WARNING)

    def _clear_logger_lock(self) -> None:
        # Night ends a run of rejected stick passwords too: a 401 at dusk and
        # one the next morning are not "in a row".
        self._clear_stick_rejections()
        self._degraded_since = None
        self._degraded_logged = False
        self._reason = ""
        for key in _LOGGER_ISSUES:
            self._drop_issue(key)

    def _drop_issue(self, key: str) -> None:
        issue_id = self._issue_id(key)
        if issue_id not in self._open_issues:
            return
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._open_issues.discard(issue_id)

    async def _hold_degraded(self, err: SolisLoggerBusy) -> SolisSnapshot:
        """Keep the last sample without calling the inverter asleep."""
        # The logger answered (with a reset or a refusal), so it is not asleep:
        # leave the slow night pace that ``_slow_for_sleep`` may have set.
        self._asleep_polls = 0
        self._recover_pace()
        self._reason = err.reason or REASON_LOCKED
        self._note_degraded()
        held = self._last_good
        if held is None:
            held = await self._load_stored()
        if held is None:
            raise _failed(_BUSY_UPDATE.get(self._reason, "update_logger_busy")) from err
        degraded = replace(
            held,
            partial=True,
            source=DATA_DEGRADED,
            inverters=tuple(
                replace(inverter, data_source=DATA_DEGRADED) for inverter in held.inverters
            ),
        )
        self._last_good = degraded
        return degraded

    async def _hold_offline(self, err: SolisLoggerOffline) -> SolisSnapshot:
        """After a restart, night keeps the stored totals instead of failing setup."""
        held = self._last_good
        if held is None:
            held = await self._load_stored()
        if held is None:
            raise _failed("update_failed") from err
        self._clear_logger_lock()
        self._slow_for_sleep()
        asleep = asleep_snapshot(held)
        self._last_good = asleep
        await self._persist(asleep)
        return asleep

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
        if not self._outage_logged:
            # Nothing in Home Assistant fixes an outage, so this is a log line and
            # unavailable live sensors, not a repair.
            _LOGGER.warning(
                "SolisCloud has been unreachable; live readings are hidden "
                "and energy totals are held"
            )
            self._outage_logged = True
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

    async def _persist(self, snapshot: SolisSnapshot, *, force: bool = False) -> None:
        """Keep the last snapshot for a restart. Writes are spaced out to spare flash storage."""
        now = time.monotonic()
        if not force and now - self._saved_at < _SAVE_EVERY:
            return
        try:
            await self._store.async_save(snapshot_to_dict(snapshot))
        except (OSError, TypeError, ValueError):
            _LOGGER.debug("Could not store the last Solis snapshot", exc_info=True)
        else:
            self._saved_at = now

    async def async_save_now(self) -> None:
        """Write the last good snapshot, so a restart starts from it."""
        if self._last_good is not None:
            await self._persist(self._last_good, force=True)

    async def async_shutdown(self) -> None:
        """Save on unload. A plain Home Assistant stop calls ``async_save_now`` instead."""
        await self.async_save_now()
        await super().async_shutdown()

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

    def logger_report(self) -> dict[str, str]:
        """Describe the local session without addresses or serials."""
        if self._degraded_since is None:
            report = {"state": "ok"}
        else:
            reason = self._reason or REASON_LOCKED
            report = {
                "state": "degraded",
                "reason": reason,
                "detail": _DEGRADED_DETAIL.get(reason, _DEGRADED_DETAIL[REASON_LOCKED]),
            }
        info = getattr(self.transport, "session_info", None)
        if callable(info):
            report.update(info())
        code = str(self.config_entry.data.get(CONF_LOGGER_DETECT) or "")
        if code or report.get("kind"):
            report["detection"] = code
            report["why"] = detection_why(code)
        return report

    def _issue_id(self, key: str) -> str:
        return f"{key}_{self.config_entry.entry_id}"

    def _open(self, key: str, severity: ir.IssueSeverity) -> None:
        issue_id = self._issue_id(key)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            learn_more_url=LEARN_MORE.get(key),
            severity=severity,
            translation_key=key,
        )
        self._open_issues.add(issue_id)

    def _clear_recovered(self) -> None:
        kept = self._cloud_side_keys()
        first = not self._cleared_stale
        self._cleared_stale = True
        for key in POLL_REPAIRS:
            if key in kept:
                continue
            issue_id = self._issue_id(key)
            if first:
                # A repair opened before this setup (a failed setup, a reload, or an
                # older release) is unknown to this coordinator. Deleting is harmless
                # when absent.
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            if issue_id in self._open_issues:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._open_issues.discard(issue_id)
