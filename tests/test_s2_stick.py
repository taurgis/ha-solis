"""Solis S2-WL-ST Modbus TCP on port 502."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import time
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import entry_options
from custom_components.solis.const import (
    DOMAIN,
    LOGGER_S2,
    REASON_SILENT,
    REASON_TCP_DISABLED,
    SOURCE_BOTH,
    SOURCE_LOCAL,
)
from custom_components.solis.errors import (
    SolisLoggerBusy,
    SolisLoggerOffline,
    logger_failure_text,
)
from custom_components.solis.local_transport import (
    LoggerRequest,
    SolisLocalTransport,
    SolisPriorityTransport,
    identify_logger,
    logger_form_error,
    parse_logger_form,
    s2_needs_identity,
)
from custom_components.solis.models import SolisSnapshot
from custom_components.solis.registers import decode_string
from custom_components.solis.solarman import ModbusTcpClient
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState, captured_registers


def _cloud_snapshot() -> SolisSnapshot:
    inverter = decode_string(
        captured_registers(),
        serial="110E40182280017",
        logger_serial="5A120001",
        now=datetime.now(UTC),
    )
    return SolisSnapshot(
        stations=(),
        inverters=(inverter,),
        alarms=(),
        collectors=(),
        epms=(),
        fetched_at=datetime.now(UTC),
        partial=False,
        source="cloud",
    )


class _Cloud:
    async def async_read(self) -> SolisSnapshot:
        return _cloud_snapshot()


async def _s2_port(state: StickState | None = None) -> tuple[StickEmulator, int]:
    emulator = StickEmulator(state or StickState(kind="s2", tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    return emulator, emulator._tcp.sockets[0].getsockname()[1]


async def test_s2_probe_reads_the_string_map(socket_enabled: None) -> None:
    """Port 502, function 4, slave 1, and the 8-byte serial from 3060."""
    emulator, port = await _s2_port()
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind=LOGGER_S2,
                port=port,
                timeout=2,
                gap=0,
                logger_serial="5A120001",
            )
        )
    finally:
        await emulator.close()
    assert probe.kind == LOGGER_S2
    assert probe.protocol == "mbtcp"
    assert probe.register_map == "string"
    assert probe.slave == 1
    assert probe.function == 4
    assert probe.inverter_serial == "SOLIS001"
    assert probe.port == port


async def test_function_3_is_the_fallback(socket_enabled: None) -> None:
    """The official Modbus Poll demo uses function 3. Exception 1 selects it."""
    emulator, port = await _s2_port(StickState(kind="s2", tcp_port=0, udp_port=0, functions=(3,)))
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2, gap=0, fallback_function=3)
    try:
        probe = await identify_logger(
            LoggerRequest("127.0.0.1", kind=LOGGER_S2, port=port, timeout=2, gap=0)
        )
        words = await client.read_input(1, 3004, 2)
    finally:
        await client.close()
        await emulator.close()
    assert probe.function == 3
    assert words == [0, 0]


async def test_auto_detect_uses_8899_when_502_is_closed(socket_enabled: None) -> None:
    """A DLS-L refuses 502. That is not the S2 firmware fault."""
    emulator = StickEmulator(StickState(tcp_port=8899, udp_port=0))
    await emulator.start()
    try:
        probe = await identify_logger(
            LoggerRequest("127.0.0.1", logger_serial=str(SERIAL), timeout=2, gap=0)
        )
    finally:
        await emulator.close()
    assert probe.kind == "solarman"
    assert probe.protocol == "v5"


async def test_closed_port_and_silence_are_named(socket_enabled: None) -> None:
    """Refused 502 is TCP disabled. A socket that never replies is silent."""
    with pytest.raises(SolisLoggerOffline) as closed:
        await identify_logger(
            LoggerRequest("127.0.0.1", kind=LOGGER_S2, port=1, timeout=0.4, gap=0)
        )
    assert closed.value.reason == REASON_TCP_DISABLED
    assert logger_form_error(closed.value) == "logger_tcp_disabled"
    assert "100101B4" in logger_failure_text("10.0.0.5", closed.value)
    emulator, port = await _s2_port(StickState(kind="s2", tcp_port=0, udp_port=0, silent=True))
    try:
        with pytest.raises(SolisLoggerOffline) as silent:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_S2, port=port, timeout=0.4, gap=0)
            )
    finally:
        await emulator.close()
    assert silent.value.reason == REASON_SILENT
    assert logger_form_error(silent.value) == "logger_modbus_silent"
    assert "one local client" in logger_failure_text("10.0.0.5", silent.value)


async def test_s2_without_a_sample_stays_offline() -> None:
    """A clean close before any reading is offline, and discovery is not used."""
    transport = SolisLocalTransport(
        "127.0.0.1",
        0,
        slave=1,
        register_map="string",
        inverter_serial="",
        kind=LOGGER_S2,
    )
    assert await transport._find_stick() is None
    with pytest.raises(SolisLoggerOffline):
        await transport._s2_offline(SolisLoggerOffline("closed", connected=True, reason="closed"))


async def test_night_keeps_totals(socket_enabled: None) -> None:
    """A dark stick is asleep. Energy totals stay and live power is zero."""
    emulator, port = await _s2_port()
    transport = SolisLocalTransport(
        "127.0.0.1",
        0,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=2,
        gap=0,
        port=port,
        kind=LOGGER_S2,
        stick_serial="5A120001",
    )
    try:
        first = await transport.async_read()
        assert first.source == "local"
        assert first.collectors[0].model == "S2-WL-ST"
        assert first.inverters[0].energy_total_kwh == 21970
        await emulator.apply({"night": True})
        asleep = await transport.async_read()
    finally:
        await transport.close()
        await emulator.close()
    assert asleep.source == "asleep"
    assert asleep.inverters[0].ac_power_w == 0
    assert asleep.inverters[0].energy_total_kwh == 21970


async def test_one_extra_connection_is_reset(socket_enabled: None) -> None:
    """The stick allows one Modbus client. The next session is reset."""
    emulator, port = await _s2_port(
        StickState(kind="s2", tcp_port=0, udp_port=0, max_clients=1, slow=1)
    )
    first = ModbusTcpClient("127.0.0.1", port=port, timeout=3, gap=0)

    async def _hold() -> None:
        try:
            await first.read_input(1, 3004, 1)
        finally:
            await first.close()

    task = asyncio.create_task(_hold())
    await asyncio.sleep(0.05)
    second = ModbusTcpClient("127.0.0.1", port=port, timeout=0.5, gap=0)
    try:
        with pytest.raises(SolisLoggerBusy):
            await second.read_input(1, 3004, 1)
        await task
    finally:
        await second.close()
        await emulator.close()


async def test_closing_the_session_can_shut_port_502(socket_enabled: None) -> None:
    """Firmware 1001318c wedges the port after the client disconnects."""
    emulator, port = await _s2_port(
        StickState(kind="s2", tcp_port=0, udp_port=0, close_disables_port=True)
    )
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=2, gap=0)
    try:
        assert await client.read_input(1, 3004, 1)
        await client.close()
        for _ in range(50):
            if emulator.state.port_closed:
                break
            await asyncio.sleep(0.01)
        again = ModbusTcpClient("127.0.0.1", port=port, timeout=0.4, gap=0)
        with pytest.raises(SolisLoggerOffline) as err:
            await again.read_input(1, 3004, 1)
        await again.close()
    finally:
        await emulator.close()
    assert err.value.refused


async def test_closed_port_uses_cloud_without_zeroing_power(socket_enabled: None) -> None:
    """TCP disabled is degraded, not the night-time zero."""
    local = SolisLocalTransport(
        "127.0.0.1",
        0,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        timeout=0.4,
        gap=0,
        port=1,
        kind=LOGGER_S2,
        stick_serial="5A120001",
    )
    router = SolisPriorityTransport(SOURCE_BOTH, local, _Cloud())
    try:
        sample = await router.async_read()
    finally:
        await router.close()
    assert sample.source == "degraded"
    assert sample.inverters[0].energy_total_kwh == 21970
    assert router.degraded_reason == REASON_TCP_DISABLED
    alone = SolisPriorityTransport(SOURCE_LOCAL, local, None)
    with pytest.raises(SolisLoggerBusy) as busy:
        await alone.async_read()
    assert busy.value.reason == REASON_TCP_DISABLED


async def test_flow_creates_an_s2_entry_and_rejects_bad_input(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """The stick serial is the unique id. A bad port and a missing id are form errors."""
    from homeassistant.data_entry_flow import FlowResultType

    emulator, port = await _s2_port()
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
        bad = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": LOGGER_S2,
                "logger_host": "127.0.0.1",
                "logger_port": "nope",
                "logger_serial": "5A120001",
            },
        )
        assert bad["errors"]["base"] == "invalid_logger"
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": LOGGER_S2,
                "logger_host": "127.0.0.1",
                "logger_port": str(port),
                "logger_serial": "5A120001",
                "slave_id": "1",
            },
        )
    finally:
        await emulator.close()
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].unique_id == "s2:5A120001"
    assert created["result"].data["logger_kind"] == LOGGER_S2
    assert created["result"].data["inverter_serial"] == "SOLIS001"
    assert created["result"].options["scan_interval"] == 60


async def test_flow_asks_for_an_identity_when_the_serial_block_locks(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Registers at 3050 are tried once. A failure still needs a typed serial."""
    emulator, port = await _s2_port(StickState(kind="s2", tcp_port=0, udp_port=0, lock_above=3050))
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
        failed = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"logger_kind": LOGGER_S2, "logger_host": "127.0.0.1", "logger_port": str(port)},
        )
    finally:
        await emulator.close()
    assert failed["errors"]["base"] == "logger_identity"


