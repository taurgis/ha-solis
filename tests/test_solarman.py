"""Solarman framing, the string-inverter map, and local-first routing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import entry_data, entry_options
from custom_components.solis.const import DOMAIN, SOURCE_BOTH, SOURCE_LOCAL
from custom_components.solis.errors import SolisCloudError, SolisLoggerBusy, SolisLoggerOffline
from custom_components.solis.local_transport import (
    SolisLocalTransport,
    SolisPriorityTransport,
    probe_logger,
)
from custom_components.solis.models import InverterSnapshot, SolisSnapshot
from custom_components.solis.registers import decode_string
from custom_components.solis.solarman import (
    SolarmanClient,
    build_request,
    discover_sticks,
    logger_serial_of,
    modbus_from_frame,
    rtu_read,
)
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState, captured_registers


def test_captured_exception_frame_is_modbus_2() -> None:
    """The real 33022 reply is exception 2, and the serial is 1902137671."""
    frame = bytes.fromhex("a513001015b117475160710201f3653c080704000063bab762018402c2c15315")
    assert logger_serial_of(frame) == 1902137671
    modbus = modbus_from_frame(frame)
    assert modbus[0] == 1
    assert modbus[1] == 0x84
    assert modbus[2] == 2


def test_string_map_matches_the_captured_registers() -> None:
    """The sunrise capture is 21970 kWh, 5.9 kWh yesterday and 278.4 V on PV1."""
    parsed = decode_string(
        captured_registers(),
        serial="SOLIS001",
        logger_serial=str(SERIAL),
        now=datetime(2026, 11, 11, 8, 0, tzinfo=UTC),
    )
    assert parsed.energy_total_kwh == 21970
    assert parsed.energy_month_kwh == 30
    assert parsed.energy_year_kwh == 2690
    assert parsed.energy_today_kwh == 0
    assert parsed.ac_power_w == 0
    assert parsed.strings[0].voltage_v == pytest.approx(278.4)
    assert parsed.strings[0].current_a == pytest.approx(1.3)
    assert parsed.phases[0].voltage_v == pytest.approx(234.9)
    assert parsed.temperature_c == pytest.approx(27.2)
    assert parsed.frequency_hz == pytest.approx(49.99)
    assert parsed.state is None
    assert parsed.serial == "SOLIS001"


async def test_emulator_answers_string_reads_and_rejects_hybrid(socket_enabled: None) -> None:
    """Slave 1 serves the 3000 map and exception 2 for register 33022."""
    state = StickState(tcp_port=0, udp_port=0)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    host, port = emulator._tcp.sockets[0].getsockname()[:2]
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=2, gap=0)
    try:
        probe = await probe_logger("127.0.0.1", SERIAL, timeout=2, port=port)
        assert probe.register_map == "string"
        assert probe.slave == 1
        assert probe.inverter_serial == "SOLIS001"
        power = await client.read_input(1, 3004, 2)
        assert power == [0, 0]
        with pytest.raises(Exception, match="exception 2"):
            await client.read_input(1, 33022, 1)
    finally:
        await client.close()
        await emulator.close()
    assert host


async def test_wrong_serial_is_silent_and_night_closes_the_port(socket_enabled: None) -> None:
    """A mismatched serial gets no bytes. Night stops listening entirely."""
    state = StickState(tcp_port=0, udp_port=0)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    wrong = SolarmanClient("127.0.0.1", 1, port=port, timeout=0.4, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline):
            await wrong.read_input(1, 3004, 1)
    finally:
        await wrong.close()
    await emulator.apply({"night": True})
    asleep = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.4, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline):
            await asleep.read_input(1, 3004, 1)
    finally:
        await asleep.close()
        await emulator.close()


async def test_discovery_and_ip_change(socket_enabled: None) -> None:
    """UDP discovery returns the stick, and a night outage keeps the energy total."""
    state = StickState(host="127.0.0.1", tcp_port=0, udp_port=48899)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        found = await discover_sticks(timeout=0.4)
        match = next(stick for stick in found if stick.serial == SERIAL)
        assert match.host == "127.0.0.1"
        assert match.mac == "bc:54:f9:f5:7b:5c"
        seen: list[str] = []
        transport = SolisLocalTransport(
            "127.0.0.1",
            SERIAL,
            slave=1,
            register_map="string",
            inverter_serial="SOLIS001",
            timeout=2,
            gap=0,
            on_host=lambda host, _mac: seen.append(host),
        )
        transport._client._port = port
        first = await transport.async_read()
        assert first.source == "local"
        assert await transport._find_stick() == "127.0.0.1"
        assert first.inverters[0].energy_total_kwh == 21970
        await emulator.apply({"night": True})
        asleep = await transport.async_read()
        assert asleep.source == "asleep"
        assert asleep.inverters[0].ac_power_w == 0
        assert asleep.inverters[0].energy_total_kwh == 21970
        state.tcp_port = port
        await emulator.apply({"night": False, "host": "127.0.0.1"})
        woken = await transport.async_read()
        assert woken.source == "local"
        assert seen == []
    finally:
        await emulator.close()


def test_frame_and_discovery_errors() -> None:
    """Bad frames and discovery text are rejected."""
    from custom_components.solis.registers import decode_hybrid, s32, serial_from_words
    from custom_components.solis.solarman import (
        ModbusException,
        decode_read,
        format_mac,
        modbus_from_request,
        parse_discovery,
    )

    assert format_mac("zz") == "zz"
    assert parse_discovery(b"nope") is None
    assert parse_discovery(b"1.2.3.4,abcd,nope") is None
    with pytest.raises(ValueError):
        modbus_from_request(b"\x00" * 10)
    with pytest.raises(ValueError):
        decode_read(b"\x01\x03\x02\x00\x01")
    from custom_components.solis.solarman import crc16

    exception = bytes.fromhex("018302")
    with pytest.raises(ModbusException):
        decode_read(exception + crc16(exception).to_bytes(2, "little"))
    with pytest.raises(ValueError):
        decode_read(exception + b"\x00\x00")
    assert serial_from_words({}) == ""
    assert s32({1: 0xFFFF, 2: 0xFFFF}, 1) == -1
    hybrid = decode_hybrid(
        {
            33149: 0,
            33150: 500,
            33135: 1,
            33134: 40,
            33133: 500,
            33139: 80,
            33035: 12,
            33029: 0,
            33030: 10,
        },
        serial="H1",
        logger_serial="1",
    )
    assert hybrid.battery_power_w == -500
    assert hybrid.battery_current_a == pytest.approx(-4.0)
    assert hybrid.product_model == "2"
    three = decode_string({3002: 1, 3033: 2300, 3035: 2310}, serial="T", logger_serial="1")
    assert len(three.phases) == 3


def _tiny_cloud() -> SolisPriorityTransport:
    inverter = InverterSnapshot(
        serial="CLOUD",
        device_id="1",
        station_id="p",
        name="Cloud",
        model=None,
        product_model="1",
        hmi_version=None,
        state=1,
        ac_output_type=0,
        data_timestamp=datetime.now(UTC),
        collector_serial=None,
        ac_power_w=10,
        frequency_hz=50,
        temperature_c=20,
        energy_today_kwh=1,
        energy_month_kwh=2,
        energy_year_kwh=3,
        energy_total_kwh=12500,
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
    )

    class _Cloud:
        async def async_read(self) -> SolisSnapshot:
            return SolisSnapshot(
                stations=(),
                inverters=(inverter,),
                alarms=(),
                collectors=(),
                epms=(),
                fetched_at=datetime.now(UTC),
                partial=False,
            )

    return SolisPriorityTransport("cloud", None, _Cloud())


async def test_register_map_detection() -> None:
    """Exception 2 selects the string map. A silent slave selects nothing."""
    from custom_components.solis.local_transport import _detect_map, _read_cloud
    from custom_components.solis.solarman import ModbusException

    class _String:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            if address == 33022:
                raise ModbusException(2)
            return [1, 2]

    class _Hybrid:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            if address < 33000:
                raise ModbusException(2)
            return [1]

    class _Dead:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            raise SolisLoggerOffline("down")

    assert await _detect_map(_String(), 1) == "string"  # type: ignore[arg-type]
    assert await _detect_map(_Hybrid(), 1) == "hybrid"  # type: ignore[arg-type]
    assert await _detect_map(_Dead(), 1) is None  # type: ignore[arg-type]
    with pytest.raises(SolisLoggerOffline):
        await _read_cloud(None)


def test_protocol_rejections() -> None:
    """Checksum, short frames and a bad function code are refused."""
    from custom_components.solis.solarman import decode_read, logger_serial_of, modbus_from_frame

    built = build_request(SERIAL, 1, rtu_read(1, 3004, 2))
    broken = bytearray(built)
    broken[-2] ^= 0xFF
    with pytest.raises(ValueError):
        modbus_from_frame(bytes(broken))
    with pytest.raises(ValueError):
        logger_serial_of(b"\x00")
    claimed = bytearray(30)
    claimed[0] = 0xA5
    claimed[-1] = 0x15
    claimed[1:3] = (80).to_bytes(2, "little")
    with pytest.raises(ValueError):
        modbus_from_frame(bytes(claimed))
    tiny = bytearray(23)
    tiny[0] = 0xA5
    tiny[-1] = 0x15
    tiny[1:3] = (10).to_bytes(2, "little")
    tiny[-2] = sum(tiny[1:-2]) & 0xFF
    with pytest.raises(ValueError):
        modbus_from_frame(bytes(tiny))
    request = bytearray(30)
    request[0] = 0xA5
    request[1:3] = (80).to_bytes(2, "little")
    with pytest.raises(ValueError):
        from custom_components.solis.solarman import modbus_from_request

        modbus_from_request(bytes(request))
    with pytest.raises(ValueError):
        decode_read(b"\x01\x10\x02\x00\x01\x00\x00")
    with pytest.raises(ValueError):
        decode_read(b"\x01\x04")
    with pytest.raises(ValueError):
        decode_read(bytes([1, 4, 4, 0, 1, 0, 0]))
    with pytest.raises(ValueError):
        decode_read(bytes([1, 4, 2, 0, 1, 0, 0]))
    short = bytearray(built[:20])
    with pytest.raises(ValueError):
        modbus_from_frame(bytes(short))


def test_request_frame_carries_the_serial() -> None:
    """The header serial is the logger serial, little endian."""
    frame = build_request(SERIAL, 1, rtu_read(1, 3004, 2))
    assert frame[0] == 0xA5
    assert frame[-1] == 0x15
    assert logger_serial_of(frame) == SERIAL


async def test_priority_keeps_local_totals_when_the_logger_sleeps(socket_enabled: None) -> None:
    """Night does not replace a higher local total with the cloud's number."""
    state = StickState(tcp_port=0, udp_port=0)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]

    class _Cloud:
        async def async_read(self) -> SolisSnapshot:
            inverter = InverterSnapshot(
                serial="CLOUD",
                device_id="1",
                station_id="p",
                name="Cloud",
                model=None,
                product_model="1",
                hmi_version=None,
                state=1,
                ac_output_type=0,
                data_timestamp=datetime.now(UTC),
                collector_serial=None,
                ac_power_w=1250,
                frequency_hz=50,
                temperature_c=20,
                energy_today_kwh=4,
                energy_month_kwh=10,
                energy_year_kwh=100,
                energy_total_kwh=12500,
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
            )
            return SolisSnapshot(
                stations=(),
                inverters=(inverter,),
                alarms=(),
                collectors=(),
                epms=(),
                fetched_at=datetime.now(UTC),
                partial=False,
            )

    local = SolisLocalTransport(
        "127.0.0.1",
        SERIAL,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=2,
        gap=0,
    )
    local._client._port = port
    router = SolisPriorityTransport(SOURCE_BOTH, local, _Cloud())
    try:
        live = await router.async_read()
        assert live.source == "local"
        assert live.inverters[0].energy_total_kwh == 21970
        await emulator.apply({"night": True})
        sleeping = await router.async_read()
        assert sleeping.source == "asleep"
        assert sleeping.inverters[0].ac_power_w == 0
        assert sleeping.inverters[0].energy_total_kwh == 21970
    finally:
        await local.close()
        await emulator.close()


