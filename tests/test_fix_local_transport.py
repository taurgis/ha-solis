"""Local transport fixes: cloud matching, ports, backoff, sleep and the S2 wedge."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import socket
import time
from typing import Any

import pytest

from custom_components.solis import local_transport as lt
from custom_components.solis.const import (
    LOGGER_MBTCP,
    LOGGER_S2,
    REASON_CLOSED,
    REASON_LOCKED,
    REASON_TCP_DISABLED,
    REASON_TIMEOUT,
)
from custom_components.solis.errors import (
    SolisLoggerAuthError,
    SolisLoggerBusy,
    SolisLoggerOffline,
)
from custom_components.solis.local_transport import (
    SolisLocalTransport,
    SolisPriorityTransport,
    _asleep_inverter,
    _cloud_donor,
    asleep_snapshot,
)
from custom_components.solis.models import InverterSnapshot, SolisSnapshot
from custom_components.solis.solarman import Stick
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState

OURS = "1031000000000001"
OTHER = "1031000000000002"


def _inverter(serial: str = OURS, **kwargs: Any) -> InverterSnapshot:
    base: dict[str, Any] = {
        "serial": serial,
        "device_id": serial,
        "station_id": "local",
        "name": serial,
        "model": "Solis 4G",
        "product_model": "1",
        "hmi_version": "0F000F",
        "state": 1,
        "ac_output_type": 0,
        "data_timestamp": datetime.now(UTC),
        "collector_serial": str(SERIAL),
        "ac_power_w": 600.0,
        "frequency_hz": 50.0,
        "temperature_c": 27.0,
        "energy_today_kwh": 3.1,
        "energy_month_kwh": 30.0,
        "energy_year_kwh": 2690.0,
        "energy_total_kwh": 21970.0,
        "battery_power_w": None,
        "battery_soc": None,
        "battery_soh": None,
        "battery_voltage_v": None,
        "battery_current_a": None,
        "battery_charge_today_kwh": None,
        "battery_discharge_today_kwh": None,
        "battery_charge_total_kwh": None,
        "battery_discharge_total_kwh": None,
        "grid_power_w": None,
        "consumption_power_w": None,
        "force_charge_soc": None,
        "force_discharge_soc": None,
        "phases": (),
        "strings": (),
        "model_code": None,
    }
    base.update(kwargs)
    return InverterSnapshot(**base)


def _snapshot(*inverters: InverterSnapshot, source: str = "local") -> SolisSnapshot:
    return SolisSnapshot(
        stations=(),
        inverters=inverters,
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
        source=source,
    )


class _Cloud:
    def __init__(self, snapshot: SolisSnapshot) -> None:
        self.snapshot = snapshot
        self.reads = 0

    async def async_read(self) -> SolisSnapshot:
        self.reads += 1
        return self.snapshot


def _local(serial: str = OURS, **kwargs: Any) -> SolisLocalTransport:
    options: dict[str, Any] = {
        "slave": 1,
        "register_map": "string",
        "inverter_serial": serial,
        "timeout": 0.3,
        "gap": 0,
        "protocol": "v5",
    }
    options.update(kwargs)
    return SolisLocalTransport("127.0.0.1", SERIAL, **options)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _emulator(**kwargs: Any) -> tuple[StickEmulator, int]:
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=0, **kwargs))
    await emulator.start()
    assert emulator._tcp is not None
    return emulator, emulator._tcp.sockets[0].getsockname()[1]


# Finding 1: the cloud fallback is matched by serial.


async def test_busy_two_inverter_account_uses_this_inverter() -> None:
    """A paused stick on a two-inverter account publishes its own inverter's cloud data."""
    local = _local()
    local._last = _snapshot(_inverter(ac_power_w=100.0))

    async def _busy() -> SolisSnapshot:
        raise SolisLoggerBusy("reset")

    local.async_read = _busy  # type: ignore[method-assign]
    cloud = _snapshot(
        _inverter(OTHER, ac_power_w=4000.0, energy_total_kwh=99999.0),
        _inverter(OURS, ac_power_w=750.0),
        source="cloud",
    )
    published = await SolisPriorityTransport("both", local, _Cloud(cloud)).async_read()
    assert published.source == "degraded"
    assert [inverter.serial for inverter in published.inverters] == [OURS]
    assert published.inverters[0].ac_power_w == 750
    assert published.inverters[0].energy_total_kwh == 21970


