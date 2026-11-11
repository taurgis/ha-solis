"""Local logger transport and the local-first router.

V5 is tried first. Plain Modbus TCP on the same port, then port 30003, is
used only when V5 connects and does not speak V5. A reset is remembered as
a lock: one attempt, then a pause of at least 10 seconds that doubles up to
about 10 minutes. The 33000 hybrid range is not read once the 3000 map is
known, and registers at 3050 and above are not retried after they fail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import logging
import random
import time
from typing import Protocol

from .const import (
    DATA_DEGRADED,
    LOCAL_REQUEST_GAP,
    MBTCP_ALT_PORT,
    PROTOCOL_MBTCP,
    PROTOCOL_MBTCP_ALT,
    PROTOCOL_V5,
    REGISTER_HYBRID,
    REGISTER_SAFE_CEILING,
    REGISTER_STRING,
    SOURCE_BOTH,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
)
from .errors import SolisCloudError, SolisLoggerBusy, SolisLoggerOffline
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
from .solarman import ModbusException, ModbusTcpClient, SolarmanClient, discover_sticks

_LOGGER = logging.getLogger(__name__)

_BACKOFF_BASE = 10.0
_BACKOFF_CAP = 600.0
_FRESH = timedelta(minutes=15)
_PROTOCOLS = (PROTOCOL_V5, PROTOCOL_MBTCP, PROTOCOL_MBTCP_ALT)

HostCallback = Callable[[str, str], None]
TextCallback = Callable[[str], None]
Reader = SolarmanClient | ModbusTcpClient


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
    protocol: str
    serial_unsupported: bool


def backoff_seconds(step: int) -> float:
    """Pause after a reset. The first wait is at least 10 seconds, the cap 10 minutes."""
    raw = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** min(max(step, 0), 6)))
    jittered = raw * float(random.uniform(0.9, 1.1))
    return float(max(_BACKOFF_BASE, min(_BACKOFF_CAP, jittered)))


def placeholder_serial(serial: str, logger_serial: int) -> bool:
    """Return whether this serial was invented because the stick did not send one."""
    text = serial.strip()
    return not text or text == f"L{logger_serial}"


def asleep_snapshot(previous: SolisSnapshot) -> SolisSnapshot:
    """Keep energy totals and publish zero live power while the stick is off."""
    now = datetime.now(UTC)
    inverters = tuple(_asleep_inverter(inverter, now) for inverter in previous.inverters)
    return replace(previous, inverters=inverters, fetched_at=now, partial=True, source="asleep")


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


def _mark_degraded(previous: SolisSnapshot) -> SolisSnapshot:
    """Hold the last readings. A small morning power value stays; this is not night."""
    inverters = tuple(
        replace(inverter, data_source=DATA_DEGRADED) for inverter in previous.inverters
    )
    return replace(previous, inverters=inverters, partial=True, source=DATA_DEGRADED)


class SolisLocalTransport:
    """Read one logger. Resets back off. A dark stick is asleep, not a fault."""

    def __init__(  # noqa: PLR0913
        self,
        host: str,
        logger_serial: int,
        *,
        slave: int,
        register_map: str,
        inverter_serial: str,
        mac: str = "",
        protocol: str = "",
        serial_unsupported: bool = False,
        timeout: float = 8.0,
        gap: float = LOCAL_REQUEST_GAP,
        port: int = 8899,
        on_host: HostCallback | None = None,
        on_protocol: TextCallback | None = None,
        on_serial: TextCallback | None = None,
    ) -> None:
        """Store the logger identity. The socket opens on the first poll."""
        self._host = host
        self._logger_serial = int(logger_serial)
        self._slave = slave
        self._map = register_map
        self._inverter_serial = inverter_serial or f"L{logger_serial}"
        self._mac = mac
        self._protocol = protocol if protocol in _PROTOCOLS else ""
        self._serial_unsupported = serial_unsupported
        self._timeout = timeout
        self._gap = gap
        self._port = port
        self._on_host = on_host
        self._on_protocol = on_protocol
        self._on_serial = on_serial
        self._active = self._protocol or PROTOCOL_V5
        self._client: Reader | None = _client(
            host,
            int(logger_serial),
            self._active,
            port=port,
            timeout=timeout,
            gap=gap,
        )
        self._last: SolisSnapshot | None = None
        self._asleep_logged = False
        self._busy_logged = False
        self._next_attempt = 0.0
        self._backoff_step = 0
        self._has_series = False
        self._identity_checked = False

    async def async_read(self) -> SolisSnapshot:
        """Return a snapshot. A locked logger raises ``SolisLoggerBusy``."""
        if time.monotonic() < self._next_attempt:
            raise SolisLoggerBusy(f"{self._host} is in backoff", waiting=True)
        try:
            snapshot = await self._read_connected()
        except SolisLoggerBusy as err:
            if not err.waiting:
                self._arm_backoff()
            raise
        except SolisLoggerOffline as err:
            snapshot = await self._after_offline(err)
        else:
            self._clear_backoff()
            self._asleep_logged = False
            self._busy_logged = False
        self._last = snapshot
        return snapshot

    def _arm_backoff(self) -> float:
        delay = backoff_seconds(self._backoff_step)
        self._backoff_step += 1
        self._next_attempt = time.monotonic() + delay
        if not self._busy_logged:
            _LOGGER.info(
                "Solarman logger %s reset the local session; next try in %s seconds",
                self._logger_serial,
                int(delay),
            )
            self._busy_logged = True
        return delay

    def _clear_backoff(self) -> None:
        self._backoff_step = 0
        self._next_attempt = 0.0

    async def _after_offline(self, err: SolisLoggerOffline | None = None) -> SolisSnapshot:
        found = await self._find_stick()
        if found is not None and found != self._host:
            self._adopt_host(found)
            try:
                recovered = await self._read_connected()
            except SolisLoggerBusy:
                raise
            except SolisLoggerOffline:
                recovered = None
            if recovered is not None:
                self._clear_backoff()
                self._asleep_logged = False
                return recovered
        if found == self._host and err is not None and err.refused:
            raise SolisLoggerBusy(f"{self._host} refused the port")
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._logger_serial} is offline")
        return asleep_snapshot(self._last)

    def _adopt_host(self, host: str) -> None:
        self._host = host
        if self._client is not None:
            self._client.host = host
        if self._on_host is not None:
            self._on_host(host, self._mac)

    def _log_asleep(self) -> None:
        if self._asleep_logged:
            return
        _LOGGER.info("Solarman logger %s is offline; the inverter is asleep", self._logger_serial)
        self._asleep_logged = True

    async def _find_stick(self) -> str | None:
        try:
            sticks = await discover_sticks(timeout=1.0)
        except OSError:
            return None
        for stick in sticks:
            if stick.serial == self._logger_serial:
                self._mac = stick.mac or self._mac
                return stick.host
        return None

    async def _read_connected(self) -> SolisSnapshot:
        words = await self._read_words()
        serial = self._inverter_serial or f"L{self._logger_serial}"
        logger = str(self._logger_serial)
        if self._map == REGISTER_HYBRID:
            inverter = decode_hybrid(words, serial=serial, logger_serial=logger)
        else:
            inverter = decode_string(words, serial=serial, logger_serial=logger)
        return _local_snapshot(inverter, logger, datetime.now(UTC))

    async def _read_words(self) -> dict[int, int]:
        last_offline: SolisLoggerOffline | None = None
        for protocol in self._candidates():
            client = await self._open(protocol)
            try:
                words = await _read_blocks(client, self._slave, self._blocks())
            except SolisLoggerBusy:
                await self._drop()
                raise
            except SolisLoggerOffline as err:
                await self._drop()
                last_offline = err
                if self._protocol or not err.connected:
                    raise
                continue
            self._remember(protocol)
            return words
        if last_offline is not None:
            raise last_offline
        raise SolisLoggerOffline(f"{self._host} did not answer")

    def _candidates(self) -> tuple[str, ...]:
        if self._protocol in _PROTOCOLS:
            return (self._protocol,)
        return _PROTOCOLS

    def _blocks(self) -> tuple[tuple[int, int], ...]:
        if self._map == REGISTER_HYBRID:
            return HYBRID_BLOCKS
        return tuple(block for block in STRING_BLOCKS if block[0] < REGISTER_SAFE_CEILING)

    async def _open(self, protocol: str) -> Reader:
        if self._client is not None and self._active == protocol:
            return self._client
        await self._drop()
        if protocol == PROTOCOL_MBTCP_ALT:
            client: Reader = ModbusTcpClient(
                self._host, port=MBTCP_ALT_PORT, timeout=self._timeout, gap=self._gap
            )
        elif protocol == PROTOCOL_MBTCP:
            client = ModbusTcpClient(
                self._host, port=self._port, timeout=self._timeout, gap=self._gap
            )
        else:
            client = SolarmanClient(
                self._host,
                self._logger_serial,
                port=self._port,
                timeout=self._timeout,
                gap=self._gap,
            )
        self._client = client
        self._active = protocol
        return client

    def _remember(self, protocol: str) -> None:
        if self._protocol == protocol:
            return
        self._protocol = protocol
        if self._on_protocol is not None:
            self._on_protocol(protocol)

    async def _drop(self) -> None:
        """Close the socket and keep the client so the next read can reconnect."""
        if self._client is None:
            return
        self._port = self._client._port
        await self._client.close()

    def adopt_serial(self, serial: str) -> None:
        """Use the cloud or user serial so later snapshots share entity ids."""
        if not serial or serial == self._inverter_serial:
            return
        self._inverter_serial = serial
        if self._on_serial is not None:
            self._on_serial(serial)

    def note_identity(self, donor: InverterSnapshot) -> None:
        """Keep a series and model code learned from SolisCloud."""
        if donor.model or donor.model_code:
            self._has_series = True

    @property
    def inverter_serial(self) -> str:
        """Serial currently used for entity ids."""
        return self._inverter_serial

    @property
    def logger_serial(self) -> int:
        """Numeric serial printed on the stick."""
        return self._logger_serial

    @property
    def last_snapshot(self) -> SolisSnapshot | None:
        """Last good local snapshot, if this process has read one."""
        return self._last

    @property
    def needs_cloud_identity(self) -> bool:
        """True until a real serial or a series name has been learned."""
        missing_serial = placeholder_serial(self._inverter_serial, self._logger_serial)
        return missing_serial or not self._has_series

    async def close(self) -> None:
        """Close the logger socket."""
        await self._drop()


class SolisPriorityTransport:
    """Prefer the local logger. A lock with a fresh cloud sample is degraded, not asleep."""

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
        """Read local first. Night keeps totals. A reset uses the cloud when it is fresh."""
        if self._mode == SOURCE_CLOUD or self._local is None:
            return _tagged(await _read_cloud(self._cloud), "cloud")
        try:
            local = await self._local.async_read()
        except SolisLoggerBusy:
            return await self._when_busy()
        except SolisLoggerOffline:
            local = None
        if local is not None and local.source == "local":
            return await self._with_identity(local)
        if local is not None:
            return _fill_missing_energy(local, await self._optional_cloud())
        if self._mode == SOURCE_LOCAL:
            raise SolisLoggerOffline("logger is offline")
        cloud = await self._optional_cloud()
        if cloud is None:
            raise SolisLoggerOffline("logger is offline")
        return asleep_snapshot(cloud)

    async def _when_busy(self) -> SolisSnapshot:
        local = self._local
        assert local is not None
        if self._mode == SOURCE_BOTH:
            cloud = await self._optional_cloud()
            if cloud is not None and _cloud_is_fresh(cloud):
                return _degraded_cloud(local, cloud)
        if local.last_snapshot is not None:
            return _mark_degraded(local.last_snapshot)
        raise SolisLoggerBusy(f"{local.inverter_serial} local session is paused")

    async def _with_identity(self, local: SolisSnapshot) -> SolisSnapshot:
        """One cloud read can supply the inverter serial, series and model code."""
        transport = self._local
        if (
            transport is None
            or self._mode != SOURCE_BOTH
            or transport._identity_checked
            or not transport.needs_cloud_identity
        ):
            return local
        transport._identity_checked = True
        cloud = await self._optional_cloud()
        if cloud is None or not cloud.inverters or not local.inverters:
            return local
        donor = cloud.inverters[0]
        serial = transport.inverter_serial
        if placeholder_serial(serial, transport.logger_serial) and donor.serial:
            transport.adopt_serial(donor.serial)
            serial = donor.serial
        elif donor.model or donor.model_code:
            transport.note_identity(donor)
        current = local.inverters[0]
        named = replace(
            current,
            serial=serial,
            device_id=serial,
            model=donor.model or current.model,
            model_code=donor.model_code if donor.model_code is not None else current.model_code,
            hmi_version=current.hmi_version or donor.hmi_version,
        )
        return replace(local, inverters=(named, *local.inverters[1:]))

    async def _optional_cloud(self) -> SolisSnapshot | None:
        if self._cloud is None:
            return None
        try:
            return _tagged(await _read_cloud(self._cloud), "cloud")
        except SolisCloudError:
            return None

    async def close(self) -> None:
        """Close the local socket. The cloud client uses Home Assistant's session."""
        if self._local is not None:
            await self._local.close()


