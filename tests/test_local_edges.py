"""Coverage for local framing, a locked logger, and serial adoption."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.solis import (
    _store_inverter_serial,
    _store_text,
    async_remove_config_entry_device,
)
from custom_components.solis.const import DOMAIN
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.errors import SolisLoggerBusy, SolisLoggerOffline, logger_failure_text
from custom_components.solis.local_transport import (
    LoggerProbe,
    SolisLocalTransport,
    SolisPriorityTransport,
    _cloud_is_fresh,
    _degraded_cloud,
    _detect_map,
    _mark_degraded,
    _prefer,
    _serial_once,
    asleep_snapshot,
    placeholder_serial,
    probe_logger,
)
from custom_components.solis.migration import async_adopt_inverter_serial
from custom_components.solis.models import InverterSnapshot, SolisSnapshot
from custom_components.solis.registers import _string_state, device_model_name, firmware_version
from custom_components.solis.solarman import (
    ModbusException,
    build_mbap,
    build_request,
    build_response,
    classify_io,
    crc16,
    decode_mbap,
    decode_read,
    modbus_from_request,
    rtu_read,
    take_frame,
)
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState


def _inverter(**kwargs: object) -> InverterSnapshot:
    base: dict[str, object] = {
        "serial": "110E40182280017",
        "device_id": "110E40182280017",
        "station_id": "local",
        "name": "110E40182280017",
        "model": "Solis 4G",
        "product_model": "1",
        "hmi_version": "0F000F",
        "state": 1,
        "ac_output_type": 0,
        "data_timestamp": datetime.now(UTC),
        "collector_serial": "1",
        "ac_power_w": 60.0,
        "frequency_hz": 50.0,
        "temperature_c": 27.0,
        "energy_today_kwh": 0.1,
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
        "grid_power_w": 10.0,
        "consumption_power_w": 20.0,
        "force_charge_soc": None,
        "force_discharge_soc": None,
        "phases": (),
        "strings": (),
        "model_code": None,
    }
    base.update(kwargs)
    return InverterSnapshot(**base)  # type: ignore[arg-type]


def _snapshot(inverter: InverterSnapshot, source: str = "local") -> SolisSnapshot:
    return SolisSnapshot(
        stations=(),
        inverters=(inverter,),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
        source=source,
    )


def test_labels_and_failure_text() -> None:
    """Model code, version and the two manual-read sentences."""
    assert device_model_name("Solis-mini-2500-4G", None) == "Solis-mini-2500-4G"
    assert device_model_name(None, "E4") == "E4"
    assert device_model_name(None, None) == "Solis inverter"
    assert firmware_version({}) is None
    assert _string_state(1) == 1
    assert _string_state(2) == 3
    assert "producing" in logger_failure_text("10.0.0.5", SolisLoggerBusy("reset"))
    assert "night" in logger_failure_text("10.0.0.5", SolisLoggerOffline("down"))
    assert placeholder_serial("", 1)
    assert placeholder_serial("L1", 1)
    assert not placeholder_serial("110E40182280017", 1)
    assert _prefer(None, 1.0) == 1.0
    assert _prefer(5.0, None) == 5.0
    assert _prefer(5.0, 4.0) == 5.0
    assert _prefer(5.0, 6.0) == 6.0


async def test_frame_edges() -> None:
    """Illegal lengths, a bad checksum and Modbus TCP exceptions are refused."""
    assert take_frame(b"nope") == (None, b"")
    short = bytes([0xA5, 0x10])
    assert take_frame(short)[0] is None
    bad_end = bytearray(build_request(1, 1, rtu_read(1, 3004, 1)))
    bad_end[-1] = 0x00
    assert take_frame(bytes(bad_end))[0] is None
    truncated = bytearray(30)
    truncated[0] = 0xA5
    truncated[1:3] = (80).to_bytes(2, "little")
    with pytest.raises(ValueError):
        modbus_from_request(bytes(truncated))
    huge = bytearray(build_request(1, 1, rtu_read(1, 1, 1)))
    huge[1:3] = (600).to_bytes(2, "little")
    with pytest.raises(ValueError):
        modbus_from_request(bytes(huge))
    body = bytes((1, 0x84, 2))
    with pytest.raises(ModbusException):
        decode_mbap(b"\x00\x01\x00\x00\x00\x03\x01\x84\x02")
    with pytest.raises(ModbusException):
        decode_read(body + crc16(body).to_bytes(2, "little"))
    offline = classify_io(TimeoutError(), connected=True)
    assert isinstance(offline, SolisLoggerOffline)
    refused = classify_io(ConnectionRefusedError(), connected=False)
    assert isinstance(refused, SolisLoggerOffline) and refused.refused
    assert isinstance(classify_io(ConnectionResetError(), connected=True), SolisLoggerBusy)
    with pytest.raises(RuntimeError):
        classify_io(RuntimeError("no"), connected=False)
    odd = bytes([1, 4, 1, 9])
    with pytest.raises(ValueError):
        decode_read(odd + crc16(odd).to_bytes(2, "little"))
    long_frame = bytearray(20)
    long_frame[0] = 0xA5
    long_frame[-1] = 0x15
    long_frame[1:3] = (600).to_bytes(2, "little")
    from custom_components.solis.solarman import _local_target, modbus_from_frame

    with pytest.raises(ValueError):
        modbus_from_frame(bytes(long_frame))
    with pytest.raises(ValueError):
        decode_mbap(b"\x00\x01\x00\x01\x00\x03\x01\x04\x00")
    with pytest.raises(ValueError):
        decode_mbap(b"\x00\x01\x00\x00\x00\x02\x01\x84")
    with pytest.raises(ValueError):
        decode_mbap(b"\x00\x01\x00\x00\x00\x03\x01\x03\x00")
    refused_os = OSError()
    refused_os.errno = 111
    assert classify_io(refused_os, connected=False).refused  # type: ignore[union-attr]
    down = OSError()
    down.errno = 113
    assert isinstance(classify_io(down, connected=False), SolisLoggerOffline)
    wrapped = ConnectionError("x")
    wrapped.__cause__ = ConnectionResetError()
    assert isinstance(classify_io(wrapped, connected=True), SolisLoggerBusy)
    assert isinstance(classify_io(SolisLoggerBusy("a"), connected=True), SolisLoggerBusy)
    assert isinstance(classify_io(SolisLoggerOffline("a"), connected=True), SolisLoggerOffline)
    assert isinstance(classify_io(ConnectionError("x"), connected=True), SolisLoggerOffline)
    assert isinstance(classify_io(ValueError("x"), connected=True), SolisLoggerOffline)
    assert _local_target("localhost")
    assert _local_target("10.1.1.1")
    assert not _local_target("8.8.8.8")
    assert not _local_target("not-an-ip")
    assert take_frame(b"\xa5")[0] is None
    assert take_frame(b"\xa5\x10\x00" + b"\x00" * 8)[0] is None
    with pytest.raises(ValueError):
        decode_mbap(b"\x00\x01\x00\x00\x00\x04\x01\x04\x01\x00")
    piped = OSError()
    piped.errno = 32
    assert isinstance(classify_io(piped, connected=True), SolisLoggerBusy)
    from custom_components.solis.solarman import SolarmanClient

    paced = SolarmanClient("127.0.0.1", 1, timeout=0.2, gap=0.01)
    await paced._pace()
    await paced._pace()


def test_degraded_overlay_keeps_a_higher_total() -> None:
    """Cloud 12500 kWh must not replace a local 21970 kWh total."""
    previous = _snapshot(_inverter())
    marked = _mark_degraded(previous)
    assert marked.source == "degraded"
    assert marked.inverters[0].ac_power_w == 60
    asleep = asleep_snapshot(previous)
    assert asleep.inverters[0].ac_power_w == 0
    assert asleep.inverters[0].grid_power_w == 0
    donor = _inverter(
        serial="CLOUD",
        model="Solis-mini-2500-4G",
        model_code="E4",
        ac_power_w=59.0,
        energy_total_kwh=12500.0,
        data_timestamp=datetime.now(UTC) - timedelta(hours=2),
    )
    cloud = _snapshot(donor, "cloud")
    assert _cloud_is_fresh(cloud) is False
    fresh_inverter = _inverter(
        serial="CLOUD",
        model="Solis-mini-2500-4G",
        model_code="E4",
        ac_power_w=59.0,
        energy_total_kwh=12500.0,
    )
    fresh = _snapshot(fresh_inverter)
    assert _cloud_is_fresh(fresh) is True
    local = SolisLocalTransport(
        "127.0.0.1", 1, slave=1, register_map="string", inverter_serial="L1", timeout=0.2, gap=0
    )
    local._last = previous
    merged = _degraded_cloud(local, fresh)
    assert merged.inverters[0].energy_total_kwh == 21970
    assert merged.inverters[0].serial == "CLOUD"
    assert merged.inverters[0].model == "Solis-mini-2500-4G"


async def test_v5_falls_back_to_modbus_tcp(socket_enabled: None) -> None:
    """A stick that closes V5 and answers MBAP is remembered as Modbus TCP."""
    state = StickState(tcp_port=0, udp_port=0, protocol="mbtcp")
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    transport = SolisLocalTransport(
        "127.0.0.1",
        SERIAL,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=0.4,
        gap=0,
        port=port,
    )
    try:
        snapshot = await transport.async_read()
    finally:
        await transport.close()
        await emulator.close()
    assert snapshot.inverters[0].energy_total_kwh == 21970
    assert transport._protocol == "mbtcp"


async def test_serial_register_failure_is_remembered(socket_enabled: None) -> None:
    """A reset while reading 3060 is cached. The 3000 map is kept."""
    state = StickState(tcp_port=0, udp_port=0, lock_above=3050)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        probed = await probe_logger("127.0.0.1", SERIAL, timeout=1, port=port, gap=0)
    finally:
        await emulator.close()
    assert probed.register_map == "string"
    assert probed.serial_unsupported is True
    assert probed.inverter_serial == f"L{SERIAL}"


async def test_user_serial_skips_the_serial_block(socket_enabled: None) -> None:
    """A typed inverter serial is not looked up in the locking register range."""
    state = StickState(tcp_port=0, udp_port=0, lock_above=3050)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        probed = await probe_logger(
            "127.0.0.1",
            SERIAL,
            timeout=1,
            port=port,
            gap=0,
            inverter_serial="110E40182280017",
        )
    finally:
        await emulator.close()
    assert probed == LoggerProbe(1, "string", "110E40182280017", "v5", False)


async def test_detect_map_and_serial_helpers() -> None:
    """Exception 2 on 3004 selects the hybrid map. A reset stops the serial read."""

    class _Hybrid:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            if address < 33000:
                raise ModbusException(2)
            return [4]

    class _Busy:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            raise SolisLoggerBusy("reset")

    assert await _detect_map(_Hybrid(), 1) == "hybrid"  # type: ignore[arg-type]
    serial, unsupported = await _serial_once(
        _Busy(), 1, known="", unsupported=False, logger_serial=7  # type: ignore[arg-type]
    )
    assert serial == "L7" and unsupported is True
    kept, flag = await _serial_once(
        _Busy(), 1, known="ABC", unsupported=True, logger_serial=7  # type: ignore[arg-type]
    )
    assert kept == "ABC" and flag is True


async def test_reconfigure_reports_a_reset(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """Attaching a stick that resets says busy, not asleep."""
    from custom_components.solis import config_flow as flow

    async def _busy(*_args: object, **_kwargs: object) -> None:
        raise SolisLoggerBusy("reset")

    monkeypatch.setattr(flow, "probe_logger", _busy)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "api_url": "http://127.0.0.1:9",
            "key_id": "k",
            "key_secret": "s",
            "station_ids": ["1"],
            "source": "cloud",
        },
        options={"scan_interval": 300, "request_timeout": 5, "endpoint_interval": 0.5},
        version=3,
    )
    entry.add_to_hass(hass)

    async def _stations(_hass: object, _data: object) -> dict[str, str]:
        return {"1": "Plant"}

    monkeypatch.setattr(flow, "_fetch_stations", _stations)
    started = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            "api_url": "http://127.0.0.1:9",
            "station_ids": ["1"],
            "scan_interval": 60,
            "request_timeout": 5,
            "endpoint_interval": 0.5,
            "logger_host": "127.0.0.1",
            "logger_serial": "1902137671",
            "inverter_serial": "110E40182280017",
        },
    )
    assert result["errors"]["base"] == "logger_busy"


async def test_degraded_hold_keeps_morning_power(hass: HomeAssistant) -> None:
    """A reset keeps 60 W and opens no repair until the pause has lasted 10 minutes."""
    from homeassistant.helpers import issue_registry as ir

    inverter = _inverter()

    class _Script:
        def __init__(self) -> None:
            self._calls = 0

        async def async_read(self) -> SolisSnapshot:
            self._calls += 1
            if self._calls == 1:
                return _snapshot(inverter)
            if self._calls == 2:
                return _snapshot(inverter, "degraded")
            if self._calls >= 5:
                return _snapshot(inverter)
            raise SolisLoggerBusy("reset")

    entry = MockConfigEntry(domain=DOMAIN, data={"source": "local"}, options={}, version=3)
    entry.add_to_hass(hass)
    coordinator = SolisCoordinator(hass, entry, _Script())
    first = await coordinator._async_update_data()
    assert first.inverters[0].ac_power_w == 60
    second = await coordinator._async_update_data()
    assert second.source == "degraded"
    assert coordinator.logger_report()["state"] == "degraded"
    third = await coordinator._async_update_data()
    assert third.inverters[0].ac_power_w == 60
    assert third.inverters[0].data_source == "degraded"
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert not any(issue_id.startswith("logger_locked") for issue_id in issues)
    coordinator._degraded_since = 0
    await coordinator._async_update_data()
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert any(issue_id.startswith("logger_locked") for issue_id in issues)
    recovered = await coordinator._async_update_data()
    assert recovered.source == "local"


async def test_adopt_moves_entity_and_device_ids(hass: HomeAssistant) -> None:
    """L plus the logger serial becomes the SolisCloud inverter serial."""
    entry = MockConfigEntry(domain=DOMAIN, data={"inverter_serial": "L1"}, version=3)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "L1_energy_total",
        config_entry=entry,
        suggested_object_id="old_total",
    )
    devices = dr.async_get(hass)
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "inverter:L1")},
        name="old",
    )
    async_adopt_inverter_serial(hass, entry.entry_id, "L1", "110E40182280017")
    assert registry.async_get_entity_id("sensor", DOMAIN, "110E40182280017_energy_total")
    found = devices.async_get_device_by_identifier(
        (DOMAIN, "inverter:110E40182280017"), entry.entry_id
    )
    assert found is not None
    async_adopt_inverter_serial(hass, entry.entry_id, "", "110E40182280017")
    async_adopt_inverter_serial(hass, entry.entry_id, "110E40182280017", "110E40182280017")
    _store_text(hass, entry, "logger_protocol", "v5")
    _store_text(hass, entry, "logger_protocol", "v5")
    _store_inverter_serial(hass, entry, "")
    _store_inverter_serial(hass, entry, "L1")
    _store_inverter_serial(hass, entry, "110E40182280017")
    assert entry.data["inverter_serial"] == "110E40182280017"

    class _Device:
        identifiers = frozenset({("other", "x")})

    assert await async_remove_config_entry_device(hass, entry, _Device()) is False  # type: ignore[arg-type]


def test_checksum_and_mbap_decode() -> None:
    """A short MBAP body and a bad transaction length are rejected."""
    with pytest.raises(ValueError):
        decode_mbap(b"\x00\x01\x00\x00\x00\x01\x01")
    frame = build_mbap(3, 1, 3004, 1)
    pdu = bytes((0x04, 2, 0x00, 0x3C))
    good = frame[:2] + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + bytes((1,)) + pdu
    assert decode_mbap(good) == [60]
    response = build_response(SERIAL, 1, rtu_read(1, 3004, 1))
    with pytest.raises(ValueError):
        from custom_components.solis.solarman import modbus_from_frame

        broken = bytearray(response)
        broken[-2] ^= 0xFF
        modbus_from_frame(bytes(broken))


async def test_heartbeat_is_skipped(socket_enabled: None) -> None:
    """An unsolicited 0x4710 frame does not hide the Modbus response."""
    from custom_components.solis.solarman import SolarmanClient

    request_holder: list[bytes] = []

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(256)
        request_holder.append(data)
        heartbeat = bytes.fromhex("a501001047010000000000005d15")
        writer.write(heartbeat + build_response(SERIAL, 0x2201, _reply()))
        await writer.drain()
        writer.close()

    def _reply() -> bytes:
        body = bytes((1, 0x04, 2, 0x00, 0x3C))
        return body + crc16(body).to_bytes(2, "little")

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=1, gap=0)
    try:
        assert await client.read_input(1, 3004, 1) == [60]
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
    assert request_holder


async def test_priority_local_only_holds_the_last_sample(socket_enabled: None) -> None:
    """Without cloud, a second reset republishes the last power and does not open a socket."""
    state = StickState(tcp_port=0, udp_port=0, locked=True)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    local = SolisLocalTransport(
        "127.0.0.1",
        SERIAL,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=0.4,
        gap=0,
        port=port,
    )
    local._last = _snapshot(_inverter(battery_power_w=5.0))
    router = SolisPriorityTransport("local", local, None)
    try:
        held = await router.async_read()
    finally:
        await router.close()
        await emulator.close()
    assert held.source == "degraded"
    assert held.inverters[0].ac_power_w == 60
    assert held.inverters[0].battery_power_w == 5


async def test_router_edges() -> None:
    """Cloud failures and a paused logger without a previous sample stay explicit."""
    from custom_components.solis.errors import SolisCloudError

    local = SolisLocalTransport(
        "127.0.0.1", 1, slave=1, register_map="string", inverter_serial="L1", timeout=0.2, gap=0
    )

    async def _offline() -> SolisSnapshot:
        raise SolisLoggerOffline("down")

    local.async_read = _offline  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline):
        await SolisPriorityTransport("local", local, None).async_read()

    async def _busy() -> SolisSnapshot:
        raise SolisLoggerBusy("reset")

    local.async_read = _busy  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy):
        await SolisPriorityTransport("local", local, None).async_read()

    class _Boom:
        async def async_read(self) -> SolisSnapshot:
            raise SolisCloudError("down")

    async def _asleep() -> SolisSnapshot:
        return asleep_snapshot(_snapshot(_inverter()))

    local.async_read = _asleep  # type: ignore[method-assign]
    filled = await SolisPriorityTransport("both", local, _Boom()).async_read()
    assert filled.source == "asleep"
    empty = SolisSnapshot(
        stations=(),
        inverters=(),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
    )
    assert _cloud_is_fresh(empty) is False
    naive = _inverter(data_timestamp=datetime(2026, 1, 1))
    assert _cloud_is_fresh(_snapshot(naive)) is False
    assert _cloud_is_fresh(_snapshot(_inverter(data_timestamp=None))) is True
    local._log_asleep()
    local._log_asleep()
    noted: list[str] = []
    local._on_protocol = noted.append
    local._remember("v5")
    local._remember("v5")
    assert noted == ["v5"]
    await local._open("mbtcp")
    await local._open("mbtcp30003")
    await local.close()

    class _Bad:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            raise ValueError("bad")

    serial, flag = await _serial_once(
        _Bad(), 1, known="", unsupported=False, logger_serial=3  # type: ignore[arg-type]
    )
    assert serial == "L3" and flag

    class _Reset:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            raise SolisLoggerBusy("reset")

    with pytest.raises(SolisLoggerBusy):
        await _detect_map(_Reset(), 1)  # type: ignore[arg-type]


async def test_offline_adopts_a_new_address_and_a_refused_port() -> None:
    """Discovery of a new address is one reconnect. The same address refusing is busy."""
    local = SolisLocalTransport(
        "10.0.0.1", 1, slave=1, register_map="string", inverter_serial="S", gap=0, timeout=0.2
    )
    seen: list[str] = []
    local._on_host = lambda host, _mac: seen.append(host)

    async def _new() -> str:
        return "10.0.0.2"

    async def _ok() -> SolisSnapshot:
        return _snapshot(_inverter())

    local._find_stick = _new  # type: ignore[method-assign]
    local._read_connected = _ok  # type: ignore[method-assign]
    adopted = await local._after_offline(SolisLoggerOffline("down"))
    assert adopted.source == "local"
    assert seen == ["10.0.0.2"]

    async def _same() -> str:
        return "10.0.0.2"

    local._find_stick = _same  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy):
        await local._after_offline(SolisLoggerOffline("closed", refused=True))

    async def _moved() -> str:
        return "10.0.0.3"

    async def _still() -> SolisSnapshot:
        raise SolisLoggerOffline("still", connected=True)

    local._find_stick = _moved  # type: ignore[method-assign]
    local._read_connected = _still  # type: ignore[method-assign]
    local._last = _snapshot(_inverter())
    asleep = await local._after_offline(SolisLoggerOffline("down", connected=True))
    assert asleep.source == "asleep"

    async def _open(protocol: str) -> object:
        class _Down:
            _port = 9

            async def read_input(self, slave: int, address: int, count: int) -> list[int]:
                raise SolisLoggerOffline("no frame", connected=True)

            async def close(self) -> None:
                return None

        return _Down()

    local._active = "mbtcp"
    await SolisLocalTransport._open(local, "v5")
    bare = SolisLocalTransport(
        "127.0.0.1", 2, slave=1, register_map="string", inverter_serial="L2", gap=0, timeout=0.2
    )

    async def _down() -> SolisSnapshot:
        raise SolisLoggerOffline("down")

    bare.async_read = _down  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline):
        await SolisPriorityTransport("both", bare, None).async_read()

    async def _boom() -> SolisSnapshot:
        raise SolisLoggerBusy("reset")

    local._read_connected = _boom  # type: ignore[method-assign]

    async def _elsewhere() -> str:
        return "10.9.9.9"

    local._find_stick = _elsewhere  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerBusy):
        await local._after_offline(SolisLoggerOffline("down"))
    local._protocol = ""
    local._open = _open  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline):
        await local._read_words()
    local._candidates = lambda: ()  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline):
        await local._read_words()
    local._client = None
    await local._drop()
    local.adopt_serial("")
    local.adopt_serial("S")
    heard: list[str] = []
    local._on_serial = heard.append
    local.adopt_serial("110E40182280017")
    assert heard == ["110E40182280017"]
    await local._open("not-v5")
    await local._open("v5")


async def test_bad_peer_frames(socket_enabled: None) -> None:
    """Wrong serial, a bad checksum and a mismatched MBAP transaction are dropped."""
    from custom_components.solis.solarman import ModbusTcpClient, SolarmanClient

    async def _v5(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(128)
        bad = bytearray(build_response(SERIAL, 1, rtu_read(1, 3004, 1)))
        bad[-2] ^= 0xFF
        writer.write(bad)
        await writer.drain()

    server = await asyncio.start_server(_v5, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.4, gap=0.01)
    try:
        with pytest.raises(SolisLoggerOffline):
            await client.read_input(1, 3004, 1)
        with pytest.raises(SolisLoggerOffline):
            await client.read_input(1, 3004, 1)
    finally:
        await client.close()
        server.close()
        await server.wait_closed()

    async def _mbap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(32)
        writer.write(b"\x00\x09\x00\x00\x00\x03\x01\x04\x00")
        await writer.drain()

    other = await asyncio.start_server(_mbap, "127.0.0.1", 0)
    mbap_port = other.sockets[0].getsockname()[1]
    async def _wrong_serial(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(64)
        writer.write(build_response(1, 1, rtu_read(1, 3004, 1)))
        await writer.drain()
        await asyncio.sleep(0.5)

    weird = await asyncio.start_server(_wrong_serial, "127.0.0.1", 0)
    weird_port = weird.sockets[0].getsockname()[1]
    other_client = SolarmanClient("127.0.0.1", SERIAL, port=weird_port, timeout=0.3, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline):
            await other_client.read_input(1, 3004, 1)
    finally:
        await other_client.close()
        weird.close()
        await weird.wait_closed()

    async def _long_mbap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(32)
        writer.write(b"\x00\x01\x00\x00\xff\xff")
        await writer.drain()

    long_server = await asyncio.start_server(_long_mbap, "127.0.0.1", 0)
    long_port = long_server.sockets[0].getsockname()[1]
    long_client = ModbusTcpClient("127.0.0.1", port=long_port, timeout=0.4, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline):
            await long_client.read_input(1, 3004, 1)
    finally:
        await long_client.close()
        long_server.close()
        await long_server.wait_closed()

    tcp = ModbusTcpClient("127.0.0.1", port=mbap_port, timeout=0.4, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline):
            await tcp.read_input(1, 3004, 1)
    finally:
        await tcp.close()
        other.close()
        await other.wait_closed()