async def test_placeholder_serial_never_takes_one_of_two_inverters() -> None:
    """Without a serial, a two-inverter account has no donor: the last sample is held."""
    local = _local("")
    local._last = _snapshot(_inverter(f"L{SERIAL}", ac_power_w=100.0))

    async def _busy() -> SolisSnapshot:
        raise SolisLoggerBusy("reset")

    local.async_read = _busy  # type: ignore[method-assign]
    cloud = _snapshot(_inverter(OTHER), _inverter(OURS), source="cloud")
    assert _cloud_donor(local, cloud) is None
    held = await SolisPriorityTransport("both", local, _Cloud(cloud)).async_read()
    assert held.source == "degraded"
    assert held.inverters[0].ac_power_w == 100
    assert local.inverter_serial == f"L{SERIAL}"
    single = _snapshot(_inverter(OURS), source="cloud")
    assert _cloud_donor(local, single) is not None


async def test_offline_without_local_sample_publishes_only_the_match() -> None:
    """A first poll at night publishes this inverter's cloud totals, never the plant."""
    local = _local()

    async def _offline() -> SolisSnapshot:
        raise SolisLoggerOffline("dark")

    local.async_read = _offline  # type: ignore[method-assign]
    cloud = _snapshot(
        _inverter(OTHER, energy_total_kwh=5.0), _inverter(OURS, energy_total_kwh=21970.0)
    )
    asleep = await SolisPriorityTransport("both", local, _Cloud(cloud)).async_read()
    assert asleep.source == "asleep"
    assert [inverter.serial for inverter in asleep.inverters] == [OURS]
    assert asleep.inverters[0].ac_power_w == 0
    unmatched = _snapshot(_inverter(OTHER), _inverter("1031000000000003"))
    with pytest.raises(SolisLoggerOffline):
        await SolisPriorityTransport("both", local, _Cloud(unmatched)).async_read()


# Finding 2: a protocol sweep never moves the configured port.


async def test_sweep_keeps_the_configured_port(socket_enabled: None) -> None:
    """V5 and Modbus TCP time out, port 30003 is refused: the next poll is on the same port."""
    emulator, port = await _emulator(silent=True)
    local = _local(protocol="", port=port, timeout=0.3)

    async def _nowhere() -> str | None:
        return None

    local._find_stick = _nowhere  # type: ignore[method-assign]
    try:
        for _ in range(2):
            with pytest.raises(SolisLoggerOffline):
                await local.async_read()
            assert local._port == port
            # The sweep ended on the 30003 client; V5 opens on the configured port again.
            assert local.session_info()["port"] == "30003"
            await local._open("v5")
            assert local._client is not None
            assert local._client.port == port
            assert local.session_info()["port"] == str(port)
    finally:
        await local.close()
        await emulator.close()


# Finding 3: a refused known host is a lock with a backoff.


async def test_refused_known_host_arms_the_backoff(socket_enabled: None) -> None:
    """Discovery finds the stick at the same address and its port refuses: back off."""
    local = _local(port=_free_port())

    async def _same() -> str:
        return "127.0.0.1"

    local._find_stick = _same  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy) as first:
        await local.async_read()
    assert not first.value.waiting
    assert local.degraded_reason == REASON_LOCKED
    assert local._next_attempt > time.monotonic()
    with pytest.raises(SolisLoggerBusy) as second:
        await local.async_read()
    assert second.value.waiting
    await local.close()


# Finding 14: discovery is rate limited while the stick stays dark.