def _cloud_is_fresh(snapshot: SolisSnapshot) -> bool:
    if not snapshot.inverters:
        return False
    moment = snapshot.inverters[0].data_timestamp
    if moment is None:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return datetime.now(UTC) - moment <= _FRESH


def _prefer(previous: float | None, incoming: float | None) -> float | None:
    if previous is None:
        return incoming
    if incoming is None or incoming < previous:
        return previous
    return incoming


def _degraded_cloud(local: SolisLocalTransport, cloud: SolisSnapshot) -> SolisSnapshot:
    """Publish the cloud sample on the local inverter serial."""
    donor = cloud.inverters[0]
    previous = (
        local.last_snapshot.inverters[0]
        if local.last_snapshot and local.last_snapshot.inverters
        else None
    )
    serial = local.inverter_serial
    if placeholder_serial(serial, local.logger_serial) and donor.serial:
        local.adopt_serial(donor.serial)
        serial = donor.serial
    model = donor.model or (previous.model if previous else None)
    code = donor.model_code
    if code is None and previous is not None:
        code = previous.model_code
    version = donor.hmi_version
    if previous is not None and previous.hmi_version:
        version = previous.hmi_version
    inverter = replace(
        donor,
        serial=serial,
        device_id=serial,
        name=serial,
        station_id=previous.station_id if previous else donor.station_id,
        model=model,
        model_code=code,
        hmi_version=version,
        energy_today_kwh=_prefer(
            previous.energy_today_kwh if previous else None, donor.energy_today_kwh
        ),
        energy_month_kwh=_prefer(
            previous.energy_month_kwh if previous else None, donor.energy_month_kwh
        ),
        energy_year_kwh=_prefer(
            previous.energy_year_kwh if previous else None, donor.energy_year_kwh
        ),
        energy_total_kwh=_prefer(
            previous.energy_total_kwh if previous else None, donor.energy_total_kwh
        ),
        data_source=DATA_DEGRADED,
    )
    return replace(
        cloud,
        inverters=(inverter,),
        source=DATA_DEGRADED,
        fetched_at=datetime.now(UTC),
    )


