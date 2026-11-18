"""Solis stick that only serves a status web page (inverter.cgi)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import errno
import time
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import entry_data, entry_options
from .flow_helpers import logger_form, pick, start_user
from custom_components.solis.cloud_fill import fill_from_cloud, match_inverter
from custom_components.solis.const import (
    DOMAIN,
    LOGGER_WEB,
    REASON_SILENT,
    REASON_TIMEOUT,
    REASON_WEB_AUTH,
    REASON_WEB_PAGE,
    SOURCE_BOTH,
    SOURCE_LOCAL,
    WEB_DEFAULT_PASSWORD,
)
from custom_components.solis.coordinator import SolisCoordinator
from custom_components.solis.errors import (
    SolisCloudError,
    SolisLoggerAuthError,
    SolisLoggerBusy,
    SolisLoggerOffline,
)
from custom_components.solis.local_transport import (
    LoggerProbe,
    LoggerRequest,
    SolisPriorityTransport,
    _auto_web,
    identify_logger,
    logger_form_error,
    logger_setup,
    needs_stable_id,
    parse_logger_form,
)
from custom_components.solis.loggers import detection_code, detection_why, spec_for
from custom_components.solis.loggers.web import (
    WebStickClient,
    is_solis_web_banner,
    parse_collector,
    parse_inverter,
)
from custom_components.solis.models import SolisSnapshot
from custom_components.solis.registers import decode_string
from custom_components.solis.web_transport import SolisWebTransport, probe_web
from mock_soliscloud.stick import captured_registers
from mock_soliscloud.web_stick import WebStickEmulator

PAGE = "1031000000000001;0F000F;E4;38.4;382;2.800000;21986;NO;"
COLLECTOR = (
    "\x00" * 33 + ";2;7A00000000000001;10010106;Disabled;null;null;Disabled;null;null;"
    "192.168.0.112;B8:D6:1A:00:00:01;Connected;null;"
)


def _transport(stick: WebStickEmulator, **kwargs: Any) -> SolisWebTransport:
    return SolisWebTransport(
        "127.0.0.1", "123456789", port=stick.port, timeout=kwargs.pop("timeout", 3.0), **kwargs
    )


def _cloud_snapshot() -> SolisSnapshot:
    inverter = decode_string(
        captured_registers(),
        serial="1031000000000001",
        logger_serial="7A00000000000001",
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
    def __init__(self) -> None:
        self.reads = 0

    async def async_read(self) -> SolisSnapshot:
        self.reads += 1
        return _cloud_snapshot()


class _DownCloud:
    async def async_read(self) -> SolisSnapshot:
        raise SolisCloudError("cloud is down")


def test_parse_inverter_and_collector() -> None:
    """The semicolon strings of a real stick, with and without padding."""
    page = parse_inverter(" " * 30 + PAGE + " " * 200)
    assert parse_inverter("\x00" * 33 + PAGE + " " * 200).serial == "1031000000000001"
    assert page.serial == "1031000000000001"
    assert page.firmware == "0F000F"
    assert page.model_code == "E4"
    assert (page.temperature_c, page.power_w) == (38.4, 382.0)
    assert (page.energy_today_kwh, page.energy_total_kwh) == (2.8, 21986.0)
    assert not page.in_alarm
    assert parse_inverter(PAGE.replace(";NO;", ";Fault;")).in_alarm
    for bad in ("", "<html>Not the inverter</html>", "a;b;c", PAGE.replace("382", "x")):
        with pytest.raises(ValueError):
            parse_inverter(bad)
    collector = parse_collector(COLLECTOR)
    assert collector is not None
    assert (collector.serial, collector.version) == ("7A00000000000001", "10010106")
    assert collector.mac == "B8:D6:1A:00:00:01"
    assert parse_collector(COLLECTOR.replace("B8:D6:1A:00:00:01", "null")).mac == ""  # type: ignore[union-attr]
    assert parse_collector("short") is None
    assert parse_collector(COLLECTOR.replace("7A00000000000001", "")) is None


def test_banner_check_needs_the_solis_realm() -> None:
    """Another stick or a router page is not claimed."""
    assert is_solis_web_banner('HTTP/1.0 401 unauthorized\r\nWWW-authenticate: Basic realm="solis"')
    assert not is_solis_web_banner('HTTP/1.0 401\r\nWWW-authenticate: Basic realm="router"')
    assert not is_solis_web_banner("")


async def test_client_reads_both_pages(stick: WebStickEmulator) -> None:
    """One short connection per page, with the basic-auth password."""
    client = WebStickClient("127.0.0.1", "123456789", port=stick.port, timeout=3)
    assert (await client.read_inverter()).power_w == 382.0
    collector = await client.read_collector()
    assert collector is not None
    assert collector.serial == "7A00000000000001"
    await client.close()
    assert stick.state.requests == ["/inverter.cgi", "/moniter.cgi"]


async def test_client_names_each_failure(stick: WebStickEmulator) -> None:
    """A wrong password, another page, no reply and no port are different faults."""
    wrong = WebStickClient("127.0.0.1", "nope", port=stick.port, timeout=3)
    with pytest.raises(SolisLoggerBusy) as auth:
        await wrong.read_inverter()
    assert auth.value.reason == REASON_WEB_AUTH
    with pytest.raises(SolisLoggerBusy) as auth_again:
        await wrong.read_collector()
    assert auth_again.value.reason == REASON_WEB_AUTH
    good = WebStickClient("127.0.0.1", "123456789", port=stick.port, timeout=0.5)
    for mode in ("garbage", "error", "silent", "nopage", "redirect"):
        stick.state.mode = mode
        with pytest.raises(SolisLoggerBusy) as page:
            await good.read_inverter()
        assert page.value.reason == REASON_WEB_PAGE, mode
    stick.state.mode = "reset"
    with pytest.raises(SolisLoggerBusy) as reset:
        await good.read_inverter()
    assert reset.value.reason == REASON_WEB_PAGE
    assert "reset" in str(reset.value)
    stick.state.mode = "garbage"
    assert await good.read_collector() is None
    stick.state.mode = "hang"
    with pytest.raises(SolisLoggerOffline) as hung:
        await good.read_inverter()
    assert hung.value.connected
    assert hung.value.reason == REASON_TIMEOUT
    stick.state.mode = "ok"
    unknown = WebStickClient("127.0.0.1", "123456789", port=stick.port, timeout=3)
    await stick.close()
    with pytest.raises(SolisLoggerOffline) as closed:
        await unknown.read_inverter()
    assert closed.value.refused
    assert not closed.value.connected
    with pytest.raises(SolisLoggerOffline):
        await WebStickClient("bad host/../x", "x").read_inverter()


async def test_transport_publishes_a_local_snapshot(stick: WebStickEmulator) -> None:
    """Values from the page, the serial it reports, and nothing it does not have."""
    learned: list[str] = []
    transport = _transport(stick, on_serial=learned.append)
    snapshot = await transport.async_read()
    assert snapshot.source == "local"
    inverter = snapshot.inverters[0]
    assert inverter.serial == "1031000000000001"
    assert inverter.data_source == "local"
    assert (inverter.ac_power_w, inverter.temperature_c) == (382.0, 38.4)
    assert (inverter.energy_today_kwh, inverter.energy_total_kwh) == (2.8, 21986.0)
    assert (inverter.hmi_version, inverter.model_code, inverter.state) == ("0F000F", "E4", 1)
    assert inverter.strings == () and inverter.phases == ()
    assert inverter.energy_month_kwh is None and inverter.frequency_hz is None
    assert learned == ["1031000000000001"]
    assert transport.inverter_serial == "1031000000000001"
    assert transport.last_snapshot is snapshot
    assert transport.logger_serial == 0
    assert snapshot.collectors[0].model == "Solis web page"
    stick.state.alarm = "Fault"
    assert (await transport.async_read()).inverters[0].state == 3
    await transport.close()


async def test_a_configured_serial_wins_over_the_page(stick: WebStickEmulator) -> None:
    """Entity ids stay stable when the entry already names the inverter."""
    learned: list[str] = []
    transport = _transport(stick, inverter_serial="MINE", on_serial=learned.append)
    assert (await transport.async_read()).inverters[0].serial == "MINE"
    assert learned == []
    transport.adopt_serial("")
    transport.adopt_serial("MINE")
    transport.adopt_serial("CLOUD")
    assert learned == ["CLOUD"]
    assert transport.inverter_serial == "CLOUD"


async def test_identity_and_session_info(stick: WebStickEmulator) -> None:
    """A series name from the cloud ends the identity lookup. The password never shows."""
    transport = _transport(stick)
    assert transport.needs_cloud_identity
    await transport.async_read()
    assert transport.needs_cloud_identity
    donor = _cloud_snapshot().inverters[0]
    transport.note_identity(donor)
    assert not transport.needs_cloud_identity
    info = transport.session_info()
    assert info["kind"] == LOGGER_WEB
    assert info["protocol"] == "http"
    assert "123456789" not in str(info)


def _no_route(transport: SolisWebTransport) -> None:
    """The stick is powered down: the network has no route to it."""

    async def unreachable(path: str) -> bytes:
        raise OSError(errno.EHOSTUNREACH, "no route to host")

    transport._client._exchange = unreachable  # type: ignore[method-assign]


async def test_night_keeps_totals_and_a_dark_stick_without_a_sample_is_offline(
    stick: WebStickEmulator,
) -> None:
    """No route is asleep, as for the other inverter-powered sticks."""
    transport = _transport(stick)
    await transport.async_read()
    _no_route(transport)
    asleep = await transport.async_read()
    assert asleep.source == "asleep"
    assert asleep.inverters[0].ac_power_w == 0.0
    assert asleep.inverters[0].energy_total_kwh == 21986.0
    assert (await transport.async_read()).source == "asleep"
    fresh = SolisWebTransport("127.0.0.1", "123456789", port=stick.port, timeout=1)
    _no_route(fresh)
    with pytest.raises(SolisLoggerOffline):
        await fresh.async_read()
    refused = SolisWebTransport("127.0.0.1", "123456789", port=1, timeout=1)
    with pytest.raises(SolisLoggerBusy) as fault:
        await refused.async_read()
    assert fault.value.reason == REASON_WEB_PAGE


async def test_wrong_password_pauses_and_recovers(
    stick: WebStickEmulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected password backs off, names the reason and clears on the next good read."""
    wrong = SolisWebTransport("127.0.0.1", "nope", port=stick.port, timeout=2)
    with pytest.raises(SolisLoggerBusy) as first:
        await wrong.async_read()
    assert first.value.reason == REASON_WEB_AUTH
    assert wrong.degraded_reason == REASON_WEB_AUTH
    before = len(stick.state.requests)
    with pytest.raises(SolisLoggerBusy) as waiting:
        await wrong.async_read()
    assert waiting.value.waiting
    assert waiting.value.reason == REASON_WEB_AUTH
    assert len(stick.state.requests) == before
    stick.state.password = "nope"
    wrong._next_attempt = 0.0
    assert (await wrong.async_read()).source == "local"
    assert wrong.degraded_reason == ""
    assert wrong._next_attempt == 0.0


