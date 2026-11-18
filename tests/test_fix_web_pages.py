"""Web-page stick: other firmware layouts, other realms, a changed password, HTTP/1.1 replies."""

from __future__ import annotations

import pytest

from custom_components.solis.const import LOGGER_WEB, REASON_WEB_AUTH, REASON_WEB_PAGE
from custom_components.solis.errors import SolisLoggerAuthError, SolisLoggerBusy, SolisLoggerOffline
from custom_components.solis.local_transport import LoggerRequest
from custom_components.solis.loggers.web import (
    INVERTER_PATH,
    STATUS_PATH,
    WebStickClient,
    _complete,
    _dechunk,
    _split_response,
    is_basic_auth_challenge,
    parse_inverter,
    parse_status,
    parse_status_collector,
)
from custom_components.solis.web_transport import SolisWebTransport, probe_web
from mock_soliscloud.web_stick import LAYOUT_STATUS, WebStickEmulator

PASSWORD = "123456789"
# Quoted in the Home Assistant community thread: no serial, and "d" for the total.
COMMUNITY_PAGE = ";860041;205;20.1;790;0.300000;d;NO;"
STATUS_PAGE = """<html><head><script>
var webdata_sn = "1031000000000001      ";
var webdata_msvn = "";
var webdata_now_p = "472";
var webdata_today_e = "21.80";
var webdata_total_e = "21.8 kWh";
var webdata_alarm = "";
var cover_mid = "4000000001";
var cover_ver = "MW3_16U_5406_1.59";
var cover_sta_mac = "02:00:00:00:00:01";
</script></head><body><div id="webdata_now_p">---</div></body></html>"""


def _client(stick: WebStickEmulator, password: str = PASSWORD) -> WebStickClient:
    return WebStickClient("127.0.0.1", password, port=stick.port, timeout=2)


def test_inverter_page_needs_only_power() -> None:
    """A total of ``d`` drops the total, not the whole read."""
    page = parse_inverter("\x00" * 33 + COMMUNITY_PAGE + " " * 50)
    assert page.serial == ""
    assert (page.firmware, page.model_code) == ("860041", "205")
    assert (page.temperature_c, page.power_w, page.energy_today_kwh) == (20.1, 790.0, 0.3)
    assert page.energy_total_kwh is None
    assert not page.in_alarm
    assert page.page == INVERTER_PATH
    short = parse_inverter("S;F;M;x;12")
    assert (short.power_w, short.temperature_c, short.alarm) == (12.0, None, "")
    with pytest.raises(ValueError):
        parse_inverter(COMMUNITY_PAGE.replace("790", "d"))


def test_status_page_variables() -> None:
    """The IGEN-made page: units are stripped, blanks and dashes are no reading."""
    page = parse_status(STATUS_PAGE)
    assert page.serial == "1031000000000001"
    assert (page.power_w, page.energy_today_kwh, page.energy_total_kwh) == (472.0, 21.8, 21.8)
    assert page.temperature_c is None
    assert not page.in_alarm
    assert page.page == STATUS_PATH
    for night in ("", "---"):
        dark = parse_status(
            STATUS_PAGE.replace('"472"', f'"{night}"').replace('"21.80"', f'"{night}"')
        )
        assert (dark.power_w, dark.energy_today_kwh) == (None, None)
    assert parse_status(STATUS_PAGE.replace('webdata_alarm = ""', 'webdata_alarm = "F23"')).in_alarm
    assert parse_status(STATUS_PAGE.replace('"472"', '"1e99"')).power_w is None
    with pytest.raises(ValueError):
        parse_status('<html>var other = "1";</html>')
    collector = parse_status_collector(STATUS_PAGE)
    assert collector is not None
    assert (collector.serial, collector.version) == ("4000000001", "MW3_16U_5406_1.59")
    assert collector.mac == "02:00:00:00:00:01"
    assert parse_status_collector(STATUS_PAGE.replace('"4000000001"', '""')) is None