def _local_snapshot(inverter: InverterSnapshot, logger: str, now: datetime) -> SolisSnapshot:
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


async def probe_logger(
    host: str,
    logger_serial: int,
    *,
    timeout: float = 5.0,
    port: int = 8899,
    inverter_serial: str = "",
    serial_unsupported: bool = False,
    protocol: str = "",
    gap: float = LOCAL_REQUEST_GAP,
) -> LoggerProbe:
    """Find the framing, slave and register map. One attempt, then stop on a reset."""
    order = (protocol,) if protocol in _PROTOCOLS else _PROTOCOLS
    last_offline: SolisLoggerOffline | None = None
    for framing in order:
        client = _client(host, logger_serial, framing, port=port, timeout=timeout, gap=gap)
        try:
            for slave in (1, 2, 3):
                register_map = await _detect_map(client, slave)
                if register_map is None:
                    continue
                learned, unsupported = await _serial_once(
                    client,
                    slave,
                    known=inverter_serial,
                    unsupported=serial_unsupported,
                    logger_serial=logger_serial,
                )
                return LoggerProbe(
                    slave=slave,
                    register_map=register_map,
                    inverter_serial=learned,
                    protocol=framing,
                    serial_unsupported=unsupported,
                )
        except SolisLoggerBusy:
            raise
        except SolisLoggerOffline as err:
            last_offline = err
            if not err.connected:
                raise
        finally:
            await client.close()
    if last_offline is not None:
        raise last_offline
    raise SolisLoggerOffline(f"{host} did not answer as a Solis inverter")


