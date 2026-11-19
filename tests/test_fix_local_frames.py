"""Register decoding and local framing fixes, checked against realistic peers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import struct
import time

import pytest

from custom_components.solis.const import (
    REASON_TIMEOUT,
    REASON_WRONG_PROTOCOL,
    REASON_WRONG_SLAVE,
)
from custom_components.solis.errors import SolisLoggerOffline
from custom_components.solis.local_transport import (
    SolisLocalTransport,
    _detect_map,
    _scan_slaves,
    _serial_once,
    probe_logger,
)
from custom_components.solis.loggers.rtu import RtuTcpClient, find_rtu, rtu_request
from custom_components.solis.registers import (
    _serial_hex,
    decode_hybrid,
    decode_string,
    hybrid_serial_from_words,
    inverter_state,
    map_from_type,
    s16,
    serial_from_words,
)
from custom_components.solis.solarman import (
    ModbusException,
    ModbusTcpClient,
    SolarmanClient,
    build_request,
    build_response,
    build_time_response,
    crc16,
    logger_serial_of,
    no_rs485_answer,
    rtu_read,
    sequence_of,
    take_frame,
)
from mock_soliscloud.stick import (
    HYBRID_SERIAL,
    INVERTER_SERIAL,
    SERIAL,
    StickEmulator,
    StickState,
    captured_registers,
    encode_serial,
    hybrid_registers,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def _serve(handler: Handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _stop(server: asyncio.Server) -> None:
    server.close()
    await server.wait_closed()


async def _emulator(**kwargs: object) -> tuple[StickEmulator, int]:
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=0, **kwargs))  # type: ignore[arg-type]
    await emulator.start()
    assert emulator._tcp is not None
    return emulator, emulator._tcp.sockets[0].getsockname()[1]


def _data(slave: int, values: list[int]) -> bytes:
    body = bytes((slave, 4, len(values) * 2)) + b"".join(v.to_bytes(2, "big") for v in values)
    return body + crc16(body).to_bytes(2, "little")


def _exception(slave: int, code: int) -> bytes:
    body = bytes((slave, 0x84, code))
    return body + crc16(body).to_bytes(2, "little")


def _heartbeat(serial: int) -> bytes:
    payload = b"\x00"
    body = struct.pack("<BHBBHI", 0xA5, len(payload), 0x10, 0x47, 0x0300, serial) + payload
    return body + bytes((sum(body[1:]) & 0xFF, 0x15))


async def _frames(reader: asyncio.StreamReader, wanted: int = 1) -> list[bytes]:
    frames: list[bytes] = []
    buffer = b""
    while True:
        frame, buffer = take_frame(buffer)
        if frame is not None:
            frames.append(frame)
            if len(frames) == wanted:
                return frames
            continue
        chunk = await reader.read(512)
        if not chunk:
            raise ConnectionError
        buffer += chunk


async def _frame(reader: asyncio.StreamReader) -> bytes:
    return (await _frames(reader))[0]


# Findings 5, 7, 8 and 10: string map decoding.


def test_status_word_maps_to_cloud_codes() -> None:
    assert inverter_state(None) is None
    for normal in (0, 1, 2, 3, 15):
        assert inverter_state(normal) == 1
    assert inverter_state(0x1015) == 3
    assert inverter_state(0x2010) == 3
    assert inverter_state(7) is None
    words = captured_registers()
    assert decode_string(words, serial="S", logger_serial="1").state == 1
    words[3043] = 0x1015
    assert decode_string(words, serial="S", logger_serial="1").state == 3


def test_signed_temperature_and_serial_encodings() -> None:
    words = captured_registers()
    words[3041] = 0xFFEC  # -2.0 C on a frosty morning
    assert decode_string(words, serial="S", logger_serial="1").temperature_c == -2.0
    assert s16({1: 0x8000}, 1) == -32768
    # Hex-nibble serial, as Solis firmware stores it.
    encoded = encode_serial(INVERTER_SERIAL)
    assert encoded[0] == 0x1301
    assert _serial_hex(encoded) == INVERTER_SERIAL
    assert serial_from_words(dict(zip(range(3060, 3064), encoded, strict=True))) == (
        INVERTER_SERIAL
    )
    assert serial_from_words({}) == ""
    assert serial_from_words(dict.fromkeys(range(3060, 3064), 0xFFFF)) == ""
    # A bank of printable letters and digits is ASCII.
    ascii_bank = {3060: 0x5331, 3061: 0x3233, 3062: 0x3435, 3063: 0x3600}
    assert serial_from_words(ascii_bank) == "S123456"
    # The hybrid serial is ASCII at 33004, two characters per register.
    raw = HYBRID_SERIAL.encode()
    pairs = [int.from_bytes(raw[i : i + 2], "big") for i in range(0, 16, 2)]
    assert hybrid_serial_from_words([*pairs, 0, 0, 0, 0, 0, 0, 0, 0]) == HYBRID_SERIAL
    assert hybrid_serial_from_words([ord(c) for c in "6031"]) == "6031"
    assert hybrid_serial_from_words([6, 0, 3, 1]) == "631"
    assert hybrid_serial_from_words([0] * 16) == ""


def test_phantom_pv2_is_dropped() -> None:
    """A one-input mini reads 1 at 3003: an idle PV2 is not a string."""
    words = captured_registers()
    words.update({3003: 1, 3021: 2410, 3022: 21, 3023: 0, 3024: 0})
    assert [pv.index for pv in decode_string(words, serial="S", logger_serial="1").strings] == [1]
    words.update({3023: 2300, 3024: 18})
    live = decode_string(words, serial="S", logger_serial="1").strings
    assert [pv.index for pv in live] == [1, 2]
    assert live[1].power_w == round(230.0 * 1.8, 2)
    words.update({3003: 2, 3023: 0, 3024: 0})
    idle = decode_string(words, serial="S", logger_serial="1").strings
    assert [pv.index for pv in idle] == [1, 2]
    assert idle[1].power_w is None


# Findings 6 and 9: the hybrid map.


def test_hybrid_decode_uses_signed_and_ac_registers() -> None:
    inverter = decode_hybrid(hybrid_registers(), serial=HYBRID_SERIAL, logger_serial="1")
    assert inverter.ac_power_w == 2510
    assert inverter.temperature_c == -3.0
    assert inverter.battery_current_a == -10.0
    assert inverter.battery_power_w == -512
    assert inverter.state == 1
    assert inverter.frequency_hz == 50.01
    assert inverter.energy_today_kwh == 8.7
    assert inverter.energy_total_kwh == 8123
    assert inverter.battery_charge_today_kwh == 4.2
    assert inverter.consumption_power_w == 1820
    assert inverter.grid_power_w is None
    assert [pv.index for pv in inverter.strings] == [1, 2]
    assert len(inverter.phases) == 1
    assert inverter.ac_output_type == 0
    words = hybrid_registers()
    words.update({33074: 2401, 33075: 2399, 33135: 0, 33134: 55})
    three = decode_hybrid(words, serial="S", logger_serial="1")
    assert len(three.phases) == 3
    assert three.ac_output_type == 1
    assert three.battery_power_w == 512
    assert three.battery_current_a == 5.5
    bare = decode_hybrid({33095: 3}, serial="S", logger_serial="1")
    assert bare.ac_power_w is None
    assert bare.phases == ()
    assert bare.frequency_hz is None


def test_type_register_values() -> None:
    assert map_from_type(1005) == "string"
    assert map_from_type(0x1005) == "string"
    assert map_from_type(2005) == "hybrid"
    assert map_from_type(0x2010) == "hybrid"
    assert map_from_type(0) is None
    assert map_from_type(3005) is None


class _Map:
    """A register reader whose answers are fixed per address."""

    def __init__(self, answers: dict[int, object]) -> None:
        self.answers = answers
        self.reads: list[tuple[int, int]] = []

    @property
    def port(self) -> int:
        return 8899

    async def read_input(self, slave: int, address: int, count: int) -> list[int]:
        self.reads.append((slave, address))
        answer = self.answers.get(address, ModbusException(2))
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, list)
        return answer

    async def close(self) -> None:
        return None


async def test_detect_map_reads_the_type_register() -> None:
    hybrid = _Map({35000: [2005], 33022: [0]})
    assert await _detect_map(hybrid, 1) == "hybrid"
    assert (1, 33022) not in hybrid.reads
    # 35000 names the 3000 map but 3004 was refused: nothing this reader decodes.
    assert await _detect_map(_Map({35000: [1005], 33022: [0]}), 1) is None
    # No type register: 33022 decides.
    assert await _detect_map(_Map({33022: [0]}), 1) == "hybrid"
    assert await _detect_map(_Map({35000: [0], 33022: [0]}), 1) == "hybrid"
    assert await _detect_map(_Map({35000: []}), 1) is None
    silent = SolisLoggerOffline("silent", connected=True, reason=REASON_TIMEOUT)
    assert await _detect_map(_Map({35000: silent, 33022: [0]}), 1) == "hybrid"
    odd = SolisLoggerOffline("odd", connected=True, reason=REASON_WRONG_PROTOCOL)
    with pytest.raises(SolisLoggerOffline):
        await _detect_map(_Map({35000: odd}), 1)


async def test_a_hybrid_that_also_answers_3004_is_still_a_hybrid() -> None:
    """On a Modbus link a hybrid type in 35000 wins over a 3000-range answer."""
    both = _Map({3004: [0, 0], 35000: [2005]})
    assert await _detect_map(both, 1) == "hybrid"
    assert await _detect_map(_Map({3004: [0, 0], 35000: [1005]}), 1) == "string"
    # No or odd type register: the 3000 range that answered stays the map.
    assert await _detect_map(_Map({3004: [0, 0]}), 1) == "string"
    odd = SolisLoggerOffline("odd", connected=True, reason=REASON_WRONG_PROTOCOL)
    assert await _detect_map(_Map({3004: [0, 0], 35000: odd}), 1) == "string"


async def test_mbtcp_probe_finds_a_hybrid_that_serves_both_maps(socket_enabled: None) -> None:
    """The emulated hybrid answers 3004 too; Modbus TCP detection still says hybrid."""
    emulator, port = await _emulator(kind="s2", hybrid=True, hybrid_answers_3000=True)
    try:
        probed = await probe_logger(
            "127.0.0.1", SERIAL, timeout=0.5, port=port, gap=0, protocol="mbtcp"
        )
    finally:
        await emulator.close()
    assert probed.register_map == "hybrid"


async def test_v5_probe_does_not_read_35000_after_3004(socket_enabled: None) -> None:
    """A Solarman stick is never asked above 3050 once the 3000 range answers."""
    emulator, port = await _emulator()
    try:
        probed = await probe_logger("127.0.0.1", SERIAL, timeout=0.5, port=port, gap=0)
    finally:
        await emulator.close()
    assert probed.register_map == "string"


# Finding 4: a silent slave is the next slave id, not another framing.


async def test_scan_skips_silent_slaves() -> None:
    gateway = no_rs485_answer(ModbusException(0x0B))
    assert gateway is not None
    client = _Map({3004: [0, 0]})

    async def _read(slave: int, address: int, count: int) -> list[int]:
        if slave == 1:
            raise gateway
        return [0, 0]

    client.read_input = _read  # type: ignore[method-assign]
    assert await _scan_slaves(client, (1, 2, 3)) == (2, "string")

    async def _silent(slave: int, address: int, count: int) -> list[int]:
        raise SolisLoggerOffline("silent", connected=True, reason=REASON_TIMEOUT)

    client.read_input = _silent  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline) as caught:
        await _scan_slaves(client, (1, 2))
    assert caught.value.reason == REASON_TIMEOUT
    # One slave answered without a Solis map: that is not a silent bus.
    assert await _scan_slaves(_Map({}), (1,)) is None

    async def _refused(slave: int, address: int, count: int) -> list[int]:
        raise SolisLoggerOffline("refused", refused=True)

    client.read_input = _refused  # type: ignore[method-assign]
    with pytest.raises(SolisLoggerOffline):
        await _scan_slaves(client, (1, 2))


async def test_probe_finds_the_inverter_on_slave_two(socket_enabled: None) -> None:
    """Slave 1 is silent on this bus, as on real RS485: the probe moves to slave 2."""
    emulator, port = await _emulator(slaves=(2,))
    try:
        probed = await probe_logger("127.0.0.1", SERIAL, timeout=0.4, port=port, gap=0)
    finally:
        await emulator.close()
    assert probed.slave == 2
    assert probed.protocol == "v5"
    assert probed.inverter_serial == INVERTER_SERIAL


async def test_probe_on_a_silent_bus_is_a_timeout(socket_enabled: None) -> None:
    emulator, port = await _emulator(slaves=(7,))
    try:
        with pytest.raises(SolisLoggerOffline) as caught:
            await probe_logger("127.0.0.1", SERIAL, timeout=0.3, port=port, gap=0, protocol="v5")
    finally:
        await emulator.close()
    assert caught.value.reason == REASON_TIMEOUT


async def test_probe_and_read_a_hybrid_stick(socket_enabled: None) -> None:
    """The hybrid emulator refuses 3004 and names its map at 35000; heartbeats are answered."""
    emulator, port = await _emulator(hybrid=True, heartbeat=True)
    try:
        probed = await probe_logger("127.0.0.1", SERIAL, timeout=0.5, port=port, gap=0)
        assert probed.register_map == "hybrid"
        assert probed.inverter_serial == HYBRID_SERIAL
        local = SolisLocalTransport(
            "127.0.0.1",
            SERIAL,
            slave=probed.slave,
            register_map=probed.register_map,
            inverter_serial=probed.inverter_serial,
            protocol=probed.protocol,
            timeout=0.5,
            gap=0,
            port=port,
        )
        try:
            snapshot = await local.async_read()
        finally:
            await local.close()
    finally:
        await emulator.close()
    inverter = snapshot.inverters[0]
    assert inverter.serial == HYBRID_SERIAL
    assert inverter.ac_power_w == 2510
    assert inverter.temperature_c == -3.0
    assert emulator.state.time_responses >= 1


async def test_hybrid_serial_read_failure_is_cached() -> None:
    serial, unsupported = await _serial_once(
        _Map({}), 1, known="", unsupported=False, logger_serial=5, register_map="hybrid"
    )
    assert (serial, unsupported) == ("L5", True)


# Findings 11 and 12: V5 framing.


def test_time_response_matches_pysolarmanv5() -> None:
    heartbeat = _heartbeat(SERIAL)
    reply = build_time_response(SERIAL, heartbeat, now=1_700_000_000)
    assert reply[0] == 0xA5 and reply[-1] == 0x15
    assert reply[3:5] == bytes((0x10, 0x17))
    assert reply[5] == (heartbeat[5] + 1) & 0xFF
    assert reply[6] == heartbeat[6]
    assert logger_serial_of(reply) == SERIAL
    assert struct.unpack("<HII", reply[11:21]) == (0x0100, 1_700_000_000, 0)
    assert reply[-2] == sum(reply[1:-2]) & 0xFF


async def test_v5_skips_stray_frames_and_answers_heartbeats(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old replies, another logger and a heartbeat come first; the real reply still wins."""
    monkeypatch.setattr("custom_components.solis.solarman.random.randrange", lambda *_: 0x42)
    seen: list[bytes] = []

    async def _stick(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await _frame(reader)
        seen.append(request)
        low = sequence_of(request) & 0xFF
        writer.write(build_response(SERIAL, 0x1700 | ((low - 1) & 0xFF), _data(1, [7])))
        writer.write(build_response(SERIAL + 1, 0x1800 | low, _data(1, [8])))
        writer.write(build_response(SERIAL, 0x1900 | low, _data(2, [9])))
        writer.write(_heartbeat(SERIAL))
        writer.write(build_response(SERIAL, 0x1A00 | low, _data(1, [1234])))
        await writer.drain()
        seen.append(await _frame(reader))
        await reader.read()

    server, port = await _serve(_stick)
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=1, gap=0)
    try:
        assert await client.read_input(1, 3004, 1) == [1234]
    finally:
        await client.close()
        await _stop(server)
    assert sequence_of(seen[0]) == 0x42
    assert seen[1][3:5] == bytes((0x10, 0x17))


