"""Probes for logger kinds auto-detect must not guess.

A Waveshare on port 502 answers the same Modbus TCP probe as an S2-WL-ST.
Raw RTU is not tried during auto-detect, because a V5 stick treats that
frame as noise and a second session is how those sticks lock up.
"""

from __future__ import annotations

from ..const import (
    LOGGER_RTU,
    PROTOCOL_MBTCP,
    PROTOCOL_RTU,
    REASON_CLOSED,
    REASON_GARBAGE,
    REASON_GATEWAY,
    REASON_REFUSED,
    REASON_SILENT,
    REASON_TIMEOUT,
    REASON_UNSUPPORTED,
    REASON_WRONG_PROTOCOL,
    REASON_WRONG_SLAVE,
    S2_PORT,
)
from ..errors import SolisLoggerBusy, SolisLoggerOffline
from ..local_transport import LoggerProbe, LoggerRequest, _detect_map, _serial_once
from ..solarman import ModbusTcpClient, host_gate
from .fingerprint import rtu_default_port
from .rtu import RtuTcpClient

_BridgeClient = ModbusTcpClient | RtuTcpClient


async def probe_bridge(request: LoggerRequest, *, serial_unsupported: bool) -> LoggerProbe:
    """Confirm a Solis map over MBAP or raw RTU. One socket, then close it."""
    port, client, protocol = _open_bridge(request)
    async with host_gate(request.host, port):
        return await _probe_bridge_locked(
            request,
            port=port,
            client=client,
            protocol=protocol,
            serial_unsupported=serial_unsupported,
        )


async def _probe_bridge_locked(
    request: LoggerRequest,
    *,
    port: int,
    client: _BridgeClient,
    protocol: str,
    serial_unsupported: bool,
) -> LoggerProbe:
    numeric = int(request.logger_serial) if request.logger_serial.isdigit() else 0
    answered = False
    try:
        slaves = (request.slave,) if request.slave else (1, 2, 3)
        for slave in slaves:
            register_map = await _detect_map(client, slave)
            if register_map is None:
                answered = True
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
                protocol=protocol,
                serial_unsupported=unsupported,
                kind=request.kind,
                port=port,
                function=client.function,
            )
    except SolisLoggerBusy:
        raise
    except SolisLoggerOffline as err:
        raise _annotate_bridge(err) from err
    finally:
        await client.close()
    if answered:
        raise SolisLoggerOffline(
            f"{request.host} speaks Modbus but not a Solis register map",
            connected=True,
            reason=REASON_UNSUPPORTED,
        )
    raise SolisLoggerOffline(f"{request.host} did not answer as a Solis inverter")


def _open_bridge(request: LoggerRequest) -> tuple[int, _BridgeClient, str]:
    if request.kind == LOGGER_RTU:
        port = request.port or rtu_default_port(request.preset)
        client: _BridgeClient = RtuTcpClient(
            request.host,
            port=port,
            timeout=request.timeout,
            gap=request.gap,
            fallback_function=3,
        )
        return port, client, PROTOCOL_RTU
    port = request.port or S2_PORT
    return (
        port,
        ModbusTcpClient(
            request.host,
            port=port,
            timeout=request.timeout,
            gap=request.gap,
            fallback_function=3,
        ),
        PROTOCOL_MBTCP,
    )


def _annotate_bridge(err: SolisLoggerOffline) -> SolisLoggerOffline:
    """Name a gateway fault. A Solis stick's firmware reasons are not used."""
    if err.reason in {REASON_WRONG_SLAVE, REASON_UNSUPPORTED, REASON_WRONG_PROTOCOL}:
        return err
    if err.refused or err.reason == REASON_REFUSED or not err.connected:
        return SolisLoggerOffline(str(err), connected=False, refused=True, reason=REASON_GATEWAY)
    if err.reason == REASON_GARBAGE:
        return SolisLoggerOffline(str(err), connected=True, reason=REASON_WRONG_PROTOCOL)
    if err.connected and err.reason in {REASON_TIMEOUT, REASON_SILENT, REASON_CLOSED}:
        return SolisLoggerOffline(str(err), connected=True, reason=REASON_TIMEOUT)
    return err
