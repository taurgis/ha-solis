"""Modbus TCP gateways, raw RTU, and the experimental S5 kind."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import socket
import struct

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import entry_options
from custom_components.solis.const import (
    DOMAIN,
    LOGGER_MBTCP,
    LOGGER_RTU,
    LOGGER_S5,
    REASON_CLOSED,
    REASON_GARBAGE,
    REASON_GATEWAY,
    REASON_REFUSED,
    REASON_SILENT,
    REASON_TIMEOUT,
    REASON_UNSUPPORTED,
    REASON_WRONG_PROTOCOL,
    REASON_WRONG_SLAVE,
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
    _asleep_log,
    _bridge_busy,
    _fc3_fallback,
    _reset_log,
    identify_logger,
    logger_form_error,
    needs_stable_id,
    parse_logger_form,
)
from custom_components.solis.loggers import detection_code, detection_why, kind_label, spec_for
from custom_components.solis.loggers.probe import _annotate_bridge
from custom_components.solis.loggers.rtu import RtuTcpClient, decode_rtu, rtu_request, take_rtu
from custom_components.solis.solarman import ModbusException, crc16
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState, captured_registers


def _rtu_reply(slave: int, function: int, values: list[int]) -> bytes:
    data = b"".join((value & 0xFFFF).to_bytes(2, "big") for value in values)
    body = bytes((slave, function, len(data))) + data
    return body + crc16(body).to_bytes(2, "little")


def _rtu_exception(slave: int, function: int, code: int) -> bytes:
    body = bytes((slave, function | 0x80, code))
    return body + crc16(body).to_bytes(2, "little")


class RtuBox:
    """One TCP socket that speaks raw Modbus RTU, with a chosen fault."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.registers = captured_registers()
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.mode == "silent":
                await asyncio.sleep(30)
                return
            if self.mode == "close":
                return
            if self.mode == "reset":
                await _linger_reset(reader, writer)
                return
            if self.mode == "partial":
                writer.write(b"\x01\x04")
                await writer.drain()
                await asyncio.sleep(30)
                return
            while True:
                request = await asyncio.wait_for(reader.readexactly(8), timeout=2)
                reply = self._reply(request)
                if reply is None:
                    await _linger_reset(reader, writer)
                    return
                if self.mode == "dribble":
                    writer.write(reply[:3])
                    await writer.drain()
                    await asyncio.sleep(0.05)
                    writer.write(reply[3:])
                elif self.mode == "junk":
                    writer.write(b"\x00\x04\x01" + reply)
                else:
                    writer.write(reply)
                await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            return
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _reply(self, request: bytes) -> bytes | None:
        slave, function = request[0], request[1]
        address = int.from_bytes(request[2:4], "big")
        count = int.from_bytes(request[4:6], "big")
        if self.mode == "reset_serial" and address >= 3050:
            return None
        if self.mode == "unsupported" or self.mode == "exc1":
            code = 1 if self.mode == "exc1" else 2
            return _rtu_exception(slave, function, code)
        if self.mode == "fc3" and function == 4:
            return _rtu_exception(slave, function, 1)
        if self.mode == "illegal" and function == 4:
            return _rtu_exception(slave, function, 3)
        if address >= 33000:
            return _rtu_exception(slave, function, 2)
        values = [self.registers.get(address + offset, 0) for offset in range(count)]
        if self.mode == "wrong_slave":
            return _rtu_reply(9, function, values)
        frame = _rtu_reply(slave, function, values)
        if self.mode == "bad_crc":
            return frame[:-1] + bytes((frame[-1] ^ 0xFF,))
        return frame