async def test_open_port_without_a_page_is_a_fault_not_night(stick: WebStickEmulator) -> None:
    """TCP opened and nothing usable came back. That is not the inverter powering off."""
    transport = _transport(stick, timeout=0.5)
    await transport.async_read()
    stick.state.mode = "hang"
    with pytest.raises(SolisLoggerBusy) as busy:
        await transport.async_read()
    assert busy.value.reason == REASON_WEB_PAGE
    assert transport.degraded_reason == REASON_WEB_PAGE


async def test_probe_reads_the_stick_page_and_collector(stick: WebStickEmulator) -> None:
    """The probe learns the serial, the stick serial and the MAC from the two pages."""
    request = LoggerRequest(
        "127.0.0.1", LOGGER_WEB, stick.port, password=WEB_DEFAULT_PASSWORD, timeout=3
    )
    probe = await probe_web(request, auto=False)
    assert probe.kind == LOGGER_WEB
    assert probe.inverter_serial == "1031000000000001"
    assert probe.stick_serial == "7A00000000000001"
    assert probe.mac == "B8:D6:1A:00:00:01"
    named = await probe_web(
        LoggerRequest(
            "127.0.0.1",
            LOGGER_WEB,
            stick.port,
            inverter_serial="MINE",
            password=WEB_DEFAULT_PASSWORD,
        ),
        auto=True,
    )
    assert named.inverter_serial == "MINE"
    blank = await probe_web(LoggerRequest("127.0.0.1", LOGGER_WEB, stick.port), auto=False)
    assert blank.inverter_serial == "1031000000000001"
    stick.state.mode = "garbage"
    with pytest.raises(SolisLoggerBusy) as page:
        await probe_web(LoggerRequest("127.0.0.1", LOGGER_WEB, stick.port), auto=False)
    assert page.value.reason == REASON_WEB_PAGE
    stick.state.mode = "ok"
    with pytest.raises(SolisLoggerBusy) as auth:
        await probe_web(
            LoggerRequest("127.0.0.1", LOGGER_WEB, stick.port, password="wrong"), auto=False
        )
    assert auth.value.reason == REASON_WEB_AUTH


