"""Cloud transport.

A later local transport (Modbus TCP or Solarman V5) should implement
``SolisTransport`` and return the same ``SolisSnapshot``. Entity platforms
never call HTTP themselves.

Control writes are intentionally absent. SolisCloud ``/v2/api/atRead`` and
``/v2/api/control`` are documented in ``docs/research/solis-api.md`` and are
not polled: upstream issue 437 reports that polling them drops the stick's
cloud session.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Protocol

from .api import SolisCloudClient
from .errors import SolisAuthError, SolisClockError, SolisCloudError
from .models import (
    AlarmSnapshot,
    CollectorSnapshot,
    EpmSnapshot,
    InverterSnapshot,
    SolisSnapshot,
    StationSnapshot,
)
from .parser import (
    local_date,
    parse_alarm,
    parse_collector,
    parse_epm,
    parse_epoch,
    parse_inverter,
    parse_offset_hours,
    parse_station,
    resolve_zone,
)

_LOGGER = logging.getLogger(__name__)

# A successful empty inverter list is a known Solis glitch. Keep the previous
# list for a few polls before believing the station really has no inverters.
_EMPTY_LIST_TOLERANCE = 3


class SolisTransport(Protocol):
    """Something that can read a plant snapshot."""

    async def async_read(self) -> SolisSnapshot:
        """Return the latest snapshot or raise ``SolisCloudError``."""


class SolisCloudTransport:
    """Read SolisCloud and remember the last good inverter list and details."""

    def __init__(
        self,
        client: SolisCloudClient,
        station_ids: set[str],
        *,
        time_zone: str = "",
        hass_time_zone: str | None = None,
    ) -> None:
        """``station_ids`` empty means every station on the account.

        ``time_zone`` is the entry option (``""``, ``plant`` or an IANA name).
        ``hass_time_zone`` is Home Assistant's zone, used when the option is
        left on automatic.
        """
        self._client = client
        self._station_ids = station_ids
        self._time_zone = time_zone
        self._hass_time_zone = hass_time_zone
        self._details: dict[str, dict[str, Any]] = {}
        self._lists: dict[str, list[dict[str, Any]]] = {}
        self._empty_lists: dict[str, int] = {}
        self._warned: set[str] = set()

    async def async_read(self) -> SolisSnapshot:
        """Poll stations, inverters, alarms, collectors and EPMs."""
        now = datetime.now(UTC)
        summaries = await self._client.user_station_list()
        chosen = [row for row in summaries if self._wanted(row)]
        if not chosen:
            raise SolisCloudError("no stations returned for this account")
        partial = False
        stations: list[StationSnapshot] = []
        inverters: list[InverterSnapshot] = []
        for summary in chosen:
            station_id = str(summary.get("id") or "")
            detail, detail_failed = await self._optional(
                f"station {station_id} detail",
                lambda station_id=station_id: self._client.station_detail(station_id),
            )
            partial = partial or detail_failed
            offset = parse_offset_hours((detail or {}).get("timeZone"))
            if offset is None:
                offset = parse_offset_hours(summary.get("timeZone"))
            zone = resolve_zone(offset, self._time_zone, self._hass_time_zone, now)
            day, day_failed = await self._optional(
                f"station {station_id} day energy",
                lambda station_id=station_id, offset=offset, zone=zone: (
                    self._client.station_day_energy(
                        station_id,
                        local_date(offset, now, zone),
                    )
                ),
            )
            partial = partial or day_failed
            stations.append(parse_station(summary, detail, day))
            listed, list_failed = await self._inverters_for(station_id)
            partial = partial or list_failed
            for row in listed:
                serial = str(row.get("sn") or "")
                device_id = str(row.get("id") or "")
                detail_row, failed, unreachable = await self._inverter_detail(
                    serial, device_id, row
                )
                partial = partial or failed
                if detail_row is None:
                    continue
                parsed = parse_inverter(
                    detail_row,
                    station_id=station_id,
                    name=str(row.get("name") or row.get("sn") or serial),
                )
                if unreachable:
                    parsed = replace(parsed, unreachable=True)
                if parsed.serial:
                    inverters.append(parsed)
        alarms, alarms_failed = await self._optional("alarms", self._client.alarm_list)
        collectors, collectors_failed = await self._optional(
            "collectors", self._client.collector_list
        )
        epms, epms_failed = await self._optional("epm", self._client.epm_list)
        partial = partial or alarms_failed or collectors_failed or epms_failed
        if not inverters:
            raise SolisCloudError("no inverters returned")
        self._prune(stations, inverters)
        return SolisSnapshot(
            stations=tuple(stations),
            inverters=tuple(inverters),
            alarms=_alarms(alarms or []),
            collectors=_collectors(collectors or []),
            epms=_epms(epms or []),
            fetched_at=now,
            partial=partial,
        )

    def _prune(self, stations: list[StationSnapshot], inverters: list[InverterSnapshot]) -> None:
        """Drop cached rows for plants and inverters that are no longer present."""
        live_stations = {station.station_id for station in stations}
        live_serials = {inverter.serial for inverter in inverters}
        for station_id in list(self._lists):
            if station_id not in live_stations:
                self._lists.pop(station_id, None)
                self._empty_lists.pop(station_id, None)
        for serial in list(self._details):
            if serial not in live_serials:
                self._details.pop(serial, None)

    def _wanted(self, row: dict[str, Any]) -> bool:
        if not self._station_ids:
            return True
        return str(row.get("id") or "") in self._station_ids

    async def _inverters_for(self, station_id: str) -> tuple[list[dict[str, Any]], bool]:
        try:
            rows = await self._client.inverter_list(station_id)
        except (SolisAuthError, SolisClockError):
            raise
        except SolisCloudError as err:
            cached = self._lists.get(station_id)
            self._warn_once(
                f"list:{station_id}",
                "Inverter list for %s failed (%s); keeping the last list",
                station_id,
                err,
            )
            if not cached:
                raise
            return cached, True
        if not rows and self._lists.get(station_id):
            self._empty_lists[station_id] = self._empty_lists.get(station_id, 0) + 1
            if self._empty_lists[station_id] < _EMPTY_LIST_TOLERANCE:
                self._warn_once(
                    f"empty:{station_id}",
                    "Inverter list for %s was empty; keeping the last list (%s/%s)",
                    station_id,
                    self._empty_lists[station_id],
                    _EMPTY_LIST_TOLERANCE,
                )
                return self._lists[station_id], True
        self._empty_lists[station_id] = 0
        self._warned.discard(f"list:{station_id}")
        self._warned.discard(f"empty:{station_id}")
        self._lists[station_id] = rows
        return rows, False

    async def _inverter_detail(
        self,
        serial: str,
        device_id: str,
        list_row: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool, bool]:
        """Return detail JSON, whether the poll was partial, and unreachable."""
        if not serial or not device_id:
            return list_row, True, True
        try:
            detail = await self._client.inverter_detail(device_id, serial)
        except (SolisAuthError, SolisClockError):
            raise
        except SolisCloudError as err:
            self._warn_once(
                f"detail:{serial}",
                "Detail for %s failed (%s); using the last copy",
                serial,
                err,
            )
            cached = self._details.get(serial)
            if cached is not None:
                return cached, True, True
            return list_row, True, True
        kept, partial, unreachable = self._prefer_cached(serial, detail)
        if not partial:
            self._warned.discard(f"detail:{serial}")
        return kept, partial, unreachable

    def _prefer_cached(
        self, serial: str, detail: dict[str, Any]
    ) -> tuple[dict[str, Any], bool, bool]:
        """Ignore a sample older than the last one, or a timestamp a day ahead.

        The return is the JSON to publish, whether this read was partial, and
        whether live sensors should hide it. A future sample with no earlier
        copy is hidden. An older sample is dropped in favour of the cache.
        """
        new_ts = parse_epoch(detail.get("dataTimestamp"))
        now = datetime.now(UTC)
        future = new_ts is not None and new_ts > now + timedelta(days=1)
        cached = self._details.get(serial)
        if cached is not None:
            old_ts = parse_epoch(cached.get("dataTimestamp"))
            if old_ts is not None and (new_ts is None or new_ts < old_ts or future):
                return cached, True, False
        if future:
            return detail, True, True
        self._details[serial] = detail
        return detail, False, False

    def _warn_once(self, key: str, message: str, *args: object) -> None:
        """Log a repeated cloud fault once at warning, then at debug."""
        if key in self._warned:
            _LOGGER.debug(message, *args)
            return
        self._warned.add(key)
        _LOGGER.warning(message, *args)

    async def _optional(self, label: str, call: Any) -> tuple[Any, bool]:
        try:
            result = await call()
        except (SolisAuthError, SolisClockError):
            raise
        except SolisCloudError as err:
            self._warn_once(label, "Solis %s failed: %s", label, err)
            return None, True
        self._warned.discard(label)
        return result, False


def _alarms(rows: list[dict[str, Any]]) -> tuple[AlarmSnapshot, ...]:
    return tuple(parse_alarm(row) for row in rows if row.get("alarmDeviceSn") or row.get("sn"))


def _collectors(rows: list[dict[str, Any]]) -> tuple[CollectorSnapshot, ...]:
    return tuple(parse_collector(row) for row in rows if row.get("sn"))


def _epms(rows: list[dict[str, Any]]) -> tuple[EpmSnapshot, ...]:
    return tuple(parse_epm(row) for row in rows if row.get("sn"))