async def _linger_reset(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with suppress(TimeoutError, ConnectionError, OSError):
        await asyncio.wait_for(reader.read(8), timeout=0.2)
    sock = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.close()


class MbapBox:
    """MBAP server that always returns one Modbus exception."""

    def __init__(self, code: int = 2, mode: str = "exception") -> None:
        self.code = code
        self.mode = mode
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self.mode == "partial":
                writer.write(b"\x00\x01\x00")
                await writer.drain()
                await asyncio.sleep(30)
                return
            if self.mode == "reset":
                await _linger_reset(reader, writer)
                return
            if self.mode == "garbage":
                writer.write(b"not-modbus-at-all!!")
                await writer.drain()
                return
            while True:
                header = await asyncio.wait_for(reader.readexactly(6), timeout=2)
                length = int.from_bytes(header[4:6], "big")
                await asyncio.wait_for(reader.readexactly(length), timeout=2)
                function = 4
                pdu = bytes((function | 0x80, self.code))
                payload = (
                    header[:2] + b"\x00\x00" + (len(pdu) + 1).to_bytes(2, "big") + b"\x01" + pdu
                )
                writer.write(payload)
                await writer.drain()
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            return
        finally:
            writer.close()


def test_registry_and_detection_codes() -> None:
    """Unknown kinds stay on Solarman. Each manual kind has its own sentence."""
    assert spec_for("nope").kind == "solarman"
    assert kind_label("") == "Solarman"
    assert kind_label("auto") == "Solarman"
    assert "gateway" in kind_label("modbus_tcp").lower()
    assert detection_code("auto", "s2_wl_st") == "auto_s2"
    assert detection_code("auto", "s5_wifi_st") == "auto_s2"
    assert detection_code("auto", "solarman") == "auto_solarman"
    assert detection_code("rtu_tcp", "rtu_tcp") == "manual_rtu_tcp"
    assert "legacy" not in detection_why("manual_modbus_tcp").lower()
    assert "before dongle detection" in detection_why("missing")
    assert spec_for("s5_wifi_st").experimental is True
    assert spec_for("modbus_tcp").bridge is True
    assert spec_for("s2_wl_st").unique_prefix == "s2"


def test_helper_sentences_cover_each_kind() -> None:
    """Log lines and gateway pauses stay specific to the kind."""
    assert _fc3_fallback("s2_wl_st", 4) == 3
    assert _fc3_fallback("s2_wl_st", 3) == 0
    assert "Solarman logger 9" in _reset_log("solarman", "Solarman", "9", 12)
    assert _reset_log("s2_wl_st", "S2-WL-ST", "5A", 12).startswith("S2-WL-ST")
    assert "Modbus TCP gateway" in _reset_log("modbus_tcp", "Modbus TCP gateway", "gw", 12)
    assert "Solarman logger" in _asleep_log("solarman", "Solarman", "9")
    assert _asleep_log("s2_wl_st", "S2-WL-ST", "5A").startswith("S2-WL-ST")
    assert "RTU" in _asleep_log("rtu_tcp", "Modbus RTU over TCP", "bridge")
    assert _bridge_busy("10.0.0.1", None) is None
    garbage = SolisLoggerOffline("bytes", connected=True, reason=REASON_GARBAGE)
    assert _bridge_busy("10.0.0.1", garbage) is not None
    refused = SolisLoggerOffline("no", connected=False, refused=True, reason=REASON_REFUSED)
    paused = _bridge_busy("10.0.0.1", refused)
    assert paused is not None and paused.reason == REASON_GATEWAY
    quiet = SolisLoggerOffline("wait", connected=True, reason=REASON_TIMEOUT)
    assert _bridge_busy("10.0.0.1", quiet) is None
    with pytest.raises(ValueError):
        decode_rtu(b"\x01\x04", 4)
    flipped = bytearray(_rtu_reply(1, 4, [1]))
    flipped[-1] ^= 0xFF
    with pytest.raises(ValueError):
        decode_rtu(bytes(flipped), 4)
    with pytest.raises(ValueError):
        decode_rtu(b"\x01\x04\x04\x00\x00\x00", 4)
    short = b"\x01\x04\x02"
    assert take_rtu(short)[0] is None
    assert take_rtu(short)[1] is short
    frame = _rtu_reply(1, 4, [15, 15])
    popped, rest = take_rtu(frame + b"\x01")
    assert popped == frame
    assert rest == b"\x01"
    assert decode_rtu(frame, 4) == [15, 15]
    waiting = frame[:5]
    assert take_rtu(waiting)[1] is waiting
    assert take_rtu(b"\x01\x04\x01\x00\x00")[1] == b"\x04\x01\x00\x00"
    exception = _rtu_exception(1, 4, 2)
    with pytest.raises(ModbusException) as err:
        decode_rtu(exception, 4)
    assert err.value.code == 2
    broken = exception[:-1] + b"\x00"
    with pytest.raises(ValueError):
        decode_rtu(broken, 4)
    with pytest.raises(ValueError):
        decode_rtu(frame, 3)
    assert rtu_request(1, 4, 3004, 2)[1] == 4


def test_bridge_annotation_and_form_errors() -> None:
    """Gateway faults get their own sentences. A Solarman refusal stays asleep."""
    kept = SolisLoggerOffline("same", connected=True, reason=REASON_WRONG_SLAVE)
    assert _annotate_bridge(kept) is kept
    closed = _annotate_bridge(SolisLoggerOffline("no", refused=True, reason=REASON_REFUSED))
    assert closed.reason == REASON_GATEWAY
    offline = _annotate_bridge(SolisLoggerOffline("no", connected=False, reason=REASON_TIMEOUT))
    assert offline.reason == REASON_GATEWAY
    garbage = _annotate_bridge(SolisLoggerOffline("no", connected=True, reason=REASON_GARBAGE))
    assert garbage.reason == REASON_WRONG_PROTOCOL
    quiet = _annotate_bridge(SolisLoggerOffline("no", connected=True, reason=REASON_SILENT))
    assert quiet.reason == REASON_TIMEOUT
    hung = _annotate_bridge(SolisLoggerOffline("no", connected=True, reason=REASON_CLOSED))
    assert hung.reason == REASON_TIMEOUT
    other = SolisLoggerOffline("no", connected=True, reason="other")
    assert _annotate_bridge(other) is other
    assert logger_form_error(closed) == "logger_gateway"
    assert logger_form_error(garbage) == "logger_wrong_protocol"
    assert logger_form_error(kept) == "logger_wrong_slave"
    unsupported = SolisLoggerOffline("map", connected=True, reason=REASON_UNSUPPORTED)
    assert logger_form_error(unsupported) == "logger_unsupported_map"
    assert logger_form_error(quiet) == "logger_timeout"
    assert "gateway" in logger_failure_text("10.0.0.8", closed)
    assert "transparent" in logger_failure_text("10.0.0.8", garbage)
    assert "slave" in logger_failure_text("10.0.0.8", kept)
    assert "3000" in logger_failure_text("10.0.0.8", unsupported)
    request, error = parse_logger_form(
        {"logger_host": "10.0.0.9", "logger_kind": LOGGER_RTU, "logger_serial": "bridge"}
    )
    assert error == "" and request is not None and request.kind == LOGGER_RTU


async def test_rtu_happy_path_and_dribble(socket_enabled: None) -> None:
    """A full read survives a frame that arrives in two pieces."""
    box = RtuBox("dribble")
    await box.start()
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind=LOGGER_RTU,
                port=box.port,
                logger_serial="bridge",
                timeout=2,
                gap=0,
            )
        )
        assert probe.kind == LOGGER_RTU
        assert probe.protocol == "rtu"
        assert probe.inverter_serial == "SOLIS001"
        transport = SolisLocalTransport(
            "127.0.0.1",
            0,
            slave=probe.slave,
            register_map=probe.register_map,
            inverter_serial=probe.inverter_serial,
            protocol=probe.protocol,
            port=box.port,
            kind=LOGGER_RTU,
            stick_serial="bridge",
            gap=0,
            timeout=2,
        )
        try:
            snapshot = await transport.async_read()
        finally:
            await transport.close()
    finally:
        await box.close()
    assert snapshot.source == "local"
    assert snapshot.inverters[0].energy_total_kwh == 21970
    assert snapshot.collectors[0].model == "Modbus RTU over TCP"


