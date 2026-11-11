"""Local Solarman transport and the local-first router.

A dongle speaks ``read_blocks``. Solarman V5 is the only implementation.
Modbus TCP or a Wi-Fi S3 stick can be added behind the same read method.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import logging
from typing import Protocol

from .const import REGISTER_HYBRID, REGISTER_STRING, SOURCE_BOTH, SOURCE_CLOUD, SOURCE_LOCAL
from .errors import SolisCloudError, SolisLoggerOffline
from .models import (
    CollectorSnapshot,
    InverterSnapshot,
    SolisSnapshot,
    StationSnapshot,
)
from .registers import (
    HYBRID_BLOCKS,
    SERIAL_BLOCK,
    STRING_BLOCKS,
    decode_hybrid,
    decode_string,
    serial_from_words,
)
from .solarman import ModbusException, SolarmanClient, discover_sticks

_LOGGER = logging.getLogger(__name__)

HostCallback = Callable[[str, str], None]


class SnapshotReader(Protocol):
    """Anything that can return a Solis snapshot."""

    async def async_read(self) -> SolisSnapshot:
        """Read one snapshot."""


@dataclass(frozen=True, slots=True)
class LoggerProbe:
    """What a connection test learned about one stick."""

    slave: int
    register_map: str
    inverter_serial: str


def asleep_snapshot(previous: SolisSnapshot) -> SolisSnapshot:
    """Keep energy totals and publish zero live power while the stick is off."""
    now = datetime.now(UTC)
    inverters = tuple(_asleep_inverter(inverter, now) for inverter in previous.inverters)
    return replace(
        previous,
        inverters=inverters,
        fetched_at=now,
        partial=True,
        source="asleep",
    )


def _asleep_inverter(inverter: InverterSnapshot, now: datetime) -> InverterSnapshot:
    return replace(
        inverter,
        state=2,
        data_timestamp=now,
        ac_power_w=0.0,
        grid_power_w=0.0 if inverter.grid_power_w is not None else None,
        consumption_power_w=0.0 if inverter.consumption_power_w is not None else None,
        battery_power_w=0.0 if inverter.battery_power_w is not None else None,
        frequency_hz=None,
        data_source="asleep",
        unreachable=False,
        phases=tuple(replace(phase, power_w=0.0, current_a=0.0) for phase in inverter.phases),
        strings=tuple(replace(pv, power_w=0.0, current_a=0.0) for pv in inverter.strings),
    )


class SolisLocalTransport:
    """Read one Solarman logger. An unreachable stick is asleep, not a fault."""

    def __init__(
        self,
        host: str,
        logger_serial: int,
        *,
        slave: int,
        register_map: str,
        inverter_serial: str,
        mac: str = "",
        timeout: float = 8.0,
        gap: float = 0.35,
        on_host: HostCallback | None = None,
    ) -> None:
        """Store the logger identity. The socket opens on the first poll."""
        self._client = SolarmanClient(host, logger_serial, timeout=timeout, gap=gap)
        self._slave = slave
        self._map = register_map
        self._inverter_serial = inverter_serial or f"L{logger_serial}"
        self._mac = mac
        self._on_host = on_host
        self._last: SolisSnapshot | None = None
        self._asleep_logged = False

    async def async_read(self) -> SolisSnapshot:
        """Return a snapshot, rediscovering the stick when its IP changed."""
        try:
            snapshot = await self._read_connected()
        except SolisLoggerOffline:
            snapshot = await self._after_offline()
        else:
            self._asleep_logged = False
        self._last = snapshot
        return snapshot

    async def _after_offline(self) -> SolisSnapshot:
        found = await self._find_stick()
        if found is not None:
            if found != self._client.host:
                self._client.host = found
                if self._on_host is not None:
                    self._on_host(found, self._mac)
            await self._client.close()
            try:
                recovered = await self._read_connected()
            except SolisLoggerOffline:
                recovered = None
            if recovered is not None:
                self._asleep_logged = False
                return recovered
        if not self._asleep_logged:
            _LOGGER.info(
                "Solarman logger %s is offline; the inverter is asleep",
                self._client.serial,
            )
            self._asleep_logged = True
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._client.serial} is offline")
        return asleep_snapshot(self._last)

    async def _find_stick(self) -> str | None:
        try:
            sticks = await discover_sticks(timeout=1.0)
        except OSError:
            return None
        for stick in sticks:
            if stick.serial == self._client.serial:
                self._mac = stick.mac or self._mac
                return stick.host
        return None

    async def _read_connected(self) -> SolisSnapshot:
        blocks = HYBRID_BLOCKS if self._map == REGISTER_HYBRID else STRING_BLOCKS
        words = await _read_blocks(self._client, self._slave, blocks)
        try:
            address, count = SERIAL_BLOCK
            serial_words = await self._client.read_input(self._slave, address, count)
        except (SolisLoggerOffline, ModbusException, ValueError, TimeoutError):
            serial_words = []
        for offset, value in enumerate(serial_words):
            words[SERIAL_BLOCK[0] + offset] = value
        learned = serial_from_words(words)
        serial = self._inverter_serial or learned or f"L{self._client.serial}"
        logger = str(self._client.serial)
        if self._map == REGISTER_HYBRID:
            inverter = decode_hybrid(words, serial=serial, logger_serial=logger)
        else:
            inverter = decode_string(words, serial=serial, logger_serial=logger)
        now = datetime.now(UTC)
        return SolisSnapshot(
            stations=(
                StationSnapshot(
                    station_id="local",
                    name="Local",
                    timezone_offset_hours=None,
                    day_energy_kwh=None,
                    grid_import_today_kwh=None,
                    grid_export_today_kwh=None,
                    load_today_kwh=None,
                    capacity_kw=None,
                    address=None,
                    latitude=None,
                    longitude=None,
                ),
            ),
            inverters=(inverter,),
            alarms=(),
            collectors=(
                CollectorSnapshot(
                    serial=logger,
                    station_id="local",
                    model="Solarman",
                    state=1,
                    rssi=None,
                ),
            ),
            epms=(),
            fetched_at=now,
            partial=False,
            source="local",
        )

    async def close(self) -> None:
        """Close the logger socket."""
        await self._client.close()


class SolisPriorityTransport:
    """Prefer the local logger. Cloud fills totals, and is the only source in cloud mode."""

    def __init__(
        self,
        mode: str,
        local: SolisLocalTransport | None,
        cloud: SnapshotReader | None,
    ) -> None:
        """``cloud`` is anything with ``async_read``, usually the SolisCloud transport."""
        self._mode = mode
        self._local = local
        self._cloud = cloud

    async def async_read(self) -> SolisSnapshot:
        """Read local first. Night keeps totals and does not raise a repair."""
        if self._mode == SOURCE_CLOUD or self._local is None:
            return _tagged(await _read_cloud(self._cloud), "cloud")
        try:
            local = await self._local.async_read()
        except SolisLoggerOffline:
            local = None
        if local is not None and local.source == "local":
            return local
        cloud: SolisSnapshot | None = None
        if self._mode == SOURCE_BOTH and self._cloud is not None:
            try:
                cloud = _tagged(await _read_cloud(self._cloud), "cloud")
            except SolisCloudError:
                cloud = None
        if local is not None:
            return _fill_missing_energy(local, cloud)
        if self._mode == SOURCE_LOCAL or cloud is None:
            raise SolisLoggerOffline("logger is offline")
        return asleep_snapshot(cloud)


async def probe_logger(
    host: str,
    logger_serial: int,
    *,
    timeout: float = 5.0,
    port: int = 8899,
) -> LoggerProbe:
    """Find the slave id and register map. The inverter must be powered."""
    client = SolarmanClient(host, logger_serial, port=port, timeout=timeout, gap=0.2)
    try:
        for slave in (1, 2, 3):
            register_map = await _detect_map(client, slave)
            if register_map is None:
                continue
            serial = await _optional_serial(client, slave)
            return LoggerProbe(
                slave=slave,
                register_map=register_map,
                inverter_serial=serial or f"L{logger_serial}",
            )
    finally:
        await client.close()
    raise SolisLoggerOffline(f"{host} did not answer as a Solis inverter")


async def _detect_map(client: SolarmanClient, slave: int) -> str | None:
    """Return the register map for this slave, or None when it does not answer."""
    try:
        await client.read_input(slave, 33022, 1)
    except ModbusException as err:
        if err.code != 2:
            return None
    except SolisLoggerOffline:
        return None
    else:
        return REGISTER_HYBRID
    try:
        await client.read_input(slave, 3004, 2)
    except (ModbusException, SolisLoggerOffline, ValueError):
        return None
    return REGISTER_STRING


async def _optional_serial(client: SolarmanClient, slave: int) -> str:
    try:
        words = await client.read_input(slave, SERIAL_BLOCK[0], SERIAL_BLOCK[1])
    except (SolisLoggerOffline, ModbusException, ValueError, TimeoutError):
        return ""
    bank = {SERIAL_BLOCK[0] + offset: value for offset, value in enumerate(words)}
    return serial_from_words(bank)


async def _read_blocks(
    client: SolarmanClient,
    slave: int,
    blocks: tuple[tuple[int, int], ...],
) -> dict[int, int]:
    words: dict[int, int] = {}
    for address, count in blocks:
        values = await client.read_input(slave, address, count)
        for offset, value in enumerate(values):
            words[address + offset] = value
    return words


async def _read_cloud(cloud: SnapshotReader | None) -> SolisSnapshot:
    if cloud is None:
        raise SolisLoggerOffline("no cloud transport")
    snapshot = await cloud.async_read()
    if not isinstance(snapshot, SolisSnapshot):
        raise SolisCloudError("cloud transport returned nothing")
    return snapshot


def _tagged(snapshot: SolisSnapshot, source: str) -> SolisSnapshot:
    return replace(
        snapshot,
        source=source,
        inverters=tuple(replace(inverter, data_source=source) for inverter in snapshot.inverters),
    )


def _fill_missing_energy(local: SolisSnapshot, cloud: SolisSnapshot | None) -> SolisSnapshot:
    """Use cloud counters only where the local snapshot has no number."""
    if cloud is None or not local.inverters or not cloud.inverters:
        return local
    donor = cloud.inverters[0]
    filled = []
    for inverter in local.inverters:
        filled.append(
            replace(
                inverter,
                energy_today_kwh=inverter.energy_today_kwh
                if inverter.energy_today_kwh is not None
                else donor.energy_today_kwh,
                energy_month_kwh=inverter.energy_month_kwh
                if inverter.energy_month_kwh is not None
                else donor.energy_month_kwh,
                energy_year_kwh=inverter.energy_year_kwh
                if inverter.energy_year_kwh is not None
                else donor.energy_year_kwh,
                energy_total_kwh=inverter.energy_total_kwh
                if inverter.energy_total_kwh is not None
                else donor.energy_total_kwh,
            )
        )
    return replace(local, inverters=tuple(filled))