def _client(
    host: str,
    logger_serial: int,
    protocol: str,
    *,
    port: int,
    timeout: float,
    gap: float,
) -> Reader:
    if protocol == PROTOCOL_MBTCP_ALT:
        return ModbusTcpClient(host, port=MBTCP_ALT_PORT, timeout=timeout, gap=gap)
    if protocol == PROTOCOL_MBTCP:
        return ModbusTcpClient(host, port=port, timeout=timeout, gap=gap)
    return SolarmanClient(host, logger_serial, port=port, timeout=timeout, gap=gap)


async def _detect_map(client: Reader, slave: int) -> str | None:
    """Prefer the 3000-range map. Probe 33022 only when 3004 is an illegal address."""
    try:
        await client.read_input(slave, 3004, 2)
    except ModbusException as err:
        if err.code != 2:
            return None
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline:
        return None
    else:
        return REGISTER_STRING
    try:
        await client.read_input(slave, 33022, 1)
    except ModbusException:
        return None
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline:
        return None
    return REGISTER_HYBRID


async def _serial_once(
    client: Reader,
    slave: int,
    *,
    known: str,
    unsupported: bool,
    logger_serial: int,
) -> tuple[str, bool]:
    """Read 3060 once. A failure is cached so the poll never scans that range again."""
    if known and not placeholder_serial(known, logger_serial):
        return known, unsupported
    if unsupported:
        return known or f"L{logger_serial}", True
    try:
        words = await client.read_input(slave, SERIAL_BLOCK[0], SERIAL_BLOCK[1])
    except SolisLoggerBusy:
        return known or f"L{logger_serial}", True
    except (SolisLoggerOffline, ModbusException, ValueError, TimeoutError):
        return known or f"L{logger_serial}", True
    bank = {SERIAL_BLOCK[0] + offset: value for offset, value in enumerate(words)}
    learned = serial_from_words(bank)
    return learned or known or f"L{logger_serial}", not bool(learned)


async def _read_blocks(
    client: Reader,
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