async def test_rtu_failure_modes(socket_enabled: None) -> None:
    """Reset, silence, the wrong slave, a bad CRC, and a map that is not Solis."""
    reset = RtuBox("reset")
    await reset.start()
    try:
        with pytest.raises(SolisLoggerBusy):
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=reset.port, timeout=1, gap=0)
            )
    finally:
        await reset.close()
    silent = RtuBox("silent")
    await silent.start()
    try:
        with pytest.raises(SolisLoggerOffline) as timeout:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=silent.port, timeout=0.3, gap=0)
            )
    finally:
        await silent.close()
    assert timeout.value.reason == REASON_TIMEOUT
    wrong = RtuBox("wrong_slave")
    await wrong.start()
    try:
        with pytest.raises(SolisLoggerOffline) as slave:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=wrong.port, timeout=1, gap=0)
            )
    finally:
        await wrong.close()
    assert slave.value.reason == REASON_WRONG_SLAVE
    crc = RtuBox("bad_crc")
    await crc.start()
    try:
        with pytest.raises(SolisLoggerOffline) as framed:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=crc.port, timeout=1, gap=0)
            )
    finally:
        await crc.close()
    assert framed.value.reason == REASON_WRONG_PROTOCOL
    partial = RtuBox("partial")
    await partial.start()
    try:
        with pytest.raises(SolisLoggerOffline) as hung:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=partial.port, timeout=0.3, gap=0)
            )
    finally:
        await partial.close()
    assert hung.value.reason == REASON_TIMEOUT
    alien = RtuBox("unsupported")
    await alien.start()
    try:
        with pytest.raises(SolisLoggerOffline) as mapped:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=alien.port, timeout=1, gap=0)
            )
    finally:
        await alien.close()
    assert mapped.value.reason == REASON_UNSUPPORTED
    illegal = RtuBox("illegal")
    await illegal.start()
    try:
        with pytest.raises(SolisLoggerOffline) as coded:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=illegal.port, timeout=1, gap=0)
            )
    finally:
        await illegal.close()
    assert coded.value.reason == REASON_UNSUPPORTED