async def test_local_entry_uses_the_stick(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """A local-only entry exposes the stick's totals and survives the night."""
    del solis_server
    state = StickState(tcp_port=8899, udp_port=0)
    emulator = StickEmulator(state)
    await emulator.start()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "127.0.0.1",
            "logger_serial": str(SERIAL),
            "logger_mac": "bc:54:f9:f5:7b:5c",
            "slave_id": 1,
            "register_map": "string",
            "inverter_serial": "SOLIS001",
            "api_url": "http://127.0.0.1:9",
            "key_id": "",
            "key_secret": "",
            "station_ids": [],
        },
        options=entry_options(),
        version=3,
        unique_id=str(SERIAL),
    )
    entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        total_id = registry.async_get_entity_id("sensor", DOMAIN, "SOLIS001_energy_total")
        power_id = registry.async_get_entity_id("sensor", DOMAIN, "SOLIS001_ac_power")
        source = registry.async_get_entity_id("sensor", DOMAIN, "SOLIS001_data_source")
        assert total_id is not None and power_id is not None and source is not None
        assert float(hass.states.get(total_id).state) == 21970  # type: ignore[union-attr]
        assert hass.states.get(source).state == "local"  # type: ignore[union-attr]
        await emulator.apply({"night": True})
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(source).state == "asleep"  # type: ignore[union-attr]
        assert float(hass.states.get(total_id).state) == 21970  # type: ignore[union-attr]
        assert float(hass.states.get(power_id).state) == 0  # type: ignore[union-attr]
    finally:
        await emulator.close()


