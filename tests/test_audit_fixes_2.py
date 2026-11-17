"""Second audit batch: spoofed announcements, junk peers, stale repairs and sensor edges."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .conftest import entry_data, entry_options
from .test_coordinator import _entry, _snapshot
from .test_web_stick import _Cloud, _cloud_snapshot, _transport
from custom_components.solis import async_remove_config_entry_device, local_transport as lt
from custom_components.solis.cloud_fill import fill_from_cloud
from custom_components.solis.config_flow import _normalize_url
from custom_components.solis.const import DOMAIN, LOGGER_WEB, SOURCE_BOTH, SOURCE_LOCAL
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.diagnostics import async_get_config_entry_diagnostics
from custom_components.solis.errors import SolisLoggerOffline
from custom_components.solis.local_transport import SolisPriorityTransport
from custom_components.solis.loggers.rtu import RtuTcpClient
from custom_components.solis.loggers.web import WebStickClient
from custom_components.solis.registers import decode_string
from custom_components.solis.solarman import (
    SolarmanClient,
    _is_lan_address,
    discover_sticks,
)
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState, captured_registers
from mock_soliscloud.web_stick import WebStickEmulator


def test_discovery_replies_must_name_a_lan_address() -> None:
    """A reply that names a public, multicast or unspecified host is never followed."""
    assert _is_lan_address("192.168.0.10")
    assert _is_lan_address("127.0.0.1")
    for host in ("8.8.8.8", "224.0.0.1", "0.0.0.0"):
        assert not _is_lan_address(host)


async def test_a_discovery_reply_naming_another_host_is_ignored(socket_enabled: None) -> None:
    """The reply has to come from the address it announces."""
    loop = asyncio.get_running_loop()

    class _Liar(asyncio.DatagramProtocol):
        def connection_made(self, transport: Any) -> None:
            self.transport = transport

        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            self.transport.sendto(b"10.9.9.9,BC54F9F57B5C,2200001234", addr)

    transport, _ = await loop.create_datagram_endpoint(
        _Liar, local_addr=("127.0.0.1", 48899), reuse_port=True
    )
    try:
        assert await discover_sticks(timeout=0.4) == []
    finally:
        transport.close()


@pytest.mark.parametrize(
    ("url", "scheme"),
    [
        ("http://[2001:4860:4860::8888]:13333", "https"),
        ("http://134744072:13333", "https"),
        ("http://8.8.8.8:13333", "https"),
        ("http://192.168.1.5:13333", "http"),
        ("http://[::1]:13333", "http"),
        ("http://mock:8765", "http"),
        ("http://localhost:8765", "http"),
    ],
)
def test_plain_http_stays_on_private_hosts_only(url: str, scheme: str) -> None:
    """The signed request must not cross a public network in cleartext."""
    assert _normalize_url(url).startswith(f"{scheme}://")


async def test_a_huge_content_length_is_a_page_fault_not_a_crash(
    stick: WebStickEmulator,
) -> None:
    """A stick that sends a 5000 digit length gets the usual fault."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\nx")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = WebStickClient("127.0.0.1", "x", port=port, timeout=1)
        from custom_components.solis.errors import SolisLoggerBusy

        with pytest.raises(SolisLoggerBusy):
            await client.read_inverter()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


async def test_a_peer_streaming_junk_cannot_hold_a_read(socket_enabled: None) -> None:
    """Every chunk used to restart the timeout. A read now has an overall deadline."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while not writer.is_closing():
                writer.write(b"\x00" * 8)
                await writer.drain()
                await asyncio.sleep(0.05)
        except ConnectionError:
            return

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for client in (
            SolarmanClient("127.0.0.1", 1, port=port, timeout=0.3, gap=0),
            RtuTcpClient("127.0.0.1", port=port, timeout=0.3, gap=0),
        ):
            started = asyncio.get_running_loop().time()
            with pytest.raises(SolisLoggerOffline):
                await client.read_input(1, 3004, 1)
            assert asyncio.get_running_loop().time() - started < 3
            await client.close()
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()
        await asyncio.sleep(0.2)


async def test_dhcp_follows_private_addresses_and_never_moves_a_web_stick(
    hass: HomeAssistant,
) -> None:
    """A broadcast cannot send a stored password to a new address."""
    web = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_WEB,
            "logger_host": "192.168.0.112",
            "logger_mac": "b8:d6:1a:58:a7:0b",
            "logger_password": "secret-pass",
        },
        version=3,
        unique_id="web:1",
    )
    web.add_to_hass(hass)
    moved = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "192.168.0.250", "macaddress": "b8:d6:1a:58:a7:0b", "hostname": "x"},
    )
    assert moved["reason"] == "already_configured"
    assert web.data["logger_host"] == "192.168.0.112"
    public = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "8.8.8.8", "macaddress": "b8:d6:1a:58:a7:0b", "hostname": "ew11"},
    )
    assert public["reason"] == "not_solis_logger"
    assert web.data["logger_host"] == "192.168.0.112"
    named = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "stick", "macaddress": "b8:d6:1a:58:a7:0b", "hostname": "x"},
    )
    assert named["reason"] == "not_solis_logger"


async def test_reconfigure_aborts_when_another_entry_owns_the_logger(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A local reconfigure cannot take a logger serial that another entry already uses."""
    owner = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_host": "127.0.0.1",
            "logger_serial": str(SERIAL),
        },
        options=entry_options(),
        version=3,
        unique_id=str(SERIAL),
    )
    other = MockConfigEntry(
        domain=DOMAIN,
        data={"source": SOURCE_LOCAL, "logger_host": "127.0.0.1", "logger_serial": "1"},
        options=entry_options(),
        version=3,
        unique_id="1",
    )
    owner.add_to_hass(hass)
    other.add_to_hass(hass)
    emulator = StickEmulator(StickState(tcp_port=8899, udp_port=0))
    await emulator.start()
    try:
        started = await other.start_reconfigure_flow(hass)
        assert started["step_id"] == "logger"
        result = await hass.config_entries.flow.async_configure(
            started["flow_id"],
            {"logger_host": "127.0.0.1", "logger_serial": str(SERIAL)},
        )
    finally:
        await emulator.close()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert other.unique_id == "1"
    assert other.data["logger_serial"] == "1"


