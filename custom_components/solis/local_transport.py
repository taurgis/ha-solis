"""Local logger transport and the local-first router.

V5 is tried first. Plain Modbus TCP on the same port, then port 30003, is
used only when V5 connects and does not speak V5. A reset is remembered as
a lock: one attempt, then a pause of at least 10 seconds that doubles up to
about 10 minutes. The 33000 hybrid range is not read once the 3000 map is
known, and registers at 3050 and above are not retried after they fail.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import logging
import random
import time
from typing import Any, Protocol

from .const import (
    CONF_INVERTER_SERIAL,
    CONF_LOGGER_DETECT,
    CONF_LOGGER_FUNCTION,
    CONF_LOGGER_HOST,
    CONF_LOGGER_KIND,
    CONF_LOGGER_MAC,
    CONF_LOGGER_PORT,
    CONF_LOGGER_PRESET,
    CONF_LOGGER_PROTOCOL,
    CONF_LOGGER_SERIAL,
    CONF_REGISTER_MAP,
    CONF_SERIAL_UNSUPPORTED,
    CONF_SLAVE_ID,
    CONF_SOURCE,
    DATA_DEGRADED,
    LOCAL_REQUEST_GAP,
    LOGGER_AUTO,
    LOGGER_MBTCP,
    LOGGER_RTU,
    LOGGER_S2,
    LOGGER_S5,
    LOGGER_SOLARMAN,
    MBTCP_ALT_PORT,
    PROTOCOL_MBTCP,
    PROTOCOL_MBTCP_ALT,
    PROTOCOL_RTU,
    PROTOCOL_V5,
    REASON_CLOSED,
    REASON_GARBAGE,
    REASON_GATEWAY,
    REASON_LOCKED,
    REASON_REFUSED,
    REASON_S5_CLOSED,
    REASON_SILENT,
    REASON_TCP_DISABLED,
    REASON_TIMEOUT,
    REASON_UNSUPPORTED,
    REASON_WRONG_PROTOCOL,
    REASON_WRONG_SLAVE,
    REGISTER_HYBRID,
    REGISTER_SAFE_CEILING,
    REGISTER_STRING,
    S2_PORT,
    SOLARMAN_PORT,
    SOURCE_BOTH,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
)
from .errors import SolisCloudError, SolisLoggerBusy, SolisLoggerOffline
from .loggers import detection_code, spec_for
from .loggers.rtu import RtuTcpClient
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
_KNOWN_PROTOCOLS = (*_PROTOCOLS, PROTOCOL_RTU)
_FC3_KINDS = {LOGGER_S2, LOGGER_S5, LOGGER_MBTCP, LOGGER_RTU}

HostCallback = Callable[[str, str], None]
TextCallback = Callable[[str], None]


class RegisterReader(Protocol):
    """A framed Modbus client. V5, MBAP and raw RTU all look like this."""

    host: str
    _port: int

    async def read_input(self, slave: int, address: int, count: int) -> list[int]: ...

    async def close(self) -> None: ...


Reader = RegisterReader


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
    kind: str = ""
    port: int = 0
    function: int = 4
    needs_choice: bool = False
    suggested_kind: str = ""
    identify_note: str = ""
    confidence: str = ""
    detection: str = ""


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
        port: int | None = None,
        on_host: HostCallback | None = None,
        on_protocol: TextCallback | None = None,
        on_serial: TextCallback | None = None,
        kind: str = "",
        stick_serial: str = "",
        function: int = 4,
    ) -> None:
        """Store the logger identity. The socket opens on the first poll."""
        self._host = host
        self._logger_serial = int(logger_serial)
        self._spec = spec_for(kind)
        self._kind = self._spec.kind
        self._stick_serial = stick_serial.strip()
        self._function = 3 if function == 3 else 4
        self._slave = slave
        self._map = register_map
        self._inverter_serial = inverter_serial or f"L{logger_serial}"
        self._mac = mac
        self._protocol = protocol if protocol in _KNOWN_PROTOCOLS else ""
        self._serial_unsupported = serial_unsupported
        self._timeout = timeout
        self._gap = gap
        self._port = port if port else self._spec.default_port
        self._on_host = on_host
        self._on_protocol = on_protocol
        self._on_serial = on_serial
        self.degraded_reason = ""
        self._active = self._protocol or self._spec.protocol
        self._client: Reader | None = _client(
            host,
            int(logger_serial),
            self._active,
            port=self._port,
            timeout=timeout,
            gap=gap,
            function=self._function if self._kind in _FC3_KINDS else 4,
            fallback=3 if self._kind in _FC3_KINDS and self._function == 4 else 0,
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
            raise SolisLoggerBusy(
                f"{self._host} is in backoff",
                waiting=True,
                reason=self.degraded_reason or REASON_LOCKED,
            )
        try:
            snapshot = await self._read_connected()
        except SolisLoggerBusy as err:
            if not err.waiting:
                self.degraded_reason = err.reason or REASON_LOCKED
                self._arm_backoff()
            raise
        except SolisLoggerOffline as err:
            snapshot = await self._after_offline(err)
        else:
            self._clear_backoff()
            self.degraded_reason = ""
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
                "%s",
                _reset_log(self._kind, self._spec.label, self._stick_label(), int(delay)),
            )
            self._busy_logged = True
        return delay

    def _clear_backoff(self) -> None:
        self._backoff_step = 0
        self._next_attempt = 0.0

    async def _after_offline(self, err: SolisLoggerOffline | None = None) -> SolisSnapshot:
        if self._kind in {LOGGER_S2, LOGGER_S5}:
            return await self._s2_offline(err)
        if self._spec.bridge:
            return await self._bridge_offline(err)
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
        _LOGGER.info("%s", _asleep_log(self._kind, self._spec.label, self._stick_label()))
        self._asleep_logged = True

    async def _s2_offline(self, err: SolisLoggerOffline | None) -> SolisSnapshot:
        """A closed port or a silent socket is a firmware fault, not night."""
        failure = _s2_busy(self._host, err)
        if (
            failure is not None
            and self._kind == LOGGER_S5
            and failure.reason == REASON_TCP_DISABLED
        ):
            failure = SolisLoggerBusy(str(failure), reason=REASON_S5_CLOSED)
        if failure is not None:
            self.degraded_reason = failure.reason
            self._arm_backoff()
            raise failure
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._stick_label()} is offline")
        return asleep_snapshot(self._last)

    async def _bridge_offline(self, err: SolisLoggerOffline | None) -> SolisSnapshot:
        """A refused gateway is a fault. A timeout after TCP opens is the inverter."""
        failure = _bridge_busy(self._host, err)
        if failure is not None:
            self.degraded_reason = failure.reason
            self._arm_backoff()
            raise failure
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._stick_label()} is offline")
        return asleep_snapshot(self._last)

    def _stick_label(self) -> str:
        if self._kind == LOGGER_SOLARMAN:
            return str(self._logger_serial)
        return self._stick_serial or self._inverter_serial or self._host

    async def _find_stick(self) -> str | None:
        if self._kind != LOGGER_SOLARMAN:
            return None
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
        logger = self._stick_label()
        if self._map == REGISTER_HYBRID:
            inverter = decode_hybrid(words, serial=serial, logger_serial=logger)
        else:
            inverter = decode_string(words, serial=serial, logger_serial=logger)
        return _local_snapshot(inverter, logger, datetime.now(UTC), self._spec.label)

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
        if self._kind in {LOGGER_S2, LOGGER_S5, LOGGER_MBTCP}:
            return (PROTOCOL_MBTCP,)
        if self._kind == LOGGER_RTU:
            return (PROTOCOL_RTU,)
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
        if protocol == PROTOCOL_RTU:
            client: Reader = RtuTcpClient(
                self._host,
                port=self._port,
                timeout=self._timeout,
                gap=self._gap,
                function=self._function,
                fallback_function=_fc3_fallback(self._kind, self._function),
            )
        elif protocol == PROTOCOL_MBTCP_ALT:
            client = ModbusTcpClient(
                self._host, port=MBTCP_ALT_PORT, timeout=self._timeout, gap=self._gap
            )
        elif protocol == PROTOCOL_MBTCP:
            client = ModbusTcpClient(
                self._host,
                port=self._port,
                timeout=self._timeout,
                gap=self._gap,
                function=self._function if self._kind in _FC3_KINDS else 4,
                fallback_function=_fc3_fallback(self._kind, self._function),
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

    def session_info(self) -> dict[str, str]:
        """Kind and framing for diagnostics. Addresses and serials stay out."""
        return {
            "kind": self._kind,
            "protocol": self._protocol or self._active,
            "port": str(self._port),
            "slave": str(self._slave),
            "register_map": self._map,
            "function": str(self._function),
            "experimental": "true" if self._spec.experimental else "false",
            "bridge": "true" if self._spec.bridge else "false",
        }


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
        raise SolisLoggerBusy(
            f"{local.inverter_serial} local session is paused",
            reason=local.degraded_reason or REASON_LOCKED,
        )

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

    @property
    def degraded_reason(self) -> str:
        """Why the local session is paused, when it is."""
        if self._local is None:
            return ""
        return self._local.degraded_reason

    def session_info(self) -> dict[str, str]:
        """Kind and framing for diagnostics. Addresses and serials stay out."""
        if self._local is None:
            return {"transport": "cloud"}
        info = self._local.session_info()
        info["transport"] = "local"
        info["mode"] = self._mode
        return info


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


def _local_snapshot(
    inverter: InverterSnapshot, logger: str, now: datetime, model: str = "Solarman"
) -> SolisSnapshot:
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
                model=model,
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
    only_slave: int | None = None,
) -> LoggerProbe:
    """Find the framing, slave and register map. One attempt, then stop on a reset."""
    order = (protocol,) if protocol in _PROTOCOLS else _PROTOCOLS
    slaves = (only_slave,) if only_slave is not None else (1, 2, 3)
    last_offline: SolisLoggerOffline | None = None
    for framing in order:
        client = _client(host, logger_serial, framing, port=port, timeout=timeout, gap=gap)
        try:
            for slave in slaves:
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
    function: int = 4,
    fallback: int = 0,
) -> Reader:
    if protocol == PROTOCOL_RTU:
        return RtuTcpClient(
            host,
            port=port,
            timeout=timeout,
            gap=gap,
            function=function,
            fallback_function=fallback,
        )
    if protocol == PROTOCOL_MBTCP_ALT:
        return ModbusTcpClient(host, port=MBTCP_ALT_PORT, timeout=timeout, gap=gap)
    if protocol == PROTOCOL_MBTCP:
        return ModbusTcpClient(
            host,
            port=port,
            timeout=timeout,
            gap=gap,
            function=function,
            fallback_function=fallback,
        )
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
    except SolisLoggerOffline as err:
        if err.refused or err.reason:
            raise
        return None
    else:
        return REGISTER_STRING
    try:
        await client.read_input(slave, 33022, 1)
    except ModbusException:
        return None
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        if err.refused or err.reason:
            raise
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


@dataclass(frozen=True, slots=True)
class LoggerRequest:
    """One config-flow attempt to identify a stick."""

    host: str
    kind: str = LOGGER_AUTO
    port: int | None = None
    slave: int | None = None
    logger_serial: str = ""
    inverter_serial: str = ""
    timeout: float = 5.0
    gap: float = LOCAL_REQUEST_GAP
    preset: str = ""


def parse_logger_form(user_input: Mapping[str, Any]) -> tuple[LoggerRequest | None, str]:
    """Read the logger form. An empty string is the error key."""
    from .loggers import SELECTABLE_KINDS

    host = str(user_input.get(CONF_LOGGER_HOST) or "").strip()
    if not host:
        return None, "logger_asleep"
    kind = str(user_input.get(CONF_LOGGER_KIND) or LOGGER_AUTO).strip() or LOGGER_AUTO
    if kind not in SELECTABLE_KINDS:
        kind = LOGGER_AUTO
    port, port_error = _optional_port(str(user_input.get(CONF_LOGGER_PORT) or ""))
    if port_error:
        return None, port_error
    slave, slave_error = _optional_slave(str(user_input.get(CONF_SLAVE_ID) or ""))
    if slave_error:
        return None, slave_error
    serial = str(user_input.get(CONF_LOGGER_SERIAL) or "").strip()
    inverter = str(user_input.get(CONF_INVERTER_SERIAL) or "").strip()
    preset = str(user_input.get(CONF_LOGGER_PRESET) or "").strip()
    if kind == LOGGER_SOLARMAN and not serial.isdigit():
        return None, "invalid_logger"
    return LoggerRequest(host, kind, port, slave, serial, inverter, preset=preset), ""


def _optional_port(text: str) -> tuple[int | None, str]:
    cleaned = text.strip()
    if not cleaned:
        return None, ""
    if not cleaned.isdigit() or not 1 <= int(cleaned) <= 65535:
        return None, "invalid_logger"
    return int(cleaned), ""


def _optional_slave(text: str) -> tuple[int | None, str]:
    cleaned = text.strip()
    if not cleaned:
        return None, ""
    if not cleaned.isdigit() or not 1 <= int(cleaned) <= 247:
        return None, "invalid_logger"
    return int(cleaned), ""


def logger_form_error(err: BaseException) -> str:
    """Map a probe failure onto a config-flow error key."""
    reason = str(getattr(err, "reason", "") or "")
    if reason == REASON_TCP_DISABLED:
        return "logger_tcp_disabled"
    if reason == REASON_SILENT:
        return "logger_modbus_silent"
    if reason == REASON_GATEWAY:
        return "logger_gateway"
    if reason == REASON_WRONG_PROTOCOL:
        return "logger_wrong_protocol"
    if reason == REASON_WRONG_SLAVE:
        return "logger_wrong_slave"
    if reason == REASON_UNSUPPORTED:
        return "logger_unsupported_map"
    if reason == REASON_TIMEOUT:
        return "logger_timeout"
    if isinstance(err, SolisLoggerBusy):
        return "logger_busy"
    return "logger_asleep"


def s2_needs_identity(probe: LoggerProbe, stick_serial: str, inverter_serial: str) -> bool:
    """An S2, S5 or gateway entry needs the stick label or a real inverter serial."""
    return needs_stable_id(probe, stick_serial, inverter_serial)


def needs_stable_id(probe: LoggerProbe, stick_serial: str, inverter_serial: str) -> bool:
    """Solarman uses the numeric logger serial. Every other kind needs another token."""
    if not spec_for(probe.kind or LOGGER_SOLARMAN).unique_prefix:
        return False
    token = stick_serial.strip() or inverter_serial.strip() or probe.inverter_serial.strip()
    numeric = int(stick_serial) if stick_serial.isdigit() else 0
    return placeholder_serial(token, numeric)


def logger_setup(
    request: LoggerRequest, probe: LoggerProbe, *, mac: str, source: str
) -> tuple[dict[str, Any], str]:
    """Return the config-entry fields and the stable unique id."""
    kind = probe.kind or LOGGER_SOLARMAN
    spec = spec_for(kind)
    inverter = request.inverter_serial.strip() or probe.inverter_serial
    if spec.unique_prefix:
        stick = request.logger_serial.strip()
        token = stick or inverter
        unique = f"{spec.unique_prefix}:{token}"
        serial_field = stick
        port = probe.port or spec.default_port
    else:
        unique = request.logger_serial.strip()
        serial_field = unique
        port = probe.port or SOLARMAN_PORT
    data = {
        CONF_LOGGER_KIND: kind,
        CONF_LOGGER_HOST: request.host,
        CONF_LOGGER_PORT: port,
        CONF_LOGGER_SERIAL: serial_field,
        CONF_LOGGER_MAC: mac,
        CONF_SLAVE_ID: probe.slave,
        CONF_REGISTER_MAP: probe.register_map,
        CONF_INVERTER_SERIAL: inverter,
        CONF_LOGGER_PROTOCOL: probe.protocol,
        CONF_SERIAL_UNSUPPORTED: probe.serial_unsupported,
        CONF_LOGGER_FUNCTION: probe.function,
        CONF_LOGGER_DETECT: probe.detection or detection_code(request.kind, kind),
        CONF_LOGGER_PRESET: request.preset,
        CONF_SOURCE: source,
    }
    return data, unique


def _fc3_fallback(kind: str, function: int) -> int:
    """Solis sticks and gateways retry function 3 after illegal function."""
    if kind in _FC3_KINDS and function == 4:
        return 3
    return 0


def _reset_log(kind: str, label: str, token: str, delay: int) -> str:
    """Keep the sentences existing sticks already log."""
    if kind == LOGGER_S2:
        return f"S2-WL-ST {token} reset the local session; next try in {delay} seconds"
    if kind == LOGGER_SOLARMAN:
        return f"Solarman logger {token} reset the local session; next try in {delay} seconds"
    return f"{label} {token} reset the local session; next try in {delay} seconds"


def _asleep_log(kind: str, label: str, token: str) -> str:
    """Keep the sentences existing sticks already log."""
    if kind == LOGGER_S2:
        return f"S2-WL-ST {token} is offline; the inverter is asleep"
    if kind == LOGGER_SOLARMAN:
        return f"Solarman logger {token} is offline; the inverter is asleep"
    return f"{label} {token} is offline; the inverter is asleep"


def _bridge_busy(host: str, err: SolisLoggerOffline | None) -> SolisLoggerBusy | None:
    """A gateway that is down or speaking the wrong framing is paused, not night."""
    if err is None:
        return None
    if err.reason in {
        REASON_GATEWAY,
        REASON_WRONG_PROTOCOL,
        REASON_WRONG_SLAVE,
        REASON_UNSUPPORTED,
    }:
        return SolisLoggerBusy(str(err), reason=err.reason)
    if err.refused or not err.connected:
        return SolisLoggerBusy(
            f"{host} gateway is not accepting connections",
            reason=REASON_GATEWAY,
        )
    if err.reason in {REASON_GARBAGE, REASON_WRONG_PROTOCOL}:
        return SolisLoggerBusy(str(err), reason=REASON_WRONG_PROTOCOL)
    return None


def _s2_busy(host: str, err: SolisLoggerOffline | None) -> SolisLoggerBusy | None:
    """Turn a closed or silent S2 socket into a paused session."""
    if err is None:
        return None
    if err.refused or err.reason in {REASON_REFUSED, REASON_TCP_DISABLED}:
        return SolisLoggerBusy(f"{host} port {S2_PORT} is closed", reason=REASON_TCP_DISABLED)
    if err.connected and err.reason in {REASON_TIMEOUT, REASON_GARBAGE, REASON_SILENT}:
        return SolisLoggerBusy(f"{host} accepted TCP and sent no Modbus", reason=REASON_SILENT)
    if err.reason == REASON_CLOSED or not err.connected:
        return None
    return None


async def identify_logger(
    request: LoggerRequest, *, serial_unsupported: bool = False
) -> LoggerProbe:
    """Probe port 502, then 8899, unless the form already names the stick."""
    kind = request.kind or LOGGER_AUTO
    if kind == LOGGER_S5:
        probe = await _probe_s2(request, serial_unsupported=serial_unsupported)
        return replace(probe, kind=LOGGER_S5)
    if kind in {LOGGER_MBTCP, LOGGER_RTU}:
        from .loggers.probe import probe_bridge

        return await probe_bridge(request, serial_unsupported=serial_unsupported)
    if kind == LOGGER_S2:
        return await _probe_s2(request, serial_unsupported=serial_unsupported)
    if kind == LOGGER_AUTO and request.port == S2_PORT:
        return await _auto_s2(request, serial_unsupported=serial_unsupported)
    if kind == LOGGER_SOLARMAN or (kind == LOGGER_AUTO and request.port not in (None, 0)):
        return await _probe_named_solarman(request, serial_unsupported=serial_unsupported)
    try:
        faster = replace(request, port=S2_PORT, timeout=min(request.timeout, 3.0))
        return await _auto_s2(faster, serial_unsupported=serial_unsupported)
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        s2_error: SolisLoggerOffline | None = err
    try:
        return await _probe_named_solarman(
            replace(request, port=SOLARMAN_PORT), serial_unsupported=serial_unsupported
        )
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        raise _prefer_auto_error(s2_error, err) from err


async def _auto_s2(request: LoggerRequest, *, serial_unsupported: bool) -> LoggerProbe:
    """A Solis map on port 502 is not enough. Port 80 has to name the device."""
    from .loggers.fingerprint import classify_http, read_http_banner

    probe = await _probe_s2(request, serial_unsupported=serial_unsupported)
    page = await read_http_banner(request.host, timeout=min(request.timeout, 1.2))
    found = classify_http(page)
    if found.confident_kind:
        return replace(
            probe,
            kind=found.confident_kind,
            confidence=found.confidence,
            identify_note=found.note,
            detection=found.detection,
        )
    return replace(
        probe,
        needs_choice=True,
        suggested_kind=found.suggested,
        confidence=found.confidence,
        identify_note=found.note,
    )


def _prefer_auto_error(
    s2_error: SolisLoggerOffline | None, solarman: SolisLoggerOffline
) -> SolisLoggerOffline:
    """A refused 502 is not firmware failure when 8899 is simply absent."""
    if s2_error is not None and s2_error.reason == REASON_SILENT:
        return s2_error
    if (
        s2_error is not None
        and s2_error.connected
        and s2_error.reason in {REASON_TIMEOUT, REASON_GARBAGE}
    ):
        return SolisLoggerOffline(
            "port 502 accepted TCP and sent no Modbus",
            connected=True,
            reason=REASON_SILENT,
        )
    return solarman


async def _probe_named_solarman(request: LoggerRequest, *, serial_unsupported: bool) -> LoggerProbe:
    if not request.logger_serial.isdigit():
        raise SolisLoggerOffline("Solarman discovery needs the numeric logger serial")
    port = request.port or SOLARMAN_PORT
    probe = await probe_logger(
        request.host,
        int(request.logger_serial),
        timeout=request.timeout,
        port=port,
        inverter_serial=request.inverter_serial,
        serial_unsupported=serial_unsupported,
        gap=request.gap,
        only_slave=request.slave,
    )
    return replace(probe, kind=LOGGER_SOLARMAN, port=port, function=4)


async def _probe_s2(request: LoggerRequest, *, serial_unsupported: bool) -> LoggerProbe:
    port = request.port or S2_PORT
    numeric = int(request.logger_serial) if request.logger_serial.isdigit() else 0
    client = ModbusTcpClient(
        request.host,
        port=port,
        timeout=request.timeout,
        gap=request.gap,
        fallback_function=3,
    )
    try:
        slaves = (request.slave,) if request.slave else (1, 2, 3)
        for slave in slaves:
            register_map = await _detect_map(client, slave)
            if register_map is None:
                continue
            learned, unsupported = await _serial_once(
                client,
                slave,
                known=request.inverter_serial,
                unsupported=serial_unsupported,
                logger_serial=numeric,
            )
            return LoggerProbe(
                slave=slave,
                register_map=register_map,
                inverter_serial=learned,
                protocol=PROTOCOL_MBTCP,
                serial_unsupported=unsupported,
                kind=LOGGER_S2,
                port=port,
                function=client.function,
            )
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        raise _annotate_s2(err) from err
    finally:
        await client.close()
    raise SolisLoggerOffline(f"{request.host} did not answer as a Solis inverter")


def _annotate_s2(err: SolisLoggerOffline) -> SolisLoggerOffline:
    if err.refused or err.reason == REASON_REFUSED:
        return SolisLoggerOffline(
            str(err), connected=False, refused=True, reason=REASON_TCP_DISABLED
        )
    if err.connected and err.reason in {REASON_TIMEOUT, REASON_GARBAGE}:
        return SolisLoggerOffline(str(err), connected=True, reason=REASON_SILENT)
    return err