async def test_flow_and_dhcp_follow_the_logger_serial(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Setup can target the stick, and DHCP rewrites its address."""
    del solis_server
    from homeassistant.data_entry_flow import FlowResultType

    state = StickState(tcp_port=8899, udp_port=48899)
    emulator = StickEmulator(state)
    await emulator.start()
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "source": SOURCE_LOCAL,
                "api_url": "http://127.0.0.1:9",
                "key_id": "",
                "key_secret": "",
            },
        )
        assert result["step_id"] == "logger"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"logger_host": "127.0.0.1", "logger_serial": str(SERIAL)},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["result"].unique_id == str(SERIAL)
    finally:
        await emulator.close()


async def test_dhcp_updates_the_logger_address(hass: HomeAssistant, socket_enabled: None) -> None:
    """DHCP rewrites the stored IP when the MAC matches the logger."""
    from homeassistant.config_entries import SOURCE_DHCP
    from homeassistant.data_entry_flow import FlowResultType

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "10.0.0.8",
            "logger_serial": str(SERIAL),
            "logger_mac": "bc:54:f9:f5:7b:5c",
            "slave_id": 1,
            "register_map": "string",
            "inverter_serial": "SOLIS001",
        },
        version=3,
        unique_id=str(SERIAL),
    )
    entry.add_to_hass(hass)
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "bc:54:f9:f5:7b:5c", "hostname": "stick"},
    )
    assert discovered["type"] is FlowResultType.ABORT
    assert discovered["reason"] == "already_configured"
    assert entry.data["logger_host"] == "127.0.0.1"
    ignored = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.0.0.9", "macaddress": "aa:bb:cc:dd:ee:ff", "hostname": "other"},
    )
    assert ignored["reason"] == "not_solis_logger"
    missing = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.0.0.10", "macaddress": "bc:54:f9:00:00:01", "hostname": "new"},
    )
    assert missing["reason"] == "logger_not_setup"

    class _Dhcp:
        ip = "127.0.0.1"
        macaddress = "aa:bb:cc:dd:ee:ff"
        hostname = "nope"

    via_attr = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data=_Dhcp(),
    )
    assert via_attr["reason"] == "not_solis_logger"
    same = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "127.0.0.1",
            "logger_serial": "999",
            "logger_mac": "bc:54:f9:11:22:33",
        },
        version=3,
        unique_id="999",
    )
    same.add_to_hass(hass)
    unchanged = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "bc:54:f9:11:22:33", "hostname": "stick"},
    )
    assert unchanged["reason"] == "already_configured"
    assert same.data["logger_host"] == "127.0.0.1"
    other = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "10.1.1.1",
            "logger_serial": "1000",
            "logger_mac": "bc:54:f9:11:22:44",
        },
        version=3,
        unique_id="not-the-serial",
    )
    other.add_to_hass(hass)
    moved = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "bc54f9112244", "hostname": "stick"},
    )
    assert moved["reason"] == "already_configured"
    assert other.data["logger_host"] == "127.0.0.1"


async def test_logger_reset_is_not_reported_as_night(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """A reset during setup says the logger is busy, not that it is night."""
    from custom_components.solis import config_flow as flow
    from custom_components.solis.errors import SolisLoggerBusy

    async def _busy(*_args: object, **_kwargs: object) -> None:
        raise SolisLoggerBusy("reset")

    async def _no_discovery(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(flow, "probe_logger", _busy)
    monkeypatch.setattr(flow, "discover_sticks", _no_discovery)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"source": "local", "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    failed = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "logger_host": "127.0.0.1",
            "logger_serial": "1902137671",
            "inverter_serial": "110E40182280017",
        },
    )
    assert failed["errors"]["base"] == "logger_busy"


async def test_empty_cloud_key_is_rejected(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Cloud mode still requires an API key."""
    del solis_server
    from homeassistant.data_entry_flow import FlowResultType

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"source": "cloud", "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


async def test_reconfigure_local_and_attach(
    hass: HomeAssistant, solis_server: tuple[Any, str], socket_enabled: None
) -> None:
    """A local entry reopens the logger step, and a cloud entry can gain a stick."""
    from homeassistant.data_entry_flow import FlowResultType

    _state, url = solis_server
    local = MockConfigEntry(
        domain=DOMAIN,
        data={"source": SOURCE_LOCAL, "logger_host": "127.0.0.1", "logger_serial": "1"},
        options=entry_options(),
        version=3,
        unique_id="1",
    )
    local.add_to_hass(hass)
    opened = await local.start_reconfigure_flow(hass)
    assert opened["step_id"] == "logger"
    failed = await hass.config_entries.flow.async_configure(
        opened["flow_id"],
        {"logger_host": "127.0.0.1", "logger_serial": "1"},
    )
    assert failed["errors"]["base"] == "logger_asleep"
    cloud = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        version=3,
        unique_id="test-key",
    )
    cloud.add_to_hass(hass)
    assert await hass.config_entries.async_setup(cloud.entry_id)
    await hass.async_block_till_done()
    started = await cloud.start_reconfigure_flow(hass)
    refused = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            "api_url": url,
            "station_ids": ["1900000000000001001", "1900000000000001002"],
            "scan_interval": 120,
            "request_timeout": 30,
            "endpoint_interval": 0.5,
            "logger_host": "127.0.0.1",
            "logger_serial": "1",
        },
    )
    assert refused["errors"]["base"] == "logger_asleep"
    stick = StickState(tcp_port=8899, udp_port=0)
    emulator = StickEmulator(stick)
    await emulator.start()
    try:
        retried = await hass.config_entries.flow.async_configure(
            failed["flow_id"],
            {"logger_host": "127.0.0.1", "logger_serial": str(SERIAL)},
        )
        assert retried["reason"] == "reconfigure_successful"
        updated = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            {
                "api_url": url,
                "station_ids": ["1900000000000001001", "1900000000000001002"],
                "scan_interval": 120,
                "request_timeout": 30,
                "endpoint_interval": 0.5,
                "logger_host": "127.0.0.1",
                "logger_serial": str(SERIAL),
            },
        )
    finally:
        await emulator.close()
    assert updated["type"] is FlowResultType.ABORT
    assert updated["reason"] == "reconfigure_successful"
    assert cloud.data["register_map"] == "string"
    assert cloud.unique_id == str(SERIAL)


