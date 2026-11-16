"""Local transport for Solis sticks that only serve a status web page.

The page shows what the stick last read from the inverter, about every five
minutes. A good read is a local snapshot, so ``SolisPriorityTransport`` does
not call SolisCloud for it. A stick that cannot be reached is asleep, as for
the other inverter-powered sticks. A wrong web password or a page that is not
the Solis format is a paused session, not night.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
import time

from .const import (
    LOGGER_WEB,
    PROTOCOL_HTTP,
    REASON_WEB_PAGE,
    WEB_DEFAULT_PASSWORD,
    WEB_PORT,
)
from .errors import SolisLoggerBusy, SolisLoggerOffline
from .local_transport import (
    LoggerProbe,
    LoggerRequest,
    asleep_snapshot,
    backoff_seconds,
    local_snapshot,
    placeholder_serial,
)
from .loggers import spec_for
from .loggers.web import WebInverter, WebStickClient
from .models import InverterSnapshot, SolisSnapshot
from .solarman import host_gate

_LOGGER = logging.getLogger(__name__)

TextCallback = Callable[[str], None]


def web_inverter(page: WebInverter, *, serial: str, logger: str, now: datetime) -> InverterSnapshot:
    """Build the inverter from one page read. Fields the page lacks stay ``None``."""
    return InverterSnapshot(
        serial=serial,
        device_id=serial,
        station_id="local",
        name=serial,
        model=None,
        product_model="1",
        hmi_version=page.firmware or None,
        state=3 if page.in_alarm else 1,
        ac_output_type=None,
        data_timestamp=now,
        collector_serial=logger,
        ac_power_w=page.power_w,
        frequency_hz=None,
        temperature_c=page.temperature_c,
        energy_today_kwh=page.energy_today_kwh,
        energy_month_kwh=None,
        energy_year_kwh=None,
        energy_total_kwh=page.energy_total_kwh,
        battery_power_w=None,
        battery_soc=None,
        battery_soh=None,
        battery_voltage_v=None,
        battery_current_a=None,
        battery_charge_today_kwh=None,
        battery_discharge_today_kwh=None,
        battery_charge_total_kwh=None,
        battery_discharge_total_kwh=None,
        grid_power_w=None,
        consumption_power_w=None,
        force_charge_soc=None,
        force_discharge_soc=None,
        phases=(),
        strings=(),
        data_source="local",
        model_code=page.model_code or None,
    )


class SolisWebTransport:
    """Read one stick's status page. A reset or refusal backs off like the other sticks."""

    def __init__(
        self,
        host: str,
        password: str,
        *,
        inverter_serial: str = "",
        stick_serial: str = "",
        port: int | None = None,
        timeout: float = 8.0,
        on_serial: TextCallback | None = None,
    ) -> None:
        """Store the address. The first connection opens on the first poll."""
        self._host = host
        self._spec = spec_for(LOGGER_WEB)
        self._port = port or WEB_PORT
        self._inverter_serial = inverter_serial
        self._stick_serial = stick_serial.strip()
        self._on_serial = on_serial
        self._client = WebStickClient(
            host, password or WEB_DEFAULT_PASSWORD, port=self._port, timeout=timeout
        )
        self.degraded_reason = ""
        self.identity_checked = False
        self._has_series = False
        self._last: SolisSnapshot | None = None
        self._asleep_logged = False
        self._busy_logged = False
        self._next_attempt = 0.0
        self._backoff_step = 0

    async def async_read(self) -> SolisSnapshot:
        """Return a snapshot. A paused session raises ``SolisLoggerBusy``."""
        if time.monotonic() < self._next_attempt:
            raise SolisLoggerBusy(
                f"{self._host} is in backoff",
                waiting=True,
                reason=self.degraded_reason or REASON_WEB_PAGE,
            )
        try:
            snapshot = await self._read_connected()
        except SolisLoggerBusy as err:
            self._pause(err)
            raise
        except SolisLoggerOffline as err:
            snapshot = self._after_offline(err)
        else:
            self._next_attempt = 0.0
            self._backoff_step = 0
            self.degraded_reason = ""
            self._asleep_logged = False
            self._busy_logged = False
        self._last = snapshot
        return snapshot

    def _pause(self, err: SolisLoggerBusy) -> None:
        if err.waiting:
            return
        self.degraded_reason = err.reason or REASON_WEB_PAGE
        delay = backoff_seconds(self._backoff_step)
        self._backoff_step += 1
        self._next_attempt = time.monotonic() + delay
        if not self._busy_logged:
            _LOGGER.info(
                "%s %s paused: %s; next try in %d seconds",
                self._spec.label,
                self._label(),
                err,
                int(delay),
            )
            self._busy_logged = True

    def _after_offline(self, err: SolisLoggerOffline) -> SolisSnapshot:
        """TCP that opened and then gave no page is a fault. No route is night."""
        if err.connected:
            failure = SolisLoggerBusy(str(err), reason=REASON_WEB_PAGE)
            self._pause(failure)
            raise failure from err
        if not self._asleep_logged:
            _LOGGER.info(
                "%s %s is offline; the inverter is asleep", self._spec.label, self._label()
            )
            self._asleep_logged = True
        if self._last is None:
            raise SolisLoggerOffline(f"stick {self._label()} is offline")
        return asleep_snapshot(self._last)

    async def _read_connected(self) -> SolisSnapshot:
        async with host_gate(self._host, self._port):
            page = await self._client.read_inverter()
        serial = self._inverter_serial or page.serial
        if serial and serial != self._inverter_serial and self._on_serial is not None:
            self._inverter_serial = serial
            self._on_serial(serial)
        logger = self._label()
        now = datetime.now(UTC)
        inverter = web_inverter(page, serial=serial or logger, logger=logger, now=now)
        return local_snapshot(inverter, logger, now, self._spec.label)

    def _label(self) -> str:
        return self._stick_serial or self._inverter_serial or self._host

    def adopt_serial(self, serial: str) -> None:
        """Use the cloud or user serial so later snapshots share entity ids."""
        if not serial or serial == self._inverter_serial:
            return
        self._inverter_serial = serial
        if self._on_serial is not None:
            self._on_serial(serial)

    def note_identity(self, donor: InverterSnapshot) -> None:
        """Keep a series name learned from SolisCloud."""
        if donor.model or donor.model_code:
            self._has_series = True

    @property
    def inverter_serial(self) -> str:
        """Serial currently used for entity ids."""
        return self._inverter_serial

    @property
    def logger_serial(self) -> int:
        """The stick has no numeric serial; a placeholder keeps the shared helpers happy."""
        return 0

    @property
    def last_snapshot(self) -> SolisSnapshot | None:
        """Last good snapshot, if this process has read one."""
        return self._last

    @property
    def needs_cloud_identity(self) -> bool:
        """True until a real serial or a series name is known."""
        return placeholder_serial(self._inverter_serial, 0) or not self._has_series

    async def close(self) -> None:
        """Nothing stays open between reads."""
        await self._client.close()

    def session_info(self) -> dict[str, str]:
        """Kind and framing for diagnostics. Addresses, serials and passwords stay out."""
        return {
            "kind": LOGGER_WEB,
            "protocol": PROTOCOL_HTTP,
            "port": str(self._port),
            "slave": "0",
            "register_map": "web",
            "function": "0",
            "experimental": "true" if self._spec.experimental else "false",
            "bridge": "false",
        }