async def test_discovery_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    found: list[Stick] = []

    async def _discover(timeout: float = 1.0) -> list[Stick]:
        calls.append(timeout)
        return list(found)

    monkeypatch.setattr(lt, "discover_sticks", _discover)
    local = _local()
    assert await local._find_stick() is None
    assert await local._find_stick() is None
    assert len(calls) == 1
    local._next_discovery = 0.0
    found.append(Stick("10.0.0.9", "bc54f9f57b5c", SERIAL))
    assert await local._find_stick() == "10.0.0.9"
    # Found: the next miss may look again at once.
    found.clear()
    assert await local._find_stick() is None
    assert len(calls) == 3

    async def _broken(timeout: float = 1.0) -> list[Stick]:
        raise OSError("no broadcast")

    monkeypatch.setattr(lt, "discover_sticks", _broken)
    local._next_discovery = 0.0
    assert await local._find_stick() is None
    assert await _local(kind=LOGGER_S2)._find_stick() is None


# Finding 15: a dark hybrid is degraded; a dark string with a live cloud is degraded.


async def test_dark_hybrid_is_degraded_not_asleep() -> None:
    """A hybrid does not sleep: its silent stick backs off and the last sample is held."""
    local = _local(register_map="hybrid")
    local._last = _snapshot(_inverter(battery_power_w=-800.0))

    async def _silent() -> SolisSnapshot:
        raise SolisLoggerOffline("no answer", connected=True, reason=REASON_TIMEOUT)

    async def _nowhere() -> str | None:
        return None

    local._read_connected = _silent  # type: ignore[method-assign]
    local._find_stick = _nowhere  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy) as caught:
        await local.async_read()
    assert caught.value.reason == REASON_TIMEOUT
    assert local.degraded_reason == REASON_TIMEOUT
    assert local._next_attempt > time.monotonic()
    held = await SolisPriorityTransport("local", local, None).async_read()
    assert held.source == "degraded"
    assert held.inverters[0].battery_power_w == -800


async def test_dark_hybrid_bridge_and_s2_are_degraded() -> None:
    for kind in (LOGGER_MBTCP, LOGGER_S2):
        local = _local(register_map="hybrid", kind=kind, protocol="")
        local._last = _snapshot(_inverter())

        async def _silent() -> SolisSnapshot:
            raise SolisLoggerOffline("no answer", connected=True, reason=REASON_CLOSED)

        local._read_connected = _silent  # type: ignore[method-assign]
        with pytest.raises(SolisLoggerBusy) as caught:
            await local.async_read()
        assert caught.value.reason == REASON_TIMEOUT


async def test_string_both_mode_prefers_a_producing_cloud() -> None:
    """A dark stick while the cloud says the inverter produces 800 W is the stick's fault."""
    local = _local()
    asleep = asleep_snapshot(_snapshot(_inverter()))

    async def _asleep() -> SolisSnapshot:
        return asleep

    local.async_read = _asleep  # type: ignore[method-assign]
    live = _snapshot(_inverter(OTHER, ac_power_w=50.0), _inverter(OURS, ac_power_w=800.0))
    published = await SolisPriorityTransport("both", local, _Cloud(live)).async_read()
    assert published.source == "degraded"
    assert published.inverters[0].ac_power_w == 800
    night = _snapshot(_inverter(OURS, ac_power_w=0.0, state=2, energy_today_kwh=None))
    published = await SolisPriorityTransport("both", local, _Cloud(night)).async_read()
    assert published.source == "asleep"
    assert published.inverters[0].ac_power_w == 0
    stale = _snapshot(
        _inverter(OURS, data_timestamp=datetime.now(UTC) - timedelta(hours=2)),
    )
    published = await SolisPriorityTransport("both", local, _Cloud(stale)).async_read()
    assert published.source == "asleep"
    # Local-only mode never consults the cloud for a dark stick.
    published = await SolisPriorityTransport("local", local, _Cloud(live)).async_read()
    assert published.source == "asleep"


