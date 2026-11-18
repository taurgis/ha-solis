"""Local logger transport and the local-first router.

V5 is tried first. Plain Modbus TCP on the same port, then port 30003, is
used only when V5 connects and does not speak V5. A reset is remembered as
a lock: one attempt, then a pause of at least 10 seconds that doubles up to
about 10 minutes. The 33000 hybrid range is not read once the 3000 map is
known, and registers at 3050 and above are not retried after they fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import logging
import random
import time
from typing import Any, Protocol

from homeassistant.util import dt as dt_util

from .cloud_fill import fill_from_cloud, match_inverter
from .const import (
    CONF_INVERTER_SERIAL,
    CONF_LOGGER_DETECT,
    CONF_LOGGER_FUNCTION,
    CONF_LOGGER_HOST,
    CONF_LOGGER_KIND,
    CONF_LOGGER_MAC,
    CONF_LOGGER_PASSWORD,
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
    LOCAL_STATION,
    LOGGER_AUTO,
    LOGGER_MBTCP,
    LOGGER_RTU,
    LOGGER_S2,
    LOGGER_S5,
    LOGGER_SOLARMAN,
    LOGGER_WEB,
    MBTCP_ALT_PORT,
    PROTOCOL_MBTCP,
    PROTOCOL_MBTCP_ALT,
    PROTOCOL_RTU,
    PROTOCOL_V5,
    REASON_GARBAGE,
    REASON_GATEWAY,
    REASON_LOCKED,
    REASON_REFUSED,
    REASON_S5_CLOSED,
    REASON_SILENT,
    REASON_TCP_DISABLED,
    REASON_TIMEOUT,
    REASON_UNSUPPORTED,
    REASON_WEB_AUTH,
    REASON_WEB_PAGE,
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
    WEB_DEFAULT_PASSWORD,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisLoggerBusy,
    SolisLoggerOffline,
)
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
    HYBRID_SERIAL_BLOCK,
    SERIAL_BLOCK,
    STRING_BLOCKS,
    TYPE_REGISTER,
    decode_hybrid,
    decode_string,
    hybrid_serial_from_words,
    map_from_type,
    serial_from_words,
)
from .solarman import (
    ModbusException,
    ModbusTcpClient,
    SolarmanClient,
    discover_sticks,
    host_gate,
)

_LOGGER = logging.getLogger(__name__)

_BACKOFF_BASE = 10.0
_BACKOFF_CAP = 600.0
_FRESH = timedelta(minutes=15)
# A web-page stick and SolisCloud share one five-minute data cycle. The cloud
# fill is read at that pace, and again sooner after a failed read.
_FILL_EVERY = 300.0
_FILL_RETRY = 60.0
# One cloud read may take 3 tries of 30 s. The local poll must not wait that long for it.
_CLOUD_BUDGET = 20.0
_PROTOCOLS = (PROTOCOL_V5, PROTOCOL_MBTCP, PROTOCOL_MBTCP_ALT)
_KNOWN_PROTOCOLS = (*_PROTOCOLS, PROTOCOL_RTU)
_FC3_KINDS = {LOGGER_S2, LOGGER_S5, LOGGER_MBTCP, LOGGER_RTU}
# A sleeping stick is looked for on the LAN at most this often.
_DISCOVERY_EVERY = 900.0
# S2-WL-ST firmware ignores port 502 for 3 to 6 minutes after a socket closes.
# The setup probe closes one, so a failure that soon after it is that wedge.
_S2_WEDGE = 420.0
_S2_WEDGE_RETRY = 30.0
_RECENT_S2_PROBES: dict[tuple[str, int], float] = {}

HostCallback = Callable[[str, str], None]
TextCallback = Callable[[str], None]


class RegisterReader(Protocol):
    """A framed Modbus client. V5, MBAP and raw RTU all look like this."""

    host: str

    @property
    def port(self) -> int: ...

    async def read_input(self, slave: int, address: int, count: int) -> list[int]: ...

    async def close(self) -> None: ...


class SnapshotReader(Protocol):
    """Anything that can return a Solis snapshot."""

    async def async_read(self) -> SolisSnapshot:
        """Read one snapshot."""


class LocalSource(Protocol):
    """What ``SolisPriorityTransport`` needs from a local reader.

    ``SolisLocalTransport`` (Modbus and Solarman) and ``SolisWebTransport``
    (the stick's status page) both fit.
    """

    identity_checked: bool
    degraded_reason: str
    # True when the reader covers few fields and SolisCloud should fill the rest.
    cloud_fill: bool

    async def async_read(self) -> SolisSnapshot: ...

    async def close(self) -> None: ...

    def adopt_serial(self, serial: str) -> None: ...

    def note_identity(self, donor: InverterSnapshot) -> None: ...

    def session_info(self) -> dict[str, str]: ...

    @property
    def inverter_serial(self) -> str: ...

    @property
    def logger_serial(self) -> int: ...

    @property
    def last_snapshot(self) -> SolisSnapshot | None: ...

    @property
    def needs_cloud_identity(self) -> bool: ...


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
    stick_serial: str = ""
    mac: str = ""


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


def _period_counters(inverter: InverterSnapshot, now: datetime) -> InverterSnapshot:
    """Today, month and year restart at local midnight even while the stick is dark."""
    moment = inverter.data_timestamp
    if moment is None:
        return inverter
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    before = dt_util.as_local(moment)
    current = dt_util.as_local(now)
    today = inverter.energy_today_kwh
    month = inverter.energy_month_kwh
    year = inverter.energy_year_kwh
    if before.date() != current.date() and today is not None:
        today = 0.0
    if (before.year, before.month) != (current.year, current.month) and month is not None:
        month = 0.0
    if before.year != current.year and year is not None:
        year = 0.0
    return replace(inverter, energy_today_kwh=today, energy_month_kwh=month, energy_year_kwh=year)


def _asleep_inverter(inverter: InverterSnapshot, now: datetime) -> InverterSnapshot:
    inverter = _period_counters(inverter, now)
    return replace(
        inverter,
        state=2,
        data_timestamp=now,
        ac_power_w=0.0,
        grid_power_w=0.0 if inverter.grid_power_w is not None else None,
        consumption_power_w=0.0 if inverter.consumption_power_w is not None else None,
        battery_power_w=0.0 if inverter.battery_power_w is not None else None,
        frequency_hz=None,
        temperature_c=None,
        battery_current_a=0.0 if inverter.battery_current_a is not None else None,
        battery_voltage_v=None,
        data_source="asleep",
        unreachable=False,
        phases=tuple(
            replace(phase, voltage_v=None, power_w=0.0, current_a=0.0) for phase in inverter.phases
        ),
        strings=tuple(
            replace(pv, voltage_v=None, power_w=0.0, current_a=0.0) for pv in inverter.strings
        ),
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
        self._client: RegisterReader | None = _client(
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
        self._next_discovery = 0.0
        self._has_series = False
        self.identity_checked = False
        self.cloud_fill = False

    async def async_read(self) -> SolisSnapshot:
        """Return a snapshot. A locked logger raises ``SolisLoggerBusy``."""
        if time.monotonic() < self._next_attempt:
            raise SolisLoggerBusy(
                f"{self._host} is in backoff",
                waiting=True,
                reason=self.degraded_reason or REASON_LOCKED,
            )
        try:
            try:
                snapshot = await self._read_connected()
            except SolisLoggerOffline as err:
                # A lock found while handling the offline read (a refused port
                # on the known host, an S2 that went silent) must reach the
                # busy handler below, so it is not raised from this clause.
                snapshot = await self._after_offline(err)
            else:
                self._clear_backoff()
                self.degraded_reason = ""
                self._asleep_logged = False
                self._busy_logged = False
                # The first miss after a good read may look for a moved stick at once.
                self._next_discovery = 0.0
        except SolisLoggerBusy as err:
            if not err.waiting:
                self.degraded_reason = err.reason or REASON_LOCKED
                self._arm_backoff()
            raise
        self._last = snapshot
        return snapshot

    def _arm_backoff(self) -> float:
        delay = backoff_seconds(self._backoff_step)
        self._backoff_step += 1
        self._next_attempt = time.monotonic() + delay
        if not self._busy_logged:
            if self.degraded_reason == REASON_TIMEOUT:
                text = (
                    f"{self._spec.label} {self._stick_label()} is not answering and the "
                    f"inverter is not asleep; next try in {int(delay)} seconds"
                )
            else:
                text = _reset_log(self._kind, self._spec.label, self._stick_label(), int(delay))
            _LOGGER.info("%s", text)
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
        self._not_night(err)
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._logger_serial} is offline")
        return asleep_snapshot(self._last)

    def _not_night(self, err: SolisLoggerOffline | None) -> None:
        """A hybrid runs on its battery at night, so a dark stick there is a fault.

        The read is paused (degraded, not asleep): the priority transport then
        publishes the fresh cloud sample or holds the last reading, instead of
        zero power with the battery still working.
        """
        if self._map != REGISTER_HYBRID:
            return
        raise SolisLoggerBusy(
            f"{self._stick_label()} did not answer and a hybrid inverter does not sleep",
            reason=REASON_TIMEOUT,
        ) from err

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
        if self._kind == LOGGER_S5 and err is not None and (failure is not None or err.connected):
            # A closed S5 port is firmware, including a socket that was open and
            # then reset. S2 still treats that close as the inverter powering off.
            failure = SolisLoggerBusy(str(err), reason=REASON_S5_CLOSED)
        if failure is not None and self._just_probed():
            # The setup probe's own close wedged the port. That is not the
            # firmware fault a repair would describe, and it clears in minutes.
            self.degraded_reason = REASON_LOCKED
            self._next_attempt = time.monotonic() + _S2_WEDGE_RETRY
            raise SolisLoggerBusy(
                f"{self._host} port {self._port} is still closed after setup",
                waiting=True,
                reason=REASON_LOCKED,
            ) from err
        if failure is not None:
            raise failure
        self._not_night(err)
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._stick_label()} is offline")
        return asleep_snapshot(self._last)

    def _just_probed(self) -> bool:
        if self._last is not None:
            return False
        probed = _RECENT_S2_PROBES.get((self._host, self._port))
        return probed is not None and time.monotonic() - probed < _S2_WEDGE

    async def _bridge_offline(self, err: SolisLoggerOffline | None) -> SolisSnapshot:
        """A refused gateway is a fault. A timeout after TCP opens is the inverter."""
        failure = _bridge_busy(self._host, err)
        if failure is not None:
            raise failure
        self._not_night(err)
        self._log_asleep()
        if self._last is None:
            raise SolisLoggerOffline(f"logger {self._stick_label()} is offline")
        return asleep_snapshot(self._last)

    def _stick_label(self) -> str:
        if self._kind == LOGGER_SOLARMAN:
            return str(self._logger_serial)
        return self._stick_serial or self._inverter_serial or self._host

    async def _find_stick(self) -> str | None:
        """Look for the stick on the LAN. A sleeping stick is looked for every 15 minutes."""
        if self._kind != LOGGER_SOLARMAN:
            return None
        now = time.monotonic()
        if now < self._next_discovery:
            return None
        self._next_discovery = now + _DISCOVERY_EVERY
        try:
            sticks = await discover_sticks(timeout=1.0)
        except OSError:
            return None
        for stick in sticks:
            if stick.serial == self._logger_serial:
                self._mac = stick.mac or self._mac
                # Found: the next miss may look again at once (the stick moved).
                self._next_discovery = 0.0
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
        return local_snapshot(inverter, logger, datetime.now(UTC), self._spec.label)

    async def _read_words(self) -> dict[int, int]:
        last_offline: SolisLoggerOffline | None = None
        async with host_gate(self._host, self._port):
            return await self._read_words_locked(last_offline)

    async def _read_words_locked(self, last_offline: SolisLoggerOffline | None) -> dict[int, int]:
        for protocol in self._candidates():
            client = await self._open(protocol)
            try:
                words = await _read_blocks(client, self._slave, self._blocks())
            except SolisLoggerBusy:
                await self._drop()
                raise
            except ModbusException as err:
                await self._drop()
                raise SolisLoggerBusy(
                    f"{self._host} returned Modbus exception {err.code}",
                    reason=REASON_UNSUPPORTED,
                ) from err
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

    async def _open(self, protocol: str) -> RegisterReader:
        if self._client is not None and self._active == protocol:
            return self._client
        await self._drop()
        if protocol == PROTOCOL_RTU:
            client: RegisterReader = RtuTcpClient(
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
        """Close the socket and keep the client so the next read can reconnect.

        ``self._port`` is the configured port. Port 30003 belongs to the
        MBTCP_ALT client only, so a sweep never moves V5 or MBTCP off it.
        """
        if self._client is None:
            return
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
            "port": str(self._client.port if self._client is not None else self._port),
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
        local: LocalSource | None,
        cloud: SnapshotReader | None,
    ) -> None:
        """``cloud`` is anything with ``async_read``, usually the SolisCloud transport."""
        self._mode = mode
        self._local = local
        self._cloud = cloud
        self._fill: SolisSnapshot | None = None
        self._fill_due = 0.0
        self._budget_logged = False
        self.cloud_error: SolisCloudError | None = None

    async def async_read(self) -> SolisSnapshot:
        """Read local first. Night keeps totals. A reset uses the cloud when it is fresh."""
        if self._mode == SOURCE_CLOUD or self._local is None:
            return _tagged(await _read_cloud(self._cloud), "cloud")
        try:
            local = await self._local.async_read()
        except SolisLoggerBusy:
            # A rejected web password (SolisLoggerAuthError) also lands here: the
            # cloud keeps the entry live and the coordinator starts reauth from
            # the degraded reason.
            return await self._when_busy()
        except SolisLoggerOffline:
            local = None
        if local is not None and local.source == "local":
            if self._mode == SOURCE_BOTH and self._local.cloud_fill:
                return await self._with_cloud_fill(local)
            return await self._with_identity(local)
        if local is not None:
            return await self._while_asleep(local)
        if self._mode == SOURCE_LOCAL:
            raise SolisLoggerOffline("logger is offline")
        cloud = await self._optional_cloud()
        donor = _cloud_donor(self._local, cloud) if cloud is not None else None
        if cloud is None or donor is None:
            raise SolisLoggerOffline("logger is offline")
        # Only this entry's inverter, never another one on the same account.
        return asleep_snapshot(replace(cloud, inverters=(donor,)))

    async def _while_asleep(self, local: SolisSnapshot) -> SolisSnapshot:
        """A dark stick. A fresh cloud sample that says the inverter is online wins.

        The stick is powered by the inverter, so a dark stick is normally night.
        When SolisCloud has a recent sample of this inverter still online and
        producing, the stick is the one at fault and the cloud is published.
        """
        local_source = self._local
        assert local_source is not None
        cloud = await self._optional_cloud()
        if self._mode == SOURCE_BOTH and cloud is not None:
            donor = _cloud_donor(local_source, cloud)
            if (
                donor is not None
                and donor.state == 1
                and (donor.ac_power_w or 0) > 0
                and _donor_is_fresh(cloud, donor)
            ):
                return _degraded_cloud(local_source, cloud, donor)
        return _fill_missing_energy(local, cloud, local_source.logger_serial)

    async def _when_busy(self) -> SolisSnapshot:
        local = self._local
        assert local is not None
        if self._mode == SOURCE_BOTH:
            cloud = await self._optional_cloud()
            donor = _cloud_donor(local, cloud) if cloud is not None else None
            if cloud is not None and donor is not None and _donor_is_fresh(cloud, donor):
                return _degraded_cloud(local, cloud, donor)
        if local.last_snapshot is not None:
            return _mark_degraded(local.last_snapshot)
        raise SolisLoggerBusy(
            f"{local.inverter_serial} local session is paused",
            reason=local.degraded_reason or REASON_LOCKED,
        )

    async def _with_cloud_fill(self, local: SolisSnapshot) -> SolisSnapshot:
        """Fill fields the reader lacks from a cached cloud sample, refreshed every 5 minutes."""
        transport = self._local
        assert transport is not None
        if self._cloud is None:
            return local
        sample = await self._optional_cloud()
        # A fill that is missing or for another inverter must not prune the plant's devices.
        unfilled = replace(local, partial=True)
        if sample is None:
            return unfilled
        serial = transport.inverter_serial
        donor = match_inverter(sample, serial)
        if donor is None:
            return unfilled
        if placeholder_serial(serial, transport.logger_serial) and donor.serial:
            transport.adopt_serial(donor.serial)
            serial = donor.serial
        transport.note_identity(donor)
        return fill_from_cloud(local, sample, serial=serial, fresh=_donor_is_fresh(sample, donor))

    async def _with_identity(self, local: SolisSnapshot) -> SolisSnapshot:
        """One cloud read can supply the inverter serial, series and model code."""
        transport = self._local
        if (
            transport is None
            or self._mode != SOURCE_BOTH
            or transport.identity_checked
            or not transport.needs_cloud_identity
        ):
            return local
        cloud = await self._optional_cloud()
        if cloud is None or not cloud.inverters or not local.inverters:
            return local
        transport.identity_checked = True
        serial = transport.inverter_serial
        donor = match_inverter(cloud, serial)
        if donor is None and len(cloud.inverters) == 1:
            donor = cloud.inverters[0]
        if donor is None:
            return local
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
        """The cloud sample, read at most every 5 minutes. A failure is retried after a minute.

        The cloud publishes about every 5 minutes, so a faster read only spends rate limit.
        A rejected key or a wrong clock is kept in ``cloud_error`` for the coordinator.
        """
        if self._cloud is None:
            return None
        now = time.monotonic()
        if now >= self._fill_due:
            try:
                async with asyncio.timeout(_CLOUD_BUDGET):
                    sample = _tagged(await _read_cloud(self._cloud), "cloud")
            except (SolisCloudError, TimeoutError) as err:
                if isinstance(err, TimeoutError) and not self._budget_logged:
                    _LOGGER.info(
                        "SolisCloud did not answer within %d seconds; the local "
                        "reading is published without it and the cloud is tried "
                        "again in %d seconds",
                        int(_CLOUD_BUDGET),
                        int(_FILL_RETRY),
                    )
                    self._budget_logged = True
                self._fill_due = now + _FILL_RETRY
                self.cloud_error = (
                    err if isinstance(err, (SolisAuthError, SolisClockError)) else None
                )
            else:
                self._fill = sample
                self._fill_due = now + _FILL_EVERY
                self.cloud_error = None
                self._budget_logged = False
        return self._fill

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


def _cloud_donor(local: LocalSource, cloud: SolisSnapshot) -> InverterSnapshot | None:
    """The cloud inverter for this entry, matched by serial.

    A placeholder serial (``L`` and the logger number) matches only when the
    account has exactly one inverter. Another inverter's serial or totals are
    never adopted.
    """
    serial = local.inverter_serial
    if placeholder_serial(serial, local.logger_serial):
        return match_inverter(cloud, "")
    return match_inverter(cloud, serial)


def _donor_is_fresh(sample: SolisSnapshot, donor: InverterSnapshot) -> bool:
    """True when the cloud was read recently and its inverter reading is recent too."""
    now = datetime.now(UTC)
    if now - sample.fetched_at > _FRESH:
        return False
    moment = donor.data_timestamp
    if moment is None:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return now - moment <= _FRESH


def _period_value(previous: float | None, incoming: float | None) -> float | None:
    """A today, month or year counter belongs to its period, so the newer sample wins.

    The previous snapshot may hold yesterday's number. The sensor keeps a counter from
    dropping inside a period, so a slightly older cloud value cannot cause a dip.
    """
    return previous if incoming is None else incoming


def _prefer(previous: float | None, incoming: float | None) -> float | None:
    if previous is None:
        return incoming
    if incoming is None or incoming < previous:
        return previous
    return incoming


def _degraded_cloud(
    local: LocalSource, cloud: SolisSnapshot, donor: InverterSnapshot
) -> SolisSnapshot:
    """Publish the matched cloud inverter on the local inverter serial."""
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
        energy_today_kwh=_period_value(
            previous.energy_today_kwh if previous else None, donor.energy_today_kwh
        ),
        energy_month_kwh=_period_value(
            previous.energy_month_kwh if previous else None, donor.energy_month_kwh
        ),
        energy_year_kwh=_period_value(
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
        # The cloud plant is not this entry's local device set, so nothing may be pruned.
        partial=True,
    )


def local_snapshot(
    inverter: InverterSnapshot, logger: str, now: datetime, model: str = "Solarman"
) -> SolisSnapshot:
    return SolisSnapshot(
        stations=(
            StationSnapshot(
                station_id=LOCAL_STATION,
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
                station_id=LOCAL_STATION,
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
    async with host_gate(host, port):
        return await _probe_locked(
            host,
            logger_serial,
            order=order,
            slaves=slaves,
            timeout=timeout,
            port=port,
            inverter_serial=inverter_serial,
            serial_unsupported=serial_unsupported,
            gap=gap,
        )


async def _probe_locked(
    host: str,
    logger_serial: int,
    *,
    order: tuple[str, ...],
    slaves: tuple[int, ...],
    timeout: float,
    port: int,
    inverter_serial: str,
    serial_unsupported: bool,
    gap: float,
) -> LoggerProbe:
    last_offline: SolisLoggerOffline | None = None
    for framing in order:
        client = _client(host, logger_serial, framing, port=port, timeout=timeout, gap=gap)
        try:
            found = await _scan_slaves(client, slaves)
            if found is not None:
                slave, register_map = found
                learned, unsupported = await _serial_once(
                    client,
                    slave,
                    known=inverter_serial,
                    unsupported=serial_unsupported,
                    logger_serial=logger_serial,
                    register_map=register_map,
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
) -> RegisterReader:
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


async def _detect_map(client: RegisterReader, slave: int) -> str | None:
    """Return the register map this slave answers, or None.

    3004 is read first: an old Solis-mini 4G resets the session on reads at
    3050 and above, so nothing higher is read once the 3000 map answers.
    When 3004 is an illegal address, the type register 35000 names the map
    (hn/ginlong-solis), and 33022 is the fallback when 35000 says nothing.
    A silent slave raises the timeout so the caller can try the next slave.
    """
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
    named = await _type_register(client, slave)
    if named is not None:
        return named if named == REGISTER_HYBRID else None
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


async def _type_register(client: RegisterReader, slave: int) -> str | None:
    """Read 35000. A failure or an unknown value leaves the 33022 probe to decide."""
    try:
        values = await client.read_input(slave, TYPE_REGISTER, 1)
    except ModbusException:
        return None
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        if err.refused or (err.reason and err.reason != REASON_TIMEOUT):
            raise
        return None
    if not values:
        return None
    return map_from_type(values[0])


async def _scan_slaves(client: RegisterReader, slaves: tuple[int, ...]) -> tuple[int, str] | None:
    """Return the first slave with a Solis map.

    A slave that does not answer (a timeout on an open socket, or a gateway's
    exception 0x0B) is another slave id to try, not another framing. When
    every slave was silent the last timeout is raised; when one answered
    without a Solis map the result is None.
    """
    silent: SolisLoggerOffline | None = None
    answered = False
    for slave in slaves:
        try:
            register_map = await _detect_map(client, slave)
        except SolisLoggerOffline as err:
            if err.connected and not err.refused and err.reason == REASON_TIMEOUT:
                silent = err
                continue
            raise
        if register_map is not None:
            return slave, register_map
        answered = True
    if silent is not None and not answered:
        raise silent
    return None


async def _serial_once(
    client: RegisterReader,
    slave: int,
    *,
    known: str,
    unsupported: bool,
    logger_serial: int,
    register_map: str = REGISTER_STRING,
) -> tuple[str, bool]:
    """Read the serial once. A failure is cached so the poll never scans that range again.

    The string map keeps it hex-nibble encoded at 3060-3063; a hybrid keeps
    it as ASCII at 33004-33019.
    """
    if known and not placeholder_serial(known, logger_serial):
        return known, unsupported
    if unsupported:
        return known or f"L{logger_serial}", True
    block = HYBRID_SERIAL_BLOCK if register_map == REGISTER_HYBRID else SERIAL_BLOCK
    try:
        words = await client.read_input(slave, block[0], block[1])
    except SolisLoggerBusy:
        return known or f"L{logger_serial}", True
    except (SolisLoggerOffline, ModbusException, ValueError, TimeoutError):
        return known or f"L{logger_serial}", True
    if register_map == REGISTER_HYBRID:
        learned = hybrid_serial_from_words(list(words))
    else:
        bank = {block[0] + offset: value for offset, value in enumerate(words)}
        learned = serial_from_words(bank)
    return learned or known or f"L{logger_serial}", not bool(learned)


async def _read_blocks(
    client: RegisterReader,
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


def _fill_missing_energy(
    local: SolisSnapshot, cloud: SolisSnapshot | None, logger_serial: int = 0
) -> SolisSnapshot:
    """Use this inverter's cloud counters only where the local snapshot has no number."""
    if cloud is None or not local.inverters or not cloud.inverters:
        return local
    filled = []
    for inverter in local.inverters:
        wanted = "" if placeholder_serial(inverter.serial, logger_serial) else inverter.serial
        donor = match_inverter(cloud, wanted)
        if donor is None:
            filled.append(inverter)
            continue
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
    password: str = field(default="", repr=False)


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
    password = str(user_input.get(CONF_LOGGER_PASSWORD) or "").strip()
    if kind == LOGGER_SOLARMAN and not serial.isdigit():
        return None, "invalid_logger"
    request = LoggerRequest(
        host, kind, port, slave, serial, inverter, preset=preset, password=password
    )
    return request, ""


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


_FORM_ERRORS = {
    REASON_TCP_DISABLED: "logger_tcp_disabled",
    REASON_SILENT: "logger_modbus_silent",
    REASON_GATEWAY: "logger_gateway",
    REASON_WRONG_PROTOCOL: "logger_wrong_protocol",
    REASON_WRONG_SLAVE: "logger_wrong_slave",
    REASON_UNSUPPORTED: "logger_unsupported_map",
    REASON_TIMEOUT: "logger_timeout",
    REASON_WEB_AUTH: "logger_web_password",
    REASON_WEB_PAGE: "logger_web_page",
}


def logger_form_error(err: BaseException) -> str:
    """Map a probe failure onto a config-flow error key."""
    reason = str(getattr(err, "reason", "") or "")
    if reason in _FORM_ERRORS:
        return _FORM_ERRORS[reason]
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
    if probe.kind == LOGGER_WEB:
        return placeholder_serial(inverter_serial.strip() or probe.inverter_serial.strip(), 0)
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
    if kind == LOGGER_WEB:
        unique = f"{spec.unique_prefix}:{inverter}"
        serial_field = probe.stick_serial
        port = probe.port or spec.default_port
    elif spec.unique_prefix:
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
    if kind == LOGGER_WEB:
        data[CONF_LOGGER_PASSWORD] = request.password or WEB_DEFAULT_PASSWORD
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
    if kind == LOGGER_WEB:
        from .web_transport import probe_web

        return await probe_web(request, auto=False)
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
        failure = _prefer_auto_error(s2_error, err)
    return await _auto_web(request, failure)


async def _auto_web(request: LoggerRequest, failure: SolisLoggerOffline) -> LoggerProbe:
    """Neither Modbus nor Solarman answered. Port 80 may be a Solis stick with only a web page."""
    from .web_transport import probe_web

    if failure.reason == REASON_SILENT:
        raise failure
    try:
        return await probe_web(request, auto=True)
    except SolisLoggerOffline:
        raise failure from None


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
    async with host_gate(request.host, port):
        return await _probe_s2_locked(request, port=port, serial_unsupported=serial_unsupported)


async def _probe_s2_locked(
    request: LoggerRequest, *, port: int, serial_unsupported: bool
) -> LoggerProbe:
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
        found = await _scan_slaves(client, slaves)
        if found is not None:
            slave, register_map = found
            learned, unsupported = await _serial_once(
                client,
                slave,
                known=request.inverter_serial,
                unsupported=serial_unsupported,
                logger_serial=numeric,
                register_map=register_map,
            )
            # The entry's first poll will meet the port this close may wedge.
            _RECENT_S2_PROBES[(request.host, port)] = time.monotonic()
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