def test_any_basic_challenge_is_a_candidate() -> None:
    """The realm differs between sticks. The page read decides."""
    assert is_basic_auth_challenge(
        'HTTP/1.0 401 unauthorized\r\nWWW-authenticate: Basic realm="solis"'
    )
    assert is_basic_auth_challenge(
        'HTTP/1.1 401 Unauthorized\nWWW-Authenticate: Basic realm="USER LOGIN"'
    )
    assert not is_basic_auth_challenge(
        'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest realm="x"'
    )
    assert not is_basic_auth_challenge("HTTP/1.0 200 OK\r\nWWW-Authenticate: Basic\r\n\r\n")
    assert not is_basic_auth_challenge("")


def test_chunked_and_bare_lf_replies() -> None:
    """HTTP/1.1 chunked bodies and LF-only heads are read, and the read stops at the last chunk."""
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
    body = b"4\r\nabcd\r\n3;ext=1\r\nefg\r\n0\r\n\r\n"
    assert _split_response(head + body) == (200, "abcdefg")
    assert _complete(head + body)
    assert not _complete(head + body[:-7])
    assert not _complete(head + b"4\r\nab")
    assert _dechunk(b"zz\r\nabc") == (b"", False)
    assert _dechunk(b"3\r\nabc") == (b"abc", False)
    assert _dechunk(b"3\nabc\n0\n\n") == (b"abc", True)
    assert _split_response(b"HTTP/1.0 200 OK\nServer: x\n\nhello") == (200, "hello")
    assert _split_response(b"HTTP/1.0 404 Not Found") == (404, "")
    assert _complete(b"HTTP/1.0 200 OK\nContent-Length: 2\n\nok")


async def test_client_reads_the_d_total_and_a_chunked_reply(stick: WebStickEmulator) -> None:
    stick.state.total_text = "d"
    stick.state.chunked = True
    page = await _client(stick).read_inverter()
    assert page.power_w == 382.0
    assert page.energy_total_kwh is None
    assert page.energy_today_kwh == 2.8


async def test_status_page_fallback_is_remembered(stick: WebStickEmulator) -> None:
    """No inverter.cgi: read status.html, then go straight there."""
    stick.state.layout = LAYOUT_STATUS
    client = _client(stick)
    page = await client.read_inverter()
    assert page.power_w == 382.0
    assert page.energy_total_kwh == 21986.0
    assert page.serial == "1031000000000001"
    assert stick.state.requests == ["/inverter.cgi", "/status.html"]
    collector = await client.read_collector()
    assert collector is not None
    assert collector.serial == stick.state.mid
    await client.read_inverter()
    assert stick.state.requests[-1] == "/status.html"
    assert stick.state.requests.count("/inverter.cgi") == 1
    # The layout stops answering: the next read looks for both again.
    stick.state.mode = "garbage"
    with pytest.raises(SolisLoggerBusy) as garbled:
        await client.read_inverter()
    assert garbled.value.reason == REASON_WEB_PAGE
    assert client.page == ""
    stick.state.mode = "ok"
    fresh = _client(stick)
    fresh.page = STATUS_PATH
    assert (await fresh.read_collector()) is not None
    stick.state.mode = "error"
    fresh._status_body = ""
    assert await fresh.read_collector() is None