async def test_both_creates_one_entry(
    hass: HomeAssistant, solis_server: tuple[Any, str], socket_enabled: None
) -> None:
    """Local plus cloud is a single entry keyed by the logger serial."""
    from homeassistant.data_entry_flow import FlowResultType

    from mock_soliscloud.catalog import KEY_ID, SECRET

    _state, url = solis_server
    emulator = StickEmulator(StickState(tcp_port=8899, udp_port=0))
    await emulator.start()
    try:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"source": "both", "api_url": url, "key_id": KEY_ID, "key_secret": SECRET},
        )
        assert result["step_id"] == "stations"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"station_ids": ["1900000000000001001"]},
        )
        assert result["step_id"] == "logger"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"logger_host": "127.0.0.1", "logger_serial": str(SERIAL)},
        )
    finally:
        await emulator.close()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data["source"] == "both"


async def test_routing_and_migration(hass: HomeAssistant, socket_enabled: None) -> None:
    """Cloud-only routing, missing energy, a version-2 entry, and a dead config."""
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.solis import _known_device_keys, _store_logger_host, async_migrate_entry
    from custom_components.solis.local_transport import _fill_missing_energy

    routed = await _tiny_cloud().async_read()
    assert routed.source == "cloud"
    local = routed.inverters[0]
    filled = _fill_missing_energy(
        SolisSnapshot(
            stations=(),
            inverters=(
                InverterSnapshot(
                    serial="L",
                    device_id="L",
                    station_id="local",
                    name="L",
                    model=None,
                    product_model="1",
                    hmi_version=None,
                    state=1,
                    ac_output_type=0,
                    data_timestamp=datetime.now(UTC),
                    collector_serial=None,
                    ac_power_w=0,
                    frequency_hz=None,
                    temperature_c=None,
                    energy_today_kwh=None,
                    energy_month_kwh=None,
                    energy_year_kwh=None,
                    energy_total_kwh=None,
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
                ),
            ),
            alarms=(),
            collectors=(),
            epms=(),
            fetched_at=datetime.now(UTC),
            partial=False,
            source="asleep",
        ),
        routed,
    )
    assert filled.inverters[0].energy_total_kwh == local.energy_total_kwh
    legacy = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data("http://127.0.0.1:9"),
        options=entry_options(),
        version=2,
    )
    legacy.add_to_hass(hass)
    assert await async_migrate_entry(hass, legacy)
    assert legacy.version == 3
    assert legacy.data["source"] == "cloud"
    bare = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "cloud"},
        version=3,
    )
    bare.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(bare.entry_id)
    assert bare.state is ConfigEntryState.SETUP_RETRY
    host_entry = MockConfigEntry(
        domain=DOMAIN,
        data={"logger_host": "10.0.0.8", "logger_serial": "5", "logger_mac": ""},
        version=3,
    )
    host_entry.add_to_hass(hass)
    _store_logger_host(hass, host_entry, "10.0.0.8", "")
    _store_logger_host(hass, host_entry, "127.0.0.1", "bc:54:f9:f5:7b:5c")
    assert host_entry.data["logger_host"] == "127.0.0.1"
    host_entry.runtime_data = type("Box", (), {"data": None})()
    assert _known_device_keys(host_entry) == set()