def test_period_counters_reset_after_midnight() -> None:
    """A stick dark since yesterday publishes zero for today, and month/year at a new period."""
    before = _inverter(data_timestamp=datetime(2026, 3, 31, 12, 0, tzinfo=UTC))
    after = _asleep_inverter(before, datetime(2026, 4, 1, 12, 0, tzinfo=UTC))
    assert after.energy_today_kwh == 0
    assert after.energy_month_kwh == 0
    assert after.energy_year_kwh == 2690
    assert after.energy_total_kwh == 21970
    year_end = _inverter(data_timestamp=datetime(2026, 12, 31, 12, 0))
    new_year = _asleep_inverter(year_end, datetime(2027, 1, 1, 12, 0, tzinfo=UTC))
    assert new_year.energy_year_kwh == 0
    same_day = _asleep_inverter(
        _inverter(data_timestamp=datetime(2026, 4, 1, 11, 0, tzinfo=UTC)),
        datetime(2026, 4, 1, 11, 5, tzinfo=UTC),
    )
    assert same_day.energy_today_kwh == 3.1
    undated = _asleep_inverter(_inverter(data_timestamp=None), datetime.now(UTC))
    assert undated.energy_today_kwh == 3.1
    missing = _asleep_inverter(
        _inverter(
            data_timestamp=datetime(2026, 12, 31, 12, 0, tzinfo=UTC),
            energy_today_kwh=None,
            energy_month_kwh=None,
            energy_year_kwh=None,
        ),
        datetime(2027, 1, 1, 12, 0, tzinfo=UTC),
    )
    assert missing.energy_today_kwh is None
    assert missing.energy_month_kwh is None
    assert missing.energy_year_kwh is None


# Finding 16: a cloud that overruns the budget is logged once.


async def test_cloud_budget_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(lt, "_CLOUD_BUDGET", 0.05)
    hang = True

    class _Slow:
        async def async_read(self) -> SolisSnapshot:
            if hang:
                await asyncio.sleep(1)
            return _snapshot(_inverter())

    router = SolisPriorityTransport("both", _local(), _Slow())
    caplog.set_level(logging.INFO, logger=lt.__name__)
    for _ in range(3):
        router._fill_due = 0.0
        assert await router._optional_cloud() is None
    budget = [record for record in caplog.records if "did not answer within" in record.message]
    assert len(budget) == 1
    hang = False
    router._fill_due = 0.0
    assert await router._optional_cloud() is not None
    assert router._budget_logged is False


# Finding 17: the setup probe's own close does not raise a firmware repair.


async def test_s2_closed_right_after_setup_probe_is_a_short_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = _local(kind=LOGGER_S2, protocol="", port=6607)
    key = (local._host, local._port)
    monkeypatch.setitem(lt._RECENT_S2_PROBES, key, time.monotonic())

    async def _refused() -> SolisSnapshot:
        raise SolisLoggerOffline("refused", refused=True)

    local._read_connected = _refused  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy) as wedged:
        await local.async_read()
    assert wedged.value.waiting
    assert wedged.value.reason == REASON_LOCKED
    assert local._backoff_step == 0
    assert time.monotonic() < local._next_attempt <= time.monotonic() + lt._S2_WEDGE_RETRY
    # Long after the probe, the same close is the firmware fault.
    monkeypatch.setitem(lt._RECENT_S2_PROBES, key, time.monotonic() - lt._S2_WEDGE - 1)
    local._next_attempt = 0.0
    with pytest.raises(SolisLoggerBusy) as closed:
        await local.async_read()
    assert closed.value.reason == REASON_TCP_DISABLED
    assert not closed.value.waiting
    assert local._backoff_step == 1
    # Once a sample was read, a close is never the setup wedge.
    monkeypatch.setitem(lt._RECENT_S2_PROBES, key, time.monotonic())
    local._last = _snapshot(_inverter())
    assert local._just_probed() is False


# Coordinator request: a rejected stick password reaches the coordinator.


async def test_auth_error_is_degraded_with_the_web_auth_reason() -> None:
    local = _local()
    local._last = _snapshot(_inverter())

    async def _rejected() -> SolisSnapshot:
        raise SolisLoggerAuthError("password rejected")

    local.async_read = _rejected  # type: ignore[method-assign]
    cloud = _Cloud(_snapshot(_inverter()))
    transport = SolisPriorityTransport("both", local, cloud)
    snapshot = await transport.async_read()
    # The real web transport sets degraded_reason="web_auth" (see test_web_stick).
    assert snapshot.source == "degraded"