async def test_rtu_resync_close_and_plain_exception(socket_enabled: None) -> None:
    """A dropped byte, a bare close and a non-retryable exception are named."""

    class _Writer:
        transport = None

        def write(self, _payload: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class _Reader:
        async def read(self, _size: int) -> bytes:
            return b""

    client = RtuTcpClient("127.0.0.1", port=1, timeout=1, gap=0, fallback_function=3)
    client._reader = _Reader()  # type: ignore[assignment]
    client._writer = _Writer()  # type: ignore[assignment]
    client._buffer = b"\x01\x04\x01\x00\x00"
    with pytest.raises(SolisLoggerOffline) as dropped:
        await client.read_input(1, 3004, 2)
    assert dropped.value.reason == REASON_CLOSED
    await client.close()
    closed = RtuBox("close")
    await closed.start()
    try:
        with pytest.raises(SolisLoggerBusy):
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=closed.port, timeout=1, gap=0)
            )
    finally:
        await closed.close()
    exc = RtuBox("exc1")
    await exc.start()
    client = RtuTcpClient("127.0.0.1", port=exc.port, timeout=1, gap=0, fallback_function=0)
    try:
        with pytest.raises(ModbusException) as coded:
            await client.read_input(1, 3004, 2)

        async def _boom(_reader: asyncio.StreamReader) -> bytes:
            raise ValueError("not a frame")

        client._read_frame = _boom  # type: ignore[method-assign]
        with pytest.raises(SolisLoggerOffline) as garbage:
            await client.read_input(1, 3004, 2)
    finally:
        await client.close()
        await exc.close()
    assert coded.value.code == 1
    assert garbage.value.reason == REASON_GARBAGE
    box = RtuBox("fc3")
    await box.start()
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind=LOGGER_RTU,
                port=box.port,
                logger_serial="bridge",
                timeout=2,
                gap=0,
            )
        )
    finally:
        await box.close()
    assert probe.function == 3
    locked = RtuBox("reset_serial")
    await locked.start()
    try:
        probe = await identify_logger(
            LoggerRequest("127.0.0.1", kind=LOGGER_RTU, port=locked.port, timeout=1, gap=0)
        )
    finally:
        await locked.close()
    assert needs_stable_id(probe, "", "") is True