async def test_rediscover_updates_the_host(socket_enabled: None) -> None:
    """A failed connection followed by discovery adopts the logger's address."""
    state = StickState(tcp_port=0, udp_port=0)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    seen: list[str] = []
    transport = SolisLocalTransport(
        "10.0.0.9",
        SERIAL,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=2,
        gap=0,
        on_host=lambda host, _mac: seen.append(host),
    )
    transport._client._port = port

    async def _found() -> str:
        return "127.0.0.1"

    transport._find_stick = _found  # type: ignore[method-assign]
    try:
        snapshot = await transport._after_offline()
    finally:
        await transport.close()
        await emulator.close()
    assert snapshot.source == "local"
    assert seen == ["127.0.0.1"]
    assert transport._client.host == "127.0.0.1"


async def test_offline_rediscover_keeps_the_last_total(socket_enabled: None) -> None:
    """When the stick stays down, the last energy total is what gets published."""
    from custom_components.solis.local_transport import _detect_map, _read_cloud
    from custom_components.solis.solarman import ModbusException

    transport = SolisLocalTransport(
        "127.0.0.1",
        SERIAL,
        slave=1,
        register_map="hybrid",
        inverter_serial="H1",
        timeout=0.2,
        gap=0,
    )

    async def _read(slave: int, address: int, count: int) -> list[int]:
        if address == 3060:
            raise ModbusException(2)
        return [0] * count

    transport._client.read_input = _read  # type: ignore[method-assign]
    hybrid = await transport._read_connected()
    assert hybrid.inverters[0].model == "Hybrid inverter"
    transport._last = hybrid

    async def _down(slave: int, address: int, count: int) -> list[int]:
        raise SolisLoggerOffline("down")

    transport._client.read_input = _down  # type: ignore[method-assign]

    async def _gone() -> str:
        return "127.0.0.1"

    transport._find_stick = _gone  # type: ignore[method-assign]
    asleep = await transport._after_offline()
    assert asleep.source == "asleep"
    assert asleep.inverters[0].energy_total_kwh == hybrid.inverters[0].energy_total_kwh

    class _Odd:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            raise ModbusException(3)

    assert await _detect_map(_Odd(), 1) is None  # type: ignore[arg-type]

    class _StringThenDead:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            if address >= 33000:
                raise ModbusException(2)
            raise ModbusException(1)

    assert await _detect_map(_StringThenDead(), 1) is None  # type: ignore[arg-type]

    class _BadCloud:
        async def async_read(self) -> str:
            return "nope"

    with pytest.raises(SolisCloudError):
        await _read_cloud(_BadCloud())  # type: ignore[arg-type]
    with pytest.raises(SolisLoggerOffline):
        await probe_logger("127.0.0.1", 1, port=1, timeout=0.2)

    from custom_components.solis import local_transport as local_module

    original = local_module.discover_sticks

    async def _oserror(*_args: object, **_kwargs: object) -> list[object]:
        raise OSError("no")

    local_module.discover_sticks = _oserror  # type: ignore[assignment]
    try:
        assert (
            await SolisLocalTransport(
                "127.0.0.1",
                1,
                slave=1,
                register_map="string",
                inverter_serial="X",
                timeout=0.2,
                gap=0,
            )._find_stick()
            is None
        )
    finally:
        local_module.discover_sticks = original


