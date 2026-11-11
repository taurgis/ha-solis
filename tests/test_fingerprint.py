"""Port 80 naming, RTU factory ports, and DHCP hostname adoption."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.solis.const import (
    DOMAIN,
    LOGGER_RTU,
    LOGGER_S5,
    REASON_S5_CLOSED,
    SOURCE_LOCAL,
)
from custom_components.solis.errors import SolisLoggerBusy
from custom_components.solis.local_transport import (
    LoggerRequest,
    SolisLocalTransport,
    identify_logger,
    parse_logger_form,
)
from custom_components.solis.loggers.fingerprint import (
    classify_http,
    hostname_is_known_gateway,
    read_http_banner,
    rtu_default_port,
)
from mock_soliscloud.stick import StickEmulator, StickState


def test_http_classification_and_ports() -> None:
    """A named page decides. An empty page does not. Factory ports stay per vendor."""
    s2 = classify_http("<title>S2-WL-ST datalogger</title>")
    assert s2.confident_kind == "s2_wl_st" and s2.confidence == "high"
    solis = classify_http("Solis data logger LAN Settings")
    assert solis.confident_kind == "s2_wl_st"
    vague = classify_http("Solis inverter online")
    assert vague.confident_kind == "" and vague.suggested == "s2_wl_st"
    gateway = classify_http("<title>Waveshare RS485 TO ETH</title>")
    assert gateway.confident_kind == "modbus_tcp" and gateway.detection == "auto_gateway_http"
    elfin = classify_http("Elfin-EW11 Hi-Flying")
    assert elfin.confident_kind == "modbus_tcp"
    both = classify_http("S2-WL-ST via Waveshare")
    assert both.confident_kind == ""
    assert both.suggested == "s2_wl_st"
    empty = classify_http("")
    assert empty.confident_kind == "" and empty.suggested == "s2_wl_st"
    unnamed = classify_http("<html>index</html>")
    assert unnamed.confident_kind == "" and "did not name" in unnamed.note
    assert hostname_is_known_gateway("EW11_ab12")
    assert hostname_is_known_gateway("usr-tcp232-410s")
    assert not hostname_is_known_gateway("")
    assert not hostname_is_known_gateway("kitchen")
    assert rtu_default_port("default") == 8899
    assert rtu_default_port("elfin") == 8899
    assert rtu_default_port("waveshare") == 4196
    assert rtu_default_port("usr") == 26
    assert rtu_default_port("esp") == 6638
    request, error = parse_logger_form(
        {"logger_host": "10.0.0.4", "logger_kind": "rtu_tcp", "logger_preset": "usr"}
    )
    assert error == "" and request is not None and request.preset == "usr" and request.port is None


async def test_http_banner_timeouts_and_a_page(socket_enabled: None) -> None:
    """Port 80 is read once. A closed port and a silent socket are empty."""

    async def _page(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(100)
        writer.write(b"HTTP/1.0 200 OK\r\n\r\nS2-WL-ST logger")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_page, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]  # type: ignore[index]
    try:
        text = await read_http_banner("127.0.0.1", port=port, timeout=1)
    finally:
        server.close()
        await server.wait_closed()
    assert "S2-WL-ST" in text
    assert await read_http_banner("127.0.0.1", port=1, timeout=0.3) == ""
    assert await read_http_banner("127.0.0.1\r\nX: y", timeout=0.2) == ""
    assert await read_http_banner("http://127.0.0.1", timeout=0.2) == ""

    async def _silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(30)
        writer.close()

    stalled = await asyncio.start_server(_silent, "127.0.0.1", 0)
    stalled_port = stalled.sockets[0].getsockname()[1]  # type: ignore[index]
    try:
        assert await read_http_banner("127.0.0.1", port=stalled_port, timeout=0.2) == ""
    finally:
        stalled.close()
        await stalled.wait_closed()


async def _s2() -> tuple[StickEmulator, int]:
    emulator = StickEmulator(StickState(kind="s2", tcp_port=0, udp_port=0))
    await emulator.start()
    assert emulator._tcp is not None
    return emulator, emulator._tcp.sockets[0].getsockname()[1]


def _pretend_502(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    """Auto-detect treats this high port as 502. Binding the real 502 is not allowed."""
    monkeypatch.setattr("custom_components.solis.local_transport.S2_PORT", port)


async def test_auto_names_an_s2_from_the_web_page(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """A Solis page on port 80 is enough. The form is not asked."""
    emulator, port = await _s2()
    _pretend_502(monkeypatch, port)

    async def _page(_host: str, **_kwargs: object) -> str:
        return "Solis datalogger LAN Settings"

    monkeypatch.setattr(
        "custom_components.solis.loggers.fingerprint.read_http_banner",
        _page,
    )
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                port=port,
                logger_serial="5A120010",
                timeout=2,
                gap=0,
            )
        )
    finally:
        await emulator.close()
    assert probe.kind == "s2_wl_st"
    assert probe.needs_choice is False
    assert probe.detection == "auto_s2_http"


async def test_auto_names_a_gateway_and_asks_when_the_page_is_blank(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """A vendor page is a gateway. No page means the user picks, S2 first."""
    emulator, port = await _s2()
    _pretend_502(monkeypatch, port)
    pages = {"text": "<html>Waveshare RS485 TO ETH</html>"}

    async def _page(_host: str, **_kwargs: object) -> str:
        return pages["text"]

    monkeypatch.setattr(
        "custom_components.solis.loggers.fingerprint.read_http_banner",
        _page,
    )
    try:
        named = await identify_logger(
            LoggerRequest("127.0.0.1", port=port, logger_serial="gw-9", timeout=2, gap=0)
        )
        assert named.kind == "modbus_tcp" and named.detection == "auto_gateway_http"
        pages["text"] = ""
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
        choice = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": "auto",
                "logger_host": "127.0.0.1",
                "logger_serial": "5A120011",
            },
        )
        assert choice["step_id"] == "logger_choice"
        assert "port 80" in choice["description_placeholders"]["note"].lower()
        forced = await hass.config_entries.flow.async_configure(
            choice["flow_id"],
            {"logger_kind": "s2_wl_st"},
        )
    finally:
        await emulator.close()
    assert forced["type"] is FlowResultType.CREATE_ENTRY
    assert forced["result"].data["logger_kind"] == "s2_wl_st"
    assert forced["result"].data["logger_detect"] == "confirmed_s2_wl_st"
    assert forced["result"].unique_id == "s2:5A120011"


async def test_choice_can_store_a_gateway_and_a_missing_serial_is_rejected(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """The user can pick the gateway. A stick that hides its serial still needs one."""

    async def _blank(_host: str, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(
        "custom_components.solis.loggers.fingerprint.read_http_banner",
        _blank,
    )
    emulator, port = await _s2()
    _pretend_502(monkeypatch, port)
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
        choice = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "logger_kind": "auto",
                "logger_host": "127.0.0.1",
                "logger_port": str(port),
                "logger_serial": "gw-2",
            },
        )
        created = await hass.config_entries.flow.async_configure(
            choice["flow_id"],
            {"logger_kind": "modbus_tcp"},
        )
    finally:
        await emulator.close()
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].unique_id == "tcp:gw-2"
    assert created["result"].data["logger_detect"] == "confirmed_modbus_tcp"
    locked, locked_port = await _s2()
    _pretend_502(monkeypatch, locked_port)
    locked.state.lock_above = 3050
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
            {
                "logger_kind": "auto",
                "logger_host": "127.0.0.1",
                "logger_port": str(locked_port),
            },
        )
    finally:
        await locked.close()
    assert failed["errors"]["base"] == "logger_identity"


async def test_usr_preset_supplies_the_port(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """An empty port with the USR preset follows rtu_default_port."""
    from tests.test_dongles import RtuBox

    box = RtuBox()
    await box.start()

    def _usr(preset: str) -> int:
        assert preset == "usr"
        return box.port

    monkeypatch.setattr("custom_components.solis.loggers.probe.rtu_default_port", _usr)
    try:
        probe = await identify_logger(
            LoggerRequest(
                "127.0.0.1",
                kind=LOGGER_RTU,
                preset="usr",
                logger_serial="usr-1",
                timeout=2,
                gap=0,
            )
        )
    finally:
        await box.close()
    assert probe.kind == LOGGER_RTU
    assert probe.port == box.port


async def test_s5_closed_port_is_its_own_reason(socket_enabled: None) -> None:
    """An experimental S5 that later refuses 502 is not described as an S2 firmware note."""
    transport = SolisLocalTransport(
        "127.0.0.1",
        0,
        slave=1,
        register_map="string",
        inverter_serial="SOLIS001",
        protocol="mbtcp",
        port=1,
        kind=LOGGER_S5,
        stick_serial="5A9",
        gap=0,
        timeout=0.4,
    )
    with pytest.raises(SolisLoggerBusy) as closed:
        await transport.async_read()
    assert closed.value.reason == REASON_S5_CLOSED
    await transport.close()


async def test_dhcp_remembers_a_gateway_mac_and_ignores_a_stranger(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A known hostname at the saved address stores the MAC. A later lease moves the host."""
    from homeassistant.config_entries import SOURCE_DHCP

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": "modbus_tcp",
            "logger_host": "127.0.0.1",
            "logger_serial": "gw-1",
            "inverter_serial": "SOLIS001",
        },
        version=3,
        unique_id="tcp:gw-1",
    )
    entry.add_to_hass(hass)
    adopted = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "aa:bb:cc:dd:ee:10", "hostname": "EW11_ab12"},
    )
    assert adopted["reason"] == "already_configured"
    assert entry.data["logger_mac"] == "aa:bb:cc:dd:ee:10"
    held = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "de:ad:be:ef:00:01", "hostname": "EW11"},
    )
    assert held["reason"] == "already_configured"
    assert entry.data["logger_mac"] == "aa:bb:cc:dd:ee:10"
    assert entry.data["logger_host"] == "127.0.0.1"
    again = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.1", "macaddress": "aa:bb:cc:dd:ee:10", "hostname": "EW11_ab12"},
    )
    assert again["reason"] == "already_configured"
    moved = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "127.0.0.2", "macaddress": "aa:bb:cc:dd:ee:10", "hostname": "EW11_ab12"},
    )
    assert moved["reason"] == "already_configured"
    assert entry.data["logger_host"] == "127.0.0.2"
    stranger = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.9.9.9", "macaddress": "11:22:33:44:55:66", "hostname": "kitchen"},
    )
    assert stranger["reason"] == "not_solis_logger"
    missing = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.9.9.8", "macaddress": "11:22:33:44:55:77", "hostname": "usr-tcp232-410s"},
    )
    assert missing["reason"] == "logger_not_setup"


async def test_choice_rejects_a_kind_that_is_not_offered(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> None:
    """The identification question only accepts S2-WL-ST or a Modbus TCP gateway."""
    from custom_components.solis.local_transport import LoggerProbe

    probe = LoggerProbe(
        1,
        "string",
        "SOLIS001",
        "mbtcp",
        False,
        kind="s2_wl_st",
        port=502,
        needs_choice=True,
        suggested_kind="s2_wl_st",
        identify_note="Pick the device.",
    )

    async def _named(_request: object) -> LoggerProbe:
        return probe

    monkeypatch.setattr("custom_components.solis.config_flow.identify_logger", _named)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"source": SOURCE_LOCAL, "api_url": "http://127.0.0.1:9", "key_id": "", "key_secret": ""},
    )
    choice = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"logger_kind": "auto", "logger_host": "127.0.0.1", "logger_serial": "5A120011"},
    )
    assert choice["step_id"] == "logger_choice"
    from homeassistant.data_entry_flow import InvalidData

    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            choice["flow_id"],
            {"logger_kind": "solarman"},
        )
    created = await hass.config_entries.flow.async_configure(
        choice["flow_id"],
        {"logger_kind": "s2_wl_st"},
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].data["logger_kind"] == "s2_wl_st"