async def test_a_repair_from_a_failed_setup_is_cleared_after_recovery(
    hass: HomeAssistant,
) -> None:
    """The new coordinator never opened it, but it removes it on the first good poll."""
    entry = _entry(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"invalid_auth_{entry.entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="invalid_auth",
    )

    class _Fine:
        async def async_read(self) -> Any:
            return replace(_snapshot(), inverters=(_cloud_snapshot().inverters[0],))

    coordinator = SolisCoordinator(hass, entry, _Fine())  # type: ignore[arg-type]
    await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"invalid_auth_{entry.entry_id}") is None


async def test_a_device_of_an_unloaded_entry_can_be_removed(hass: HomeAssistant) -> None:
    """There is no snapshot to protect, so Home Assistant may delete the device."""
    entry = _entry(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "inverter:X")}
    )
    assert await async_remove_config_entry_device(hass, entry, device)  # type: ignore[arg-type]


def test_a_grid_frequency_of_zero_is_no_reading() -> None:
    """Register 3042 reads 0 when the grid is gone. That is no frequency."""
    words = dict(captured_registers())
    words[3042] = 0
    snapshot = decode_string(words, serial="X", logger_serial="Y", now=datetime.now(UTC))
    assert snapshot.frequency_hz is None
    words[3042] = 5001
    assert (
        decode_string(words, serial="X", logger_serial="Y", now=datetime.now(UTC)).frequency_hz
        == 50.01
    )


def test_a_night_snapshot_stops_the_battery_current() -> None:
    """Battery power is 0 at night, so the current and voltage cannot stay live."""
    cloud = _cloud_snapshot()
    battery = replace(cloud.inverters[0], battery_current_a=-12.0, battery_voltage_v=51.0)
    asleep = lt.asleep_snapshot(replace(cloud, inverters=(battery,))).inverters[0]
    assert asleep.battery_current_a == 0.0
    assert asleep.battery_voltage_v is None


def test_a_cloud_fill_without_live_data_does_not_prune_devices() -> None:
    """Old cloud data leaves the snapshot partial."""
    cloud = _cloud_snapshot()
    web = replace(cloud, source="local")
    stale = fill_from_cloud(web, cloud, serial=cloud.inverters[0].serial, fresh=False)
    assert stale.partial is True
    live = fill_from_cloud(web, cloud, serial=cloud.inverters[0].serial, fresh=True)
    assert live.partial is False


async def test_identity_is_learned_after_a_failed_first_cloud_read(
    stick: WebStickEmulator,
) -> None:
    """One failed read at startup must not close the question for the whole session."""

    class _Flaky(_Cloud):
        broken = True

        async def async_read(self) -> Any:
            if self.broken:
                from custom_components.solis.errors import SolisCloudError

                raise SolisCloudError("down")
            return await super().async_read()

    cloud = _Flaky()
    local = _transport(stick)
    transport = SolisPriorityTransport(SOURCE_BOTH, local, cloud)
    transport._local = local
    local.cloud_fill = False  # the older identity path used by the other local kinds
    local.identity_checked = False
    local._has_series = False
    await transport.async_read()
    assert local.identity_checked is False
    cloud.broken = False
    transport._fill_due = 0.0
    await transport.async_read()
    assert local.identity_checked is True


async def test_a_local_entry_has_no_placeholder_plant_sensors(
    hass: HomeAssistant, stick: WebStickEmulator
) -> None:
    """Two local entries used to collide on the same four station sensors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_password": "123456789",
            "inverter_serial": "110E40182280017",
        },
        options=entry_options(),
        version=3,
        unique_id="web:110E40182280017",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = {item.unique_id for item in hass.data["entity_registry"].entities.values()}
    assert not any(unique.startswith("local_") for unique in ids)
    assert "110E40182280017_ac_power" in ids
    stick.state.alarm = "E01"
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    alarm = next(
        state
        for state in hass.states.async_all("binary_sensor")
        if state.entity_id.endswith("_alarm")
    )
    assert alarm.state == "on"
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["title"] == "**REDACTED**"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reconfigure_needs_a_host_for_a_local_connection(
    hass: HomeAssistant, solis_server: tuple[Any, str]
) -> None:
    """Choosing a local connection without a logger IP is an error, not a silent no-op."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options=entry_options(),
        version=3,
        unique_id="test-key",
    )
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        {
            "api_url": url,
            "station_ids": ["1900000000000001001", "1900000000000001002"],
            "scan_interval": 120,
            "request_timeout": 30,
            "endpoint_interval": 0.5,
            "source": SOURCE_BOTH,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "logger_host_required"


async def test_the_snapshot_is_saved_when_home_assistant_stops(
    hass: HomeAssistant, stick: WebStickEmulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain stop does not unload entries, so a stop listener writes the snapshot."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_password": "123456789",
            "inverter_serial": "110E40182280017",
        },
        options=entry_options(),
        version=3,
        unique_id="web:110E40182280017",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    saves: list[dict[str, Any]] = []

    async def record(data: dict[str, Any]) -> None:
        saves.append(data)

    monkeypatch.setattr(entry.runtime_data._store, "async_save", record)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert len(saves) == 1