async def test_discovery_socket_error(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery socket that cannot open is an empty result, not a crash."""
    loop = asyncio.get_running_loop()

    async def _boom(*_args: object, **_kwargs: object) -> tuple[object, object]:
        raise OSError("no socket")

    monkeypatch.setattr(loop, "create_datagram_endpoint", _boom)
    assert await discover_sticks(timeout=0.1) == []


def test_cloud_names_the_series_and_the_model_code() -> None:
    """SolisCloud series Solis-mini-2500-4G and model code E4 stay distinct."""
    from custom_components.solis.parser import parse_inverter

    parsed = parse_inverter(
        {
            "sn": "110E40182280017",
            "machine": "Solis-mini-2500-4G",
            "model": "E4",
            "version": "0F000F",
            "productModel": "1",
            "eTotal": 21.97,
            "eTotalStr": "MWh",
            "pac": 0.059,
            "pacStr": "kW",
        }
    )
    assert parsed.model == "Solis-mini-2500-4G"
    assert parsed.model_code == "E4"
    assert parsed.hmi_version == "0F000F"
    assert parsed.energy_total_kwh == pytest.approx(21970)
    assert parsed.ac_power_w == pytest.approx(59)


def test_morning_power_is_not_asleep_and_version_matches_the_cloud() -> None:
    """60 W is production. Registers 3000 and 3001 are version 0F000F."""
    from custom_components.solis.registers import device_model_name

    words = captured_registers()
    words[3005] = 60
    parsed = decode_string(words, serial="110E40182280017", logger_serial=str(SERIAL))
    assert parsed.ac_power_w == 60
    assert parsed.model == "Solis 4G"
    assert parsed.hmi_version == "0F000F"
    assert parsed.data_source == "local"
    assert parsed.state is None
    assert device_model_name("Solis-mini-2500-4G", "E4") == "Solis-mini-2500-4G (E4)"


def test_sequence_match_ignores_the_stick_counter() -> None:
    """The captured reply used b1 17. Only the low byte is our sequence."""
    from custom_components.solis.solarman import build_response, sequence_matches

    frame = build_response(SERIAL, 0x1701, rtu_read(1, 3004, 1))
    assert sequence_matches(frame, 1)
    assert not sequence_matches(frame, 2)


def test_take_frame_drops_junk_and_an_illegal_length() -> None:
    """A heartbeat before a response is one frame. A huge length is not read."""
    from custom_components.solis.solarman import build_response, take_frame

    good = build_response(SERIAL, 1, rtu_read(1, 3004, 1))
    heartbeat = bytes.fromhex("a50100104702d7cbea91e6005d15")
    frame, rest = take_frame(b"\x00\x01" + heartbeat + good)
    assert frame == heartbeat
    assert rest == good
    huge = bytes([0xA5, 0xFF, 0xFF]) + b"\x00" * 8
    assert take_frame(huge)[0] is None


async def test_locked_stick_resets_and_discovery_still_answers(socket_enabled: None) -> None:
    """TCP connects, the request is reset, and UDP still returns the stick."""
    state = StickState(tcp_port=0, udp_port=48899, locked=True)
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=1, gap=0)
    try:
        with pytest.raises(SolisLoggerBusy):
            await client.read_input(1, 3004, 2)
        found = await discover_sticks(timeout=0.4)
        assert any(stick.serial == SERIAL for stick in found)
        before = len(state.connections)
        with pytest.raises(SolisLoggerBusy):
            await client.read_input(1, 3004, 2)
        assert len(state.connections) == before + 1
    finally:
        await client.close()
        await emulator.close()


async def test_plain_modbus_tcp_and_no_cascade_after_a_reset(socket_enabled: None) -> None:
    """MBAP on 8899 is remembered. A reset does not open a second framing."""
    from custom_components.solis.solarman import ModbusTcpClient

    state = StickState(tcp_port=0, udp_port=0, protocol="mbtcp")
    emulator = StickEmulator(state)
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=1, gap=0)
    try:
        assert await client.read_input(1, 3004, 2) == [0, 0]
    finally:
        await client.close()
        await emulator.close()

    locked = StickState(tcp_port=0, udp_port=0, locked=True)
    stuck = StickEmulator(locked)
    await stuck.start()
    assert stuck._tcp is not None
    locked_port = stuck._tcp.sockets[0].getsockname()[1]
    transport = SolisLocalTransport(
        "127.0.0.1",
        SERIAL,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=0.5,
        gap=0,
        port=locked_port,
    )
    try:
        with pytest.raises(SolisLoggerBusy):
            await transport.async_read()
        assert len(locked.connections) == 1
        with pytest.raises(SolisLoggerBusy):
            await transport.async_read()
        assert len(locked.connections) == 1
    finally:
        await transport.close()
        await stuck.close()


async def test_degraded_cloud_keeps_the_local_total_and_serial(socket_enabled: None) -> None:
    """A reset while the cloud is fresh uses the cloud on the same inverter serial."""
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
        inverter_serial="110E40182280017",
        timeout=0.5,
        gap=0,
        port=port,
    )

    class _Fresh:
        async def async_read(self) -> SolisSnapshot:
            from dataclasses import replace

            base = await _tiny_cloud()._cloud.async_read()  # type: ignore[attr-defined]
            inverter = replace(
                base.inverters[0],
                serial="110E40182280017",
                model="Solis-mini-2500-4G",
                model_code="E4",
                ac_power_w=59.0,
                energy_total_kwh=12500,
            )
            return replace(base, inverters=(inverter,))

    router = SolisPriorityTransport(SOURCE_BOTH, local, _Fresh())
    try:
        degraded = await router.async_read()
    finally:
        await router.close()
        await emulator.close()
    assert degraded.source == "degraded"
    assert degraded.inverters[0].serial == "110E40182280017"
    assert degraded.inverters[0].ac_power_w == 59
    assert degraded.inverters[0].model == "Solis-mini-2500-4G"
    assert degraded.inverters[0].model_code == "E4"
    assert degraded.inverters[0].data_source == "degraded"


def test_backoff_starts_at_ten_seconds_and_caps_at_ten_minutes() -> None:
    """The pause after a reset grows and does not exceed about 10 minutes."""
    from custom_components.solis.local_transport import backoff_seconds

    assert 10 <= backoff_seconds(0) <= 11
    assert backoff_seconds(6) == pytest.approx(600, abs=60)


async def test_coordinator_opens_the_logger_repair_only_after_ten_minutes(
    hass: HomeAssistant,
) -> None:
    """One reset is not a repair. Ten minutes of resets is."""
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.solis.coordinator import SolisCoordinator
    from custom_components.solis.errors import SolisLoggerBusy

    class _Busy:
        async def async_read(self) -> SolisSnapshot:
            raise SolisLoggerBusy("local session reset")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "local", "logger_host": "127.0.0.1", "logger_serial": "1"},
        options={},
        version=3,
    )
    entry.add_to_hass(hass)
    coordinator = SolisCoordinator(hass, entry, _Busy())
    assert coordinator.update_interval is not None
    assert coordinator.update_interval.total_seconds() == 60
    with pytest.raises(UpdateFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.translation_key == "update_logger_busy"
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert not any(issue_id.startswith("logger_locked") for issue_id in issues)
    coordinator._degraded_since = 0
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert any(issue_id.startswith("logger_locked") for issue_id in issues)