async def test_gateway_flow_diagnostics_and_night(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A Waveshare-style gateway is not an S2, and a dead gateway does not zero power."""
    emulator = StickEmulator(StickState(kind="s2", tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
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
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": LOGGER_MBTCP,
                "logger_host": "127.0.0.1",
                "logger_port": str(port),
                "logger_serial": "gw-1",
                "slave_id": "1",
            },
        )
        assert created["type"] is FlowResultType.CREATE_ENTRY
        assert created["result"].unique_id == "tcp:gw-1"
        assert created["result"].data["logger_detect"] == "manual_modbus_tcp"
        entry = created["result"]
        await hass.async_block_till_done()
        report = entry.runtime_data.logger_report()
        assert report["kind"] == LOGGER_MBTCP
        assert report["experimental"] == "false"
        assert "Modbus TCP" in report["why"]
        await entry.runtime_data.transport.close()
        await emulator.close()
        emulator = None
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        held = entry.runtime_data.data
        assert held.source == "degraded"
        assert held.inverters[0].energy_total_kwh == 21970
    finally:
        if emulator is not None:
            await emulator.close()


async def test_gateway_faults_and_asleep_inverter(socket_enabled: None) -> None:
    """A refused port is the gateway. A socket that stays quiet is the inverter."""
    with pytest.raises(SolisLoggerOffline) as down:
        await identify_logger(
            LoggerRequest("127.0.0.1", kind=LOGGER_MBTCP, port=1, timeout=0.4, gap=0)
        )
    assert down.value.reason == REASON_GATEWAY
    alien = MbapBox(code=2)
    await alien.start()
    try:
        with pytest.raises(SolisLoggerOffline) as mapped:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_MBTCP, port=alien.port, timeout=1, gap=0)
            )
    finally:
        await alien.close()
    assert mapped.value.reason == REASON_UNSUPPORTED
    garbage = MbapBox(mode="garbage")
    await garbage.start()
    try:
        with pytest.raises(SolisLoggerOffline) as framed:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_MBTCP, port=garbage.port, timeout=1, gap=0)
            )
    finally:
        await garbage.close()
    assert framed.value.reason == REASON_WRONG_PROTOCOL
    reset = MbapBox(mode="reset")
    await reset.start()
    try:
        with pytest.raises(SolisLoggerBusy):
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_MBTCP, port=reset.port, timeout=1, gap=0)
            )
    finally:
        await reset.close()
    partial = MbapBox(mode="partial")
    await partial.start()
    try:
        with pytest.raises(SolisLoggerOffline) as hung:
            await identify_logger(
                LoggerRequest("127.0.0.1", kind=LOGGER_MBTCP, port=partial.port, timeout=0.3, gap=0)
            )
    finally:
        await partial.close()
    assert hung.value.reason == REASON_TIMEOUT
    live = StickEmulator(StickState(kind="s2", tcp_port=0, udp_port=0))
    await live.start()
    assert live._tcp is not None
    port = live._tcp.sockets[0].getsockname()[1]
    transport = SolisLocalTransport(
        "127.0.0.1",
        0,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        protocol="mbtcp",
        port=port,
        kind=LOGGER_MBTCP,
        stick_serial="gw-1",
        gap=0,
        timeout=1,
    )
    router = SolisPriorityTransport(SOURCE_LOCAL, transport, None)
    try:
        first = await router.async_read()
        assert first.source == "local"
        await transport.close()
        live.state.silent = True
        asleep = await router.async_read()
    finally:
        await router.close()
        await live.close()
    assert asleep.source == "asleep"
    assert asleep.inverters[0].energy_total_kwh == 21970


async def test_s5_is_experimental_and_shares_the_s2_id(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """S5 uses the S2 frames and the s2: unique id, and the form says experimental."""
    emulator = StickEmulator(StickState(kind="s2", tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind=LOGGER_S5,
                port=port,
                logger_serial="5A120099",
                timeout=2,
                gap=0,
            )
        )
        assert probe.kind == LOGGER_S5
        assert probe.protocol == "mbtcp"
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
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": LOGGER_S5,
                "logger_host": "127.0.0.1",
                "logger_port": str(port),
                "logger_serial": "5A120099",
            },
        )
    finally:
        await emulator.close()
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].unique_id == "s2:5A120099"
    assert created["result"].data["logger_kind"] == LOGGER_S5
    assert created["result"].data["logger_detect"] == "manual_s5_wifi_st"


async def test_legacy_solarman_entry_has_no_kind(hass: HomeAssistant, socket_enabled: None) -> None:
    """An entry saved before logger_kind still reads V5 and keeps its entities."""
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "127.0.0.1",
            "logger_port": port,
            "logger_serial": str(SERIAL),
            "slave_id": 1,
            "register_map": "string",
            "inverter_serial": "SOLIS001",
        },
        options=entry_options(),
        version=3,
        unique_id=str(SERIAL),
    )
    entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        report = entry.runtime_data.logger_report()
        assert report["kind"] == "solarman"
        assert "before dongle detection" in report["why"]
        registry = er.async_get(hass)
        total = registry.async_get_entity_id("sensor", DOMAIN, "SOLIS001_energy_total")
        assert total is not None
        state = hass.states.get(total)
        assert state is not None
        assert float(state.state) == 21970
    finally:
        await emulator.close()


async def test_rtu_flow_rejects_a_closed_port(hass: HomeAssistant, socket_enabled: None) -> None:
    """The experimental RTU option explains a refused port instead of calling it night."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"source": SOURCE_LOCAL, "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    failed = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"logger_kind": LOGGER_RTU, "logger_host": "127.0.0.1", "logger_port": "1"},
    )
    assert failed["errors"]["base"] == "logger_gateway"