async def test_probe_without_a_collector_page_still_works(stick: WebStickEmulator) -> None:
    """A page that differs on moniter.cgi costs the MAC and the stick serial, not the entry."""
    original = stick._body

    def body(path: str) -> str | None:
        return None if path.startswith("/moniter.cgi") else original(path)

    stick._body = body  # type: ignore[method-assign]
    probe = await probe_web(LoggerRequest("127.0.0.1", LOGGER_WEB, stick.port), auto=False)
    assert (probe.stick_serial, probe.mac) == ("", "")
    assert probe.inverter_serial == "1031000000000001"


async def test_auto_detect_needs_a_solis_page(
    stick: WebStickEmulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port 80 has to say realm solis. A plain page is not offered."""
    request = LoggerRequest("127.0.0.1", port=stick.port)
    assert (await probe_web(request, auto=True)).kind == LOGGER_WEB

    async def plain(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.0 200 OK\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain, "127.0.0.1", 0)
    other = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(SolisLoggerOffline):
            await probe_web(LoggerRequest("127.0.0.1", port=other), auto=True)
    finally:
        server.close()
        await server.wait_closed()
    failure = SolisLoggerOffline("nothing", refused=True)
    with pytest.raises(SolisLoggerOffline) as kept:
        await _auto_web(LoggerRequest("127.0.0.1", port=other), failure)
    assert kept.value is failure
    silent = SolisLoggerOffline("s2", connected=True, reason=REASON_SILENT)
    with pytest.raises(SolisLoggerOffline) as s2:
        await _auto_web(request, silent)
    assert s2.value is silent
    monkeypatch.setattr("custom_components.solis.web_transport.WEB_PORT", stick.port)
    found = await identify_logger(LoggerRequest("127.0.0.1", timeout=2))
    assert found.kind == LOGGER_WEB
    assert found.inverter_serial == "1031000000000001"
    stick.state.password = "other"
    with pytest.raises(SolisLoggerBusy) as auth:
        await identify_logger(LoggerRequest("127.0.0.1", timeout=2))
    assert auth.value.reason == REASON_WEB_AUTH


async def test_explicit_kind_skips_the_modbus_probes(stick: WebStickEmulator) -> None:
    """Picking Solis web page goes straight to the page."""
    found = await identify_logger(
        LoggerRequest("127.0.0.1", LOGGER_WEB, stick.port, password=WEB_DEFAULT_PASSWORD)
    )
    assert found.kind == LOGGER_WEB
    assert stick.state.requests == ["/inverter.cgi", "/moniter.cgi"]


def test_form_setup_and_identity_helpers() -> None:
    """The password rides the form. The unique id is the inverter serial."""
    request, error = parse_logger_form(
        {"logger_kind": LOGGER_WEB, "logger_host": "10.0.0.9", "logger_password": " secret "}
    )
    assert request is not None and not error
    assert request.password == "secret"
    probe = LoggerProbe(
        0,
        "web",
        "1031000000000001",
        "http",
        False,
        kind=LOGGER_WEB,
        port=80,
        function=0,
        stick_serial="7A00000000000001",
    )
    fields, unique = logger_setup(request, probe, mac="B8:D6:1A:00:00:01", source=SOURCE_LOCAL)
    assert unique == "web:1031000000000001"
    assert fields["logger_password"] == "secret"
    assert fields["logger_serial"] == "7A00000000000001"
    assert fields["logger_port"] == 80
    assert fields["logger_detect"] == "manual_solis_web"
    assert not needs_stable_id(probe, "", "")
    nameless = LoggerProbe(0, "web", "", "http", False, kind=LOGGER_WEB)
    assert needs_stable_id(nameless, "", "")
    assert not needs_stable_id(nameless, "", "MINE")
    assert logger_form_error(SolisLoggerBusy("x", reason=REASON_WEB_AUTH)) == "logger_web_password"
    assert logger_form_error(SolisLoggerBusy("x", reason=REASON_WEB_PAGE)) == "logger_web_page"
    assert detection_code("auto", LOGGER_WEB) == "auto_solis_web"
    assert "status page" in detection_why("auto_solis_web")
    assert "refreshes" in detection_why("manual_solis_web")
    spec = spec_for(LOGGER_WEB)
    assert (spec.default_port, spec.protocol, spec.unique_prefix) == (80, "http", "web")


async def test_both_mode_fills_what_the_page_lacks_from_the_cloud(
    stick: WebStickEmulator,
) -> None:
    """Page values win. Month, year, frequency, phases and strings come from SolisCloud."""
    cloud = _Cloud()
    local = _transport(stick)
    transport = SolisPriorityTransport("both", local, cloud)
    first = await transport.async_read()
    assert first.source == "local"
    inverter = first.inverters[0]
    assert (inverter.ac_power_w, inverter.temperature_c) == (382.0, 38.4)
    assert (inverter.energy_today_kwh, inverter.energy_total_kwh) == (2.8, 21986.0)
    assert inverter.data_source == "local"
    assert inverter.model == "Solis 4G"
    assert inverter.energy_month_kwh == 30.0
    assert inverter.frequency_hz == 49.99
    assert inverter.strings and inverter.phases
    assert inverter.serial == "1031000000000001"
    assert not local.needs_cloud_identity
    assert cloud.reads == 1
    stick.refresh(power=1792, temperature=39.7, today=2.9)
    again = await transport.async_read()
    assert again.inverters[0].ac_power_w == 1792.0
    assert again.inverters[0].energy_year_kwh == first.inverters[0].energy_year_kwh
    assert cloud.reads == 1
    assert transport.session_info()["mode"] == "both"
    assert transport.degraded_reason == ""
    transport._fill_due = 0.0
    await transport.async_read()
    assert cloud.reads == 2


async def test_cloud_fill_survives_a_cloud_error_and_drops_stale_live_fields(
    stick: WebStickEmulator,
) -> None:
    """A failed cloud read keeps the cache and retries soon. Old live fields are not shown."""

    class _Flaky(_Cloud):
        broken = False

        async def async_read(self) -> SolisSnapshot:
            if self.broken:
                raise SolisCloudError("cloud is down")
            return await super().async_read()

    cloud = _Flaky()
    transport = SolisPriorityTransport("both", _transport(stick), cloud)
    await transport.async_read()
    cloud.broken = True
    transport._fill_due = 0.0
    kept = await transport.async_read()
    assert kept.inverters[0].energy_month_kwh == 30.0
    assert 0 < transport._fill_due - time.monotonic() <= 60
    assert transport._fill is not None
    transport._fill = replace(transport._fill, fetched_at=datetime.now(UTC) - timedelta(minutes=30))
    old = await transport.async_read()
    assert old.inverters[0].energy_month_kwh == 30.0
    assert old.inverters[0].frequency_hz is None
    assert old.inverters[0].strings == () and old.inverters[0].phases == ()
    assert old.inverters[0].ac_power_w == 382.0


async def test_cloud_fill_starts_without_a_cloud_sample_and_skips_other_inverters(
    stick: WebStickEmulator,
) -> None:
    """A cloud down at startup leaves the page values alone, and another inverter is not used."""
    transport = SolisPriorityTransport("both", _transport(stick), _DownCloud())
    only = await transport.async_read()
    assert only.inverters[0].energy_month_kwh is None
    assert only.source == "local"
    local_only = SolisPriorityTransport("local", _transport(stick), None)
    assert (await local_only.async_read()).inverters[0].energy_month_kwh is None
    other = _Cloud()
    stranger = SolisPriorityTransport("both", _transport(stick, inverter_serial="OTHER"), other)
    assert (await stranger.async_read()).inverters[0].energy_month_kwh is None
    assert other.reads == 1
    assert match_inverter(_cloud_snapshot(), "") is not None
    assert match_inverter(_cloud_snapshot(), "other") is None
    web = await _transport(stick).async_read()
    assert fill_from_cloud(web, _cloud_snapshot(), serial="x", fresh=True) is web
    empty = replace(web, inverters=())
    assert fill_from_cloud(empty, _cloud_snapshot(), serial="1031000000000001", fresh=True) is empty


async def test_both_mode_falls_back_to_a_fresh_cloud_sample(stick: WebStickEmulator) -> None:
    """A wrong password is degraded, not asleep. A dark stick is asleep."""
    port = stick.port
    wrong = SolisWebTransport("127.0.0.1", "nope", port=port, timeout=2)
    transport = SolisPriorityTransport("both", wrong, _Cloud())
    degraded = await transport.async_read()
    assert degraded.source == "degraded"
    assert transport.degraded_reason == REASON_WEB_AUTH
    dark_stick = SolisWebTransport("127.0.0.1", "x", port=port, timeout=1)
    _no_route(dark_stick)
    dark = SolisPriorityTransport("both", dark_stick, _Cloud())
    assert (await dark.async_read()).source == "asleep"
    gone = SolisWebTransport("127.0.0.1", "x", port=port, timeout=1)
    _no_route(gone)
    only = SolisPriorityTransport("both", gone, _DownCloud())
    with pytest.raises(SolisLoggerOffline):
        await only.async_read()


async def test_flow_creates_a_web_entry_and_names_a_wrong_password(
    hass: HomeAssistant, stick: WebStickEmulator
) -> None:
    """Local flow: the inverter serial is the unique id and the password is stored."""
    result = await start_user(hass, "local")
    form = logger_form(logger_kind=LOGGER_WEB, logger_host="127.0.0.1", logger_port=stick.port)
    stick.state.password = "hunter2"
    asked = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    # The password field only appears once the stick refuses the factory password.
    assert asked["step_id"] == "logger_password"
    assert asked["errors"]["base"] == "logger_web_password"
    assert asked["description_placeholders"] == {"host": "127.0.0.1"}
    wrong = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"logger_password": "nope"}
    )
    assert wrong["step_id"] == "logger_password"
    assert wrong["errors"]["base"] == "logger_web_password"
    stick.state.password = "123456789"
    stick.state.mode = "garbage"
    page = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert page["errors"]["base"] == "logger_web_page"
    stick.state.mode = "ok"
    created = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].unique_id == "web:1031000000000001"
    data = created["result"].data
    assert data["logger_kind"] == LOGGER_WEB
    assert data["logger_password"] == "123456789"
    assert data["logger_serial"] == "7A00000000000001"
    assert data["logger_mac"] == "B8:D6:1A:00:00:01"
    assert data["inverter_serial"] == "1031000000000001"
    assert created["result"].options["scan_interval"] == 60


async def test_flow_asks_for_an_identity_when_the_page_has_no_serial(
    hass: HomeAssistant, stick: WebStickEmulator
) -> None:
    """A page without a serial and no typed serial has no stable id."""
    stick.state.serial = ""
    result = await start_user(hass, "local")
    failed = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        logger_form(
            logger_kind=LOGGER_WEB,
            logger_host="127.0.0.1",
            logger_port=str(stick.port),
        ),
    )
    assert failed["errors"]["base"] == "logger_identity"


async def test_entry_publishes_the_stick_and_hides_the_password(
    hass: HomeAssistant, stick: WebStickEmulator
) -> None:
    """A saved entry reads the page. Diagnostics keep the password out."""
    from custom_components.solis.diagnostics import async_get_config_entry_diagnostics

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_serial": "7A00000000000001",
            "logger_password": "123456789",
            "inverter_serial": "1031000000000001",
            "logger_detect": "manual_solis_web",
        },
        options=entry_options(),
        version=3,
        unique_id="web:1031000000000001",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    total = registry.async_get_entity_id("sensor", DOMAIN, "1031000000000001_energy_total")
    power = registry.async_get_entity_id("sensor", DOMAIN, "1031000000000001_ac_power")
    assert total is not None and power is not None
    assert float(hass.states.get(total).state) == 21986  # type: ignore[union-attr]
    assert float(hass.states.get(power).state) == 382  # type: ignore[union-attr]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert "123456789" not in str(diagnostics)
    assert diagnostics["logger"]["kind"] == LOGGER_WEB
    assert diagnostics["logger"]["detection"] == "manual_solis_web"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_wrong_password_repair_waits_ten_minutes(hass: HomeAssistant) -> None:
    """A rejected stick password asks for reauthentication at once and opens no repair."""

    class _Rejected:
        degraded_reason = REASON_WEB_AUTH

        async def async_read(self) -> SolisSnapshot:
            raise SolisLoggerAuthError("web password rejected")

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": SOURCE_LOCAL, "logger_kind": LOGGER_WEB, "logger_host": "127.0.0.1"},
        options={},
        version=3,
    )
    entry.add_to_hass(hass)
    coordinator = SolisCoordinator(hass, entry, _Rejected())
    with pytest.raises(ConfigEntryAuthFailed) as failure:
        await coordinator._async_update_data()
    assert failure.value.translation_key == "update_logger_web_auth"
    assert coordinator.auth_failed is True
    coordinator._degraded_since = time.monotonic() - 601
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert not any(
        issue_id.startswith("logger_web") for (_domain, issue_id) in ir.async_get(hass).issues
    )


async def test_reconfigure_keeps_the_saved_password_and_attaches_a_web_stick(
    hass: HomeAssistant, solis_server: tuple[Any, str], stick: WebStickEmulator
) -> None:
    """A cloud entry gains a web stick without a stick serial. A blank password is kept."""
    _state, url = solis_server
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
    stick.state.password = "hunter2"
    started = await entry.start_reconfigure_flow(hass)
    started = await pick(hass, started["flow_id"], "logger")
    base = logger_form(logger_kind=LOGGER_WEB, logger_host="127.0.0.1", logger_port=stick.port)
    asked = await hass.config_entries.flow.async_configure(started["flow_id"], base)
    assert asked["step_id"] == "logger_password"
    updated = await hass.config_entries.flow.async_configure(
        started["flow_id"], {"logger_password": "hunter2"}
    )
    assert updated["type"] is FlowResultType.ABORT
    assert updated["reason"] == "reconfigure_successful"
    assert entry.unique_id == "web:1031000000000001"
    assert entry.data["source"] == SOURCE_BOTH
    assert entry.data["logger_password"] == "hunter2"
    again = await entry.start_reconfigure_flow(hass)
    again = await pick(hass, again["flow_id"], "logger")
    kept = await hass.config_entries.flow.async_configure(again["flow_id"], base)
    assert kept["type"] is FlowResultType.ABORT
    assert entry.data["logger_password"] == "hunter2"
    await hass.async_block_till_done()


async def test_local_reconfigure_keeps_the_saved_password(
    hass: HomeAssistant, stick: WebStickEmulator
) -> None:
    """The local-only reconfigure menu lists the logger first and keeps the password."""
    stick.state.password = "hunter2"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": SOURCE_LOCAL,
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_password": "hunter2",
            "inverter_serial": "1031000000000001",
        },
        options=entry_options(),
        version=3,
        unique_id="web:1031000000000001",
    )
    entry.add_to_hass(hass)
    started = await entry.start_reconfigure_flow(hass)
    assert started["menu_options"][0] == "logger"
    started = await pick(hass, started["flow_id"], "logger")
    assert started["step_id"] == "logger"
    done = await hass.config_entries.flow.async_configure(
        started["flow_id"],
        logger_form(
            logger_kind=LOGGER_WEB,
            logger_host="127.0.0.1",
            logger_port=str(stick.port),
        ),
    )
    assert done["type"] is FlowResultType.ABORT
    assert entry.data["logger_password"] == "hunter2"