async def test_v5_deadline_covers_endless_heartbeats(socket_enabled: None) -> None:
    async def _chatty(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _frame(reader)
        while True:
            writer.write(_heartbeat(SERIAL))
            try:
                await writer.drain()
            except ConnectionError:
                return
            await asyncio.sleep(0.05)

    server, port = await _serve(_chatty)
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.4, gap=0)
    started = time.monotonic()
    try:
        with pytest.raises(SolisLoggerOffline) as caught:
            await client.read_input(1, 3004, 1)
    finally:
        await client.close()
        await _stop(server)
    assert time.monotonic() - started < 1.5
    assert caught.value.connected


async def test_v5_empty_payload_and_gateway_exception_are_timeouts(socket_enabled: None) -> None:
    replies = [b"", _exception(1, 0x0B), _exception(1, 0x0A), _exception(1, 0x02)]

    async def _stick(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                request = await _frame(reader)
            except ConnectionError:
                return
            writer.write(build_response(SERIAL, sequence_of(request), replies.pop(0)))
            await writer.drain()

    server, port = await _serve(_stick)
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.5, gap=0)
    try:
        for _ in range(3):
            with pytest.raises(SolisLoggerOffline) as caught:
                await client.read_input(1, 3004, 1)
            assert caught.value.reason == REASON_TIMEOUT
            assert caught.value.connected
        with pytest.raises(ModbusException) as illegal:
            await client.read_input(1, 3004, 1)
        assert illegal.value.code == 2
    finally:
        await client.close()
        await _stop(server)


