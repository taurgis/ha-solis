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
from custom_components.solis.errors import SolisCloudError, SolisLoggerOffline
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
    with pytest.raises(ModbusException):
        decode_read(bytes.fromhex("018302") + b"\x00\x00")
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