async def test_s2_entry_publishes_the_stick(hass: HomeAssistant, socket_enabled: None) -> None:
    """A saved S2 entry reads port 502 and keeps the inverter serial as the entity id."""
    emulator, port = await _s2_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_S2,
            "logger_host": "127.0.0.1",
            "logger_port": port,
            "logger_serial": "5A120001",
            "slave_id": 1,
            "register_map": "string",
            "inverter_serial": "110E40182280017",
            "logger_protocol": "mbtcp",
            "logger_function": 4,
        },
        options=entry_options(),
        version=3,
        unique_id="s2:5A120001",
    )
    entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        registry = er.async_get(hass)
        total = registry.async_get_entity_id("sensor", DOMAIN, "110E40182280017_energy_total")
        assert total is not None
        assert float(hass.states.get(total).state) == 21970  # type: ignore[union-attr]
    finally:
        await emulator.close()


async def test_dhcp_updates_a_stored_s2_mac(hass: HomeAssistant, socket_enabled: None) -> None:
    """A MAC saved on the entry is enough. The S2 has no published OUI filter."""
    from homeassistant.config_entries import SOURCE_DHCP
    from homeassistant.data_entry_flow import FlowResultType

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_S2,
            "logger_host": "10.0.0.8",
            "logger_serial": "5A120001",
            "logger_mac": "aa:bb:cc:dd:ee:01",
            "inverter_serial": "110E40182280017",
        },
        version=3,
        unique_id="s2:5A120001",
    )
    entry.add_to_hass(hass)
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "aa:bb:cc:dd:ee:01", "hostname": "datalogger"},
    )
    assert discovered["type"] is FlowResultType.ABORT
    assert discovered["reason"] == "already_configured"
    assert entry.data["logger_host"] == "127.0.0.1"