async def test_v5_reopens_a_stale_socket_once(socket_enabled: None) -> None:
    """The stick dropped the idle session: the second poll reconnects and succeeds."""
    connections: list[int] = []

    async def _stick(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(1)
        request = await _frame(reader)
        writer.write(build_response(SERIAL, sequence_of(request), _data(1, [len(connections)])))
        await writer.drain()
        if len(connections) == 1:
            writer.close()
            return
        await reader.read()

    server, port = await _serve(_stick)
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.5, gap=0)
    try:
        assert await client.read_input(1, 3004, 1) == [1]
        await asyncio.sleep(0.05)
        assert await client.read_input(1, 3004, 1) == [2]
    finally:
        await client.close()
        await _stop(server)
    assert len(connections) == 2


async def test_modbus_tcp_gateway_exception_is_a_timeout(socket_enabled: None) -> None:
    async def _gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readexactly(12)
        writer.write(request[:4] + b"\x00\x03" + bytes((request[6], 0x84, 0x0B)))
        await writer.drain()
        await reader.read()

    server, port = await _serve(_gateway)
    client = ModbusTcpClient("127.0.0.1", port=port, timeout=0.5, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline) as caught:
            await client.read_input(1, 3004, 1)
    finally:
        await client.close()
        await _stop(server)
    assert caught.value.reason == REASON_TIMEOUT


