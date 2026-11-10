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

from datetime import UTC, datetime
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
    parse_inverter,
    parse_offset_hours,
    parse_station,
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

    def __init__(self, client: SolisCloudClient, station_ids: set[str]) -> None:
        """``station_ids`` empty means every station on the account."""
        self._client = client
        self._station_ids = station_ids
        self._details: dict[str, dict[str, Any]] = {}
        self._lists: dict[str, list[dict[str, Any]]] = {}
        self._empty_lists: dict[str, int] = {}

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
            day, day_failed = await self._optional(
                f"station {station_id} day energy",
                lambda station_id=station_id, offset=offset: self._client.station_day_energy(
                    station_id,
                    local_date(offset, now),
                ),
            )
            partial = partial or day_failed
            stations.append(parse_station(summary, detail, day))
            listed, list_failed = await self._inverters_for(station_id)
            partial = partial or list_failed
            for row in listed:
                serial = str(row.get("sn") or "")
                device_id = str(row.get("id") or "")
                detail_row, failed = await self._inverter_detail(serial, device_id, row)
                partial = partial or failed
                if detail_row is None:
                    continue
                parsed = parse_inverter(
                    detail_row,
                    station_id=station_id,
                    name=str(row.get("name") or row.get("sn") or serial),
                )
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
        return SolisSnapshot(
            stations=tuple(stations),
            inverters=tuple(inverters),
            alarms=_alarms(alarms or []),
            collectors=_collectors(collectors or []),
            epms=_epms(epms or []),
            fetched_at=now,
            partial=partial,
        )

    def _wanted(self, row: dict[str, Any]) -> bool:
        if not self._station_ids:
            return True
        return str(row.get("id") or "") in self._station_ids

    async def _inverters_for(self, station_id: str) -> tuple[list[dict[str, Any]], bool]:
        try:
            rows = await self._client.inverter_list(station_id)
        except SolisAuthError, SolisClockError:
            raise
        except SolisCloudError as err:
            cached = self._lists.get(station_id)
            _LOGGER.warning(
                "Inverter list for %s failed (%s); keeping the last list", station_id, err
            )
            if not cached:
                raise
            return cached, True
        if not rows and self._lists.get(station_id):
            self._empty_lists[station_id] = self._empty_lists.get(station_id, 0) + 1
            if self._empty_lists[station_id] < _EMPTY_LIST_TOLERANCE:
                _LOGGER.warning(
                    "Inverter list for %s was empty; keeping the last list (%s/%s)",
                    station_id,
                    self._empty_lists[station_id],
                    _EMPTY_LIST_TOLERANCE,
                )
                return self._lists[station_id], True
        self._empty_lists[station_id] = 0
        self._lists[station_id] = rows
        return rows, False

    async def _inverter_detail(
        self,
        serial: str,
        device_id: str,
        list_row: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        if not serial or not device_id:
            return list_row, True
        try:
            detail = await self._client.inverter_detail(device_id, serial)
        except SolisAuthError, SolisClockError:
            raise
        except SolisCloudError as err:
            _LOGGER.warning("Detail for %s failed (%s); using the last copy", serial, err)
            cached = self._details.get(serial)
            if cached is not None:
                return cached, True
            return list_row, True
        self._details[serial] = detail
        return detail, False

    async def _optional(self, label: str, call: Any) -> tuple[Any, bool]:
        try:
            return await call(), False
        except SolisAuthError, SolisClockError:
            raise
        except SolisCloudError as err:
            _LOGGER.warning("Solis %s failed: %s", label, err)
            return None, True


def _alarms(rows: list[dict[str, Any]]) -> tuple[AlarmSnapshot, ...]:
    return tuple(parse_alarm(row) for row in rows if row.get("alarmDeviceSn") or row.get("sn"))


def _collectors(rows: list[dict[str, Any]]) -> tuple[CollectorSnapshot, ...]:
    return tuple(parse_collector(row) for row in rows if row.get("sn"))


def _epms(rows: list[dict[str, Any]]) -> tuple[EpmSnapshot, ...]:
    return tuple(parse_epm(row) for row in rows if row.get("sn"))