async def test_neither_page_is_a_page_fault(stick: WebStickEmulator) -> None:
    """Garbage on inverter.cgi and no status.html names both. A 500 is not a missing page."""
    stick.state.mode = "garbage"
    client = _client(stick)
    with pytest.raises(SolisLoggerBusy) as both:
        await client.read_inverter()
    assert both.value.reason == REASON_WEB_PAGE
    assert "inverter.cgi" in str(both.value)
    assert "status.html answered with status 404" in str(both.value)
    stick.state.mode = "error"
    before = len(stick.state.requests)
    with pytest.raises(SolisLoggerBusy):
        await client.read_inverter()
    assert stick.state.requests[before:] == ["/inverter.cgi"]
    # Once inverter.cgi is known, a later garbled reply does not try status.html.
    stick.state.mode = "ok"
    await client.read_inverter()
    stick.state.mode = "garbage"
    before = len(stick.state.requests)
    with pytest.raises(SolisLoggerBusy):
        await client.read_inverter()
    assert stick.state.requests[before:] == ["/inverter.cgi"]


async def test_collector_page_errors(stick: WebStickEmulator) -> None:
    """A wrong password on moniter.cgi is still an auth error; a missing page is not."""
    with pytest.raises(SolisLoggerAuthError):
        await _client(stick, "nope").read_collector()
    stick.state.mode = "error"
    assert await _client(stick).read_collector() is None
    stick.state.mode = "nopage"
    assert await _client(stick).read_collector() is None


async def test_night_on_the_status_page(stick: WebStickEmulator) -> None:
    """Empty or dashed values: the inverter is silent. Totals from the last read stay."""
    stick.state.layout = LAYOUT_STATUS
    stick.state.night_value = "---"
    transport = SolisWebTransport("127.0.0.1", PASSWORD, port=stick.port, timeout=2)
    first = (await transport.async_read()).inverters[0]
    assert (first.state, first.ac_power_w, first.energy_total_kwh) == (2, 0.0, None)
    stick.state.night_value = None
    day = await transport.async_read()
    assert day.inverters[0].ac_power_w == 382.0
    stick.state.night_value = ""
    night = await transport.async_read()
    assert night.source == "asleep"
    assert night.inverters[0].ac_power_w == 0.0
    assert night.inverters[0].energy_total_kwh == 21986.0
    assert transport.degraded_reason == ""


async def test_password_changed_to_the_wifi_password(stick: WebStickEmulator) -> None:
    """The stick joins Wi-Fi and the stored password stops working: a typed auth error."""
    transport = SolisWebTransport("127.0.0.1", PASSWORD, port=stick.port, timeout=2)
    assert (await transport.async_read()).source == "local"
    stick.join_wifi("wifi-secret")
    with pytest.raises(SolisLoggerAuthError) as first:
        await transport.async_read()
    assert first.value.reason == REASON_WEB_AUTH
    assert not first.value.waiting
    with pytest.raises(SolisLoggerAuthError) as waiting:
        await transport.async_read()
    assert waiting.value.waiting
    assert isinstance(waiting.value, SolisLoggerBusy)
    renewed = SolisWebTransport("127.0.0.1", "wifi-secret", port=stick.port, timeout=2)
    assert (await renewed.async_read()).source == "local"


async def test_auto_probe_accepts_any_realm_but_confirms_the_page(stick: WebStickEmulator) -> None:
    """Another realm is read and confirmed. Unconfirmed, it is not claimed as a web stick."""
    stick.state.realm = "USER LOGIN"
    stick.state.layout = LAYOUT_STATUS
    request = LoggerRequest("127.0.0.1", port=stick.port, password=PASSWORD, timeout=2)
    probe = await probe_web(request, auto=True)
    assert probe.kind == LOGGER_WEB
    assert probe.inverter_serial == "1031000000000001"
    assert probe.stick_serial == stick.state.mid
    assert probe.mac == stick.state.mac
    wrong = LoggerRequest("127.0.0.1", port=stick.port, password="nope", timeout=2)
    with pytest.raises(SolisLoggerOffline) as unconfirmed:
        await probe_web(wrong, auto=True)
    assert unconfirmed.value.refused
    with pytest.raises(SolisLoggerAuthError):
        await probe_web(wrong, auto=False)
    stick.state.realm = "solis"
    with pytest.raises(SolisLoggerAuthError):
        await probe_web(wrong, auto=True)