# Finding 13: RTU resync.


def test_find_rtu_skips_noise_and_other_slaves() -> None:
    ours = _data(1, [42])
    other = _data(2, [9])
    frame, rest, dropped, skipped = find_rtu(b"\x00\xff" + other + ours + b"\x01", 1, 4, 1)
    assert frame == ours
    assert rest == b"\x01"
    assert dropped == 2
    assert skipped
    partial, rest, _, _ = find_rtu(ours[:-1], 1, 4, 1)
    assert partial is None and rest == ours[:-1]
    exception, _, _, _ = find_rtu(_exception(1, 0x0B), 1, 4, 1)
    assert exception == _exception(1, 0x0B)
    corrupt = bytearray(ours)
    corrupt[3] ^= 0xFF
    assert find_rtu(bytes(corrupt), 1, 4, 1)[0] is None


async def test_rtu_resyncs_after_echo_noise_and_another_slave(socket_enabled: None) -> None:
    async def _bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readexactly(8)
        writer.write(request[:3])
        await writer.drain()
        await asyncio.sleep(0.02)
        writer.write(request[3:] + b"\x00\x13" + _data(3, [5]))
        await writer.drain()
        await asyncio.sleep(0.02)
        writer.write(_data(1, [3456]))
        await writer.drain()
        await reader.read()

    server, port = await _serve(_bridge)
    client = RtuTcpClient("127.0.0.1", port=port, timeout=0.5, gap=0)
    try:
        assert await client.read_input(1, 3004, 1) == [3456]
    finally:
        await client.close()
        await _stop(server)