async def test_tcp_disabled_repair_waits_ten_minutes(hass: HomeAssistant) -> None:
    """One closed port is not a repair. Ten minutes of it is."""
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.solis.coordinator import SolisCoordinator

    class _Closed:
        degraded_reason = REASON_TCP_DISABLED

        async def async_read(self) -> SolisSnapshot:
            raise SolisLoggerBusy("port 502 is closed", reason=REASON_TCP_DISABLED)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": SOURCE_LOCAL, "logger_kind": LOGGER_S2, "logger_host": "127.0.0.1"},
        options={},
        version=3,
    )
    entry.add_to_hass(hass)
    coordinator = SolisCoordinator(hass, entry, _Closed())
    with pytest.raises(UpdateFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.translation_key == "update_logger_tcp_disabled"
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert not any(issue_id.startswith("logger_tcp_disabled") for issue_id in issues)
    coordinator._degraded_since = time.monotonic() - 601
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    issues = {issue_id for (_domain, issue_id) in ir.async_get(hass).issues}
    assert any(issue_id.startswith("logger_tcp_disabled") for issue_id in issues)
    assert coordinator.logger_report()["reason"] == REASON_TCP_DISABLED


def test_parse_logger_form_and_identity() -> None:
    """Empty host, a wild port, and a placeholder serial are rejected."""
    missing, error = parse_logger_form({"logger_host": "  "})
    assert missing is None and error == "logger_asleep"
    renamed, still = parse_logger_form(
        {"logger_host": "10.0.0.2", "logger_kind": "nope", "logger_serial": "1"}
    )
    assert still == "" and renamed is not None and renamed.kind == "auto"
    _solar, bad_serial = parse_logger_form(
        {"logger_host": "10.0.0.2", "logger_kind": "solarman", "logger_serial": "abc"}
    )
    assert bad_serial == "invalid_logger"
    assert logger_form_error(SolisLoggerBusy("reset")) == "logger_busy"
    _request, bad_slave = parse_logger_form(
        {"logger_host": "10.0.0.2", "logger_kind": LOGGER_S2, "slave_id": "0"}
    )
    assert bad_slave == "invalid_logger"
    request, ok = parse_logger_form(
        {
            "logger_host": "10.0.0.2",
            "logger_kind": "solarman",
            "logger_serial": "1902137671",
            "logger_port": "8899",
        }
    )
    assert ok == "" and request is not None and request.port == 8899
    from custom_components.solis.local_transport import LoggerProbe

    placeholder = LoggerProbe(1, "string", "L0", "mbtcp", True, kind=LOGGER_S2, port=502)
    assert s2_needs_identity(placeholder, "", "")
    assert logger_failure_text("10.0.0.2", SolisLoggerOffline("down")).startswith(
        "10.0.0.2 did not"
    )


async def test_fault_helpers_and_a_locked_s2(socket_enabled: None) -> None:
    """Busy, silent and a wrong slave stay distinct from a night-time timeout."""
    from custom_components.solis.local_transport import (
        _detect_map,
        _prefer_auto_error,
        _probe_named_solarman,
        _s2_busy,
    )
    from custom_components.solis.solarman import ModbusException

    assert _s2_busy("stick", None) is None
    paused = _s2_busy("stick", SolisLoggerOffline("slow", connected=True, reason="timeout"))
    assert paused is not None and paused.reason == REASON_SILENT
    assert _s2_busy("stick", SolisLoggerOffline("fin", connected=True, reason="closed")) is None
    kept = SolisLoggerOffline("other")
    assert _prefer_auto_error(None, kept) is kept
    assert (
        _prefer_auto_error(SolisLoggerOffline("already", reason=REASON_SILENT), kept).reason
        == REASON_SILENT
    )
    assert (
        _prefer_auto_error(
            SolisLoggerOffline("held", connected=True, reason="garbage"), kept
        ).reason
        == REASON_SILENT
    )
    with pytest.raises(SolisLoggerOffline):
        await _probe_named_solarman(
            LoggerRequest("127.0.0.1", kind="solarman", logger_serial="abc"),
            serial_unsupported=False,
        )

    class _HybridOffline:
        async def read_input(self, slave: int, address: int, count: int) -> list[int]:
            if address < 33000:
                raise ModbusException(2)
            raise SolisLoggerOffline("hybrid down", reason="timeout")

    with pytest.raises(SolisLoggerOffline):
        await _detect_map(_HybridOffline(), 1)  # type: ignore[arg-type]

    quiet, quiet_port = await _s2_port()
    try:
        with pytest.raises(SolisLoggerOffline):
            await identify_logger(
                LoggerRequest(
                    "127.0.0.1",
                    kind=LOGGER_S2,
                    port=quiet_port,
                    slave=9,
                    timeout=1,
                    gap=0,
                    logger_serial="5A1",
                )
            )
    finally:
        await quiet.close()
    emulator, port = await _s2_port(StickState(kind="s2", tcp_port=0, udp_port=0, locked=True))
    try:
        with pytest.raises(SolisLoggerBusy):
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_S2, port=port, timeout=1, gap=0)
            )
    finally:
        await emulator.close()