async def probe_web(request: LoggerRequest, *, auto: bool) -> LoggerProbe:
    """Confirm the web page. ``auto`` first checks that port 80 is a Solis stick.

    Raises ``SolisLoggerBusy`` with a web reason for a wrong password or a page
    in another format, and ``SolisLoggerOffline`` when nothing answers.
    """
    from .loggers.fingerprint import read_http_banner
    from .loggers.web import is_solis_web_banner

    port = request.port or WEB_PORT
    if auto and not is_solis_web_banner(
        await read_http_banner(request.host, timeout=min(request.timeout, 3.0), port=port)
    ):
        raise SolisLoggerOffline(f"{request.host} has no Solis web page", refused=True)
    client = WebStickClient(
        request.host,
        request.password or WEB_DEFAULT_PASSWORD,
        port=port,
        timeout=request.timeout,
    )
    async with host_gate(request.host, port):
        page = await client.read_inverter()
        collector = await client.read_collector()
    return LoggerProbe(
        slave=0,
        register_map="web",
        inverter_serial=request.inverter_serial.strip() or page.serial,
        protocol=PROTOCOL_HTTP,
        serial_unsupported=False,
        kind=LOGGER_WEB,
        port=port,
        function=0,
        stick_serial=collector.serial if collector else "",
        mac=collector.mac if collector else "",
    )