@pytest.mark.parametrize(
    ("payload", "close", "reason"),
    [
        (_data(3, [5]), False, REASON_WRONG_SLAVE),
        (b"\x00\x13\x99\x98\x97\x96", False, REASON_WRONG_PROTOCOL),
        (b"", False, REASON_TIMEOUT),
        (_data(3, [5]), True, REASON_WRONG_SLAVE),
        (_exception(1, 0x0B), False, REASON_TIMEOUT),
    ],
)
async def test_rtu_names_what_it_saw(
    socket_enabled: None, payload: bytes, close: bool, reason: str
) -> None:
    async def _bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(8)
        if payload:
            writer.write(payload)
            await writer.drain()
        if close:
            writer.close()
            return
        await reader.read()

    server, port = await _serve(_bridge)
    client = RtuTcpClient("127.0.0.1", port=port, timeout=0.3, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline) as caught:
            await client.read_input(1, 3004, 1)
    finally:
        await client.close()
        await _stop(server)
    assert caught.value.reason == reason
    assert rtu_request(1, 4, 3004, 1) == rtu_read(1, 3004, 1)


# Finding 19: the emulator behaves like a stick.


async def test_emulator_echoes_only_the_low_sequence_byte(socket_enabled: None) -> None:
    emulator, port = await _emulator(heartbeat=True)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(build_request(SERIAL, 0x0102, rtu_read(1, 3004, 2)))
        await writer.drain()
        first, reply = await asyncio.wait_for(_frames(reader, 2), 2)
    finally:
        writer.close()
        await emulator.close()
    assert first[4] == 0x47
    assert reply[5] == 0x02
    assert reply[6] == emulator.state.counter
    assert reply[6] != 0x01


async def test_emulator_unknown_slave_and_map_errors(socket_enabled: None) -> None:
    emulator, port = await _emulator()
    client = SolarmanClient("127.0.0.1", SERIAL, port=port, timeout=0.3, gap=0)
    try:
        with pytest.raises(SolisLoggerOffline) as caught:
            await client.read_input(5, 3004, 1)
        assert caught.value.reason == REASON_TIMEOUT
        with pytest.raises(ModbusException):
            await client.read_input(1, 33022, 1)
    finally:
        await client.close()
        await emulator.close()