async def test_explicit_solarman_kind_skips_port_502(socket_enabled: None) -> None:
    """Naming the stick avoids the 502 probe."""
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind="solarman",
                port=port,
                logger_serial=str(SERIAL),
                timeout=2,
                gap=0,
                slave=1,
            )
        )
    finally:
        await emulator.close()
    assert probe.kind == "solarman"
    assert probe.slave == 1


async def test_reconfigure_can_attach_an_s2(
    hass: HomeAssistant, solis_server: tuple[Any, str], socket_enabled: None
) -> None:
    """A cloud entry can gain an S2-WL-ST without a second device."""
    from homeassistant.data_entry_flow import FlowResultType

    from .conftest import entry_data

    _state, url = solis_server
    emulator, port = await _s2_port()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        version=3,
        unique_id="test-key",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    started = await entry.start_reconfigure_flow(hass)
    try:
        updated = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            {
                "api_url": url,
                "station_ids": ["1900000000000001001", "1900000000000001002"],
                "scan_interval": 120,
                "request_timeout": 30,
                "endpoint_interval": 0.5,
                "logger_kind": LOGGER_S2,
                "logger_host": "127.0.0.1",
                "logger_port": str(port),
                "logger_serial": "5A120001",
                "inverter_serial": "110E40182280017",
            },
        )
    finally:
        await emulator.close()
    assert updated["type"] is FlowResultType.ABORT
    assert updated["reason"] == "reconfigure_successful"
    assert entry.unique_id == "s2:5A120001"
    assert entry.data["source"] == "both"
