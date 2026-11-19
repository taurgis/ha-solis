"""Config-flow UX: menus, Advanced sections, discovery, timezone choices and reauth."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_DHCP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import entry_data, entry_options
from .flow_helpers import logger_form, pick, start_user
from custom_components.solis import config_flow as flow
from custom_components.solis.const import DOMAIN, LOGGER_WEB
from custom_components.solis.errors import SolisClockError
from custom_components.solis.local_transport import LoggerProbe, LoggerRequest
from custom_components.solis.solarman import Stick
from mock_soliscloud.catalog import KEY_ID, SECRET
from mock_soliscloud.stick import SERIAL, StickEmulator, StickState
from mock_soliscloud.web_stick import WebStickEmulator

_STATIONS = ["1900000000000001001", "1900000000000001002"]


@pytest.fixture
def no_setup() -> Iterator[AsyncMock]:
    """Reloads after a flow do not start the real coordinator."""
    with patch("custom_components.solis.async_setup_entry", return_value=True) as setup:
        yield setup


def _suggested(schema: Any) -> dict[str, Any]:
    """Suggested values of a form, with the Advanced section nested."""
    found: dict[str, Any] = {}
    for key, value in schema.schema.items():
        if isinstance(value, flow.section):
            found[str(key)] = _suggested(value.schema)
        elif key.description:
            found[str(key)] = key.description["suggested_value"]
    return found


def _probe(kind: str = "solarman") -> LoggerProbe:
    return LoggerProbe(
        slave=1,
        register_map="string",
        inverter_serial="1031000000000001",
        protocol="v5",
        serial_unsupported=False,
        kind=kind,
        port=8899,
    )


async def test_options_store_auto_plant_and_a_named_zone(
    hass: HomeAssistant, solis_server: tuple[Any, str], no_setup: AsyncMock
) -> None:
    """``auto`` is stored as the empty preference older entries use; plant and IANA stay."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data(url),
        options={**entry_options(), "time_zone": ""},
        unique_id=KEY_ID,
        version=3,
    )
    entry.add_to_hass(hass)
    for chosen, stored in (("plant", "plant"), ("Europe/Brussels", "Europe/Brussels")):
        started = await hass.config_entries.options.async_init(entry.entry_id)
        default = next(
            key.default() for key in started["data_schema"].schema if str(key) == "time_zone"
        )
        assert default == ("auto" if entry.options["time_zone"] == "" else "plant")
        done = await hass.config_entries.options.async_configure(
            started["flow_id"], {"scan_interval": 300, "time_zone": chosen}
        )
        assert done["type"] is FlowResultType.CREATE_ENTRY
        # Each save schedules a reload; let it finish before the next flow.
        await hass.async_block_till_done()
        assert entry.options["time_zone"] == stored
        # The Advanced section was not opened, so the saved knobs stay.
        assert entry.options["request_timeout"] == entry_options()["request_timeout"]
    started = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        started["flow_id"], {"scan_interval": 300, "time_zone": "auto"}
    )
    assert entry.options["time_zone"] == ""
    await hass.async_block_till_done()
    assert no_setup.call_count == 3


async def test_a_local_entry_can_add_soliscloud_later(
    hass: HomeAssistant, solis_server: tuple[Any, str], no_setup: AsyncMock
) -> None:
    """Reconfigure on a local entry adds the account, then the plants, as a fallback."""
    _state, url = solis_server
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "local", "logger_host": "127.0.0.1", "logger_serial": "1"},
        options=entry_options(),
        unique_id="1",
        version=3,
    )
    entry.add_to_hass(hass)
    menu = await entry.start_reconfigure_flow(hass)
    assert menu["menu_options"] == ["logger", "reconfigure_cloud"]
    form = await pick(hass, menu["flow_id"], "reconfigure_cloud")
    empty = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"api_url": url, "key_id": KEY_ID}
    )
    assert empty["errors"]["base"] == "key_required"
    plants = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
    )
    assert plants["step_id"] == "stations"
    done = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"station_ids": [_STATIONS[0]]}
    )
    assert done["reason"] == "reconfigure_successful"
    assert entry.data["source"] == "both"
    assert entry.data["key_id"] == KEY_ID
    assert entry.data["station_ids"] == [_STATIONS[0]]
    assert entry.unique_id == "1"


async def test_reconfigure_cannot_take_a_key_another_entry_reads(
    hass: HomeAssistant, solis_server: tuple[Any, str], no_setup: AsyncMock
) -> None:
    """A second cloud entry may not switch to the account the first one reads."""
    _state, url = solis_server
    first = MockConfigEntry(domain=DOMAIN, data=entry_data(url), unique_id=KEY_ID, version=3)
    first.add_to_hass(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={**entry_data(url), "key_id": "other-key"},
        unique_id="other-key",
        version=3,
    )
    second.add_to_hass(hass)
    menu = await second.start_reconfigure_flow(hass)
    form = await pick(hass, menu["flow_id"], "reconfigure_cloud")
    taken = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"api_url": url, "key_id": KEY_ID, "key_secret": SECRET}
    )
    assert taken["type"] is FlowResultType.ABORT
    assert taken["reason"] == "already_configured"
    assert second.data["key_id"] == "other-key"
    # The entry that owns the key may keep it: it does not conflict with itself.
    menu = await first.start_reconfigure_flow(hass)
    form = await pick(hass, menu["flow_id"], "reconfigure_cloud")
    kept = await hass.config_entries.flow.async_configure(
        form["flow_id"], {"api_url": url, "key_id": KEY_ID}
    )
    assert kept["reason"] == "reconfigure_successful"


async def test_a_wrong_clock_is_its_own_error(hass: HomeAssistant) -> None:
    """SolisCloud refusing the request time is not reported as a bad key."""
    form = await start_user(hass, "cloud")
    with patch(
        "custom_components.solis.config_flow.SolisCloudClient.user_station_list",
        new=AsyncMock(side_effect=SolisClockError("skew")),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"api_url": "https://example.test", "key_id": "k", "key_secret": "s"},
        )
    assert result["errors"]["base"] == "clock_skew"


async def test_web_stick_reauth_asks_for_the_stick_password(
    hass: HomeAssistant, stick: WebStickEmulator, no_setup: AsyncMock
) -> None:
    """A local web-page stick reauthenticates with its password, not a SolisCloud key."""
    stick.state.password = "hunter2"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": "local",
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_password": "123456789",
            "inverter_serial": "1031000000000001",
        },
        options=entry_options(),
        unique_id="web:1031000000000001",
        version=3,
    )
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    assert started["step_id"] == "reauth_web"
    assert started["description_placeholders"]["host"] == "127.0.0.1"
    wrong = await hass.config_entries.flow.async_configure(started["flow_id"], {})
    assert wrong["errors"]["base"] == "logger_web_password"
    done = await hass.config_entries.flow.async_configure(
        started["flow_id"], {"logger_password": "hunter2"}
    )
    assert done["reason"] == "reauth_successful"
    assert entry.data["logger_password"] == "hunter2"


async def test_web_stick_with_cloud_reauth_offers_both_logins(
    hass: HomeAssistant, stick: WebStickEmulator, no_setup: AsyncMock
) -> None:
    """With a SolisCloud fallback either login can be the one that was refused."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **entry_data("http://127.0.0.1:9"),
            "source": "both",
            "logger_kind": LOGGER_WEB,
            "logger_host": "127.0.0.1",
            "logger_port": stick.port,
            "logger_password": "old",
            "inverter_serial": "1031000000000001",
        },
        options=entry_options(),
        unique_id="web:1031000000000001",
        version=3,
    )
    entry.add_to_hass(hass)
    started = await entry.start_reauth_flow(hass)
    assert started["type"] is FlowResultType.MENU
    assert started["menu_options"] == ["reauth_web", "reauth_confirm"]
    form = await pick(hass, started["flow_id"], "reauth_web")
    done = await hass.config_entries.flow.async_configure(form["flow_id"], {})
    assert done["reason"] == "reauth_successful"
    assert entry.data["logger_password"] == "123456789"


async def test_logger_form_lists_every_stick_and_fills_the_serial(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, no_setup: AsyncMock
) -> None:
    """Several sticks are a dropdown. A typed address is asked for its serial directly."""
    calls: list[tuple[str, ...]] = []
    sticks = [Stick("10.0.0.5", "e8:fd:f8:00:00:05", 5), Stick("10.0.0.6", "bc:54:f9:00:00:06", 6)]

    async def _discover(*, timeout: float, extra_targets: tuple[str, ...] = ()) -> list[Stick]:
        calls.append(extra_targets)
        if not extra_targets:
            return sticks
        return [Stick("10.0.0.9", "e8:fd:f8:00:00:09", 9)]

    seen: list[LoggerRequest] = []

    async def _identify(request: LoggerRequest) -> LoggerProbe:
        seen.append(request)
        return _probe()

    monkeypatch.setattr(flow, "discover_sticks", _discover)
    monkeypatch.setattr(flow, "identify_logger", _identify)
    form = await start_user(hass, "local")
    host = next(value for key, value in form["data_schema"].schema.items() if key == "logger_host")
    labels = [option["label"] for option in host.config["options"]]
    assert labels == ["10.0.0.5 (5)", "10.0.0.6 (6)"]
    assert host.config["custom_value"] is True
    assert _suggested(form["data_schema"])["logger_host"] == "10.0.0.5"
    assert form["description_placeholders"]["docs_url"].endswith("troubleshooting.md")
    created = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        logger_form(logger_kind="solarman", logger_host="10.0.0.9", slave_id=2.0),
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert calls == [(), ("10.0.0.9",)]
    assert seen[0].logger_serial == "9" and seen[0].slave == 2
    assert created["result"].unique_id == "9"
    assert created["result"].data["logger_mac"] == "e8:fd:f8:00:00:09"


async def test_a_typed_host_that_another_stick_answers_for_is_not_trusted(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery reply for a different address does not supply the serial."""

    async def _discover(*, timeout: float, extra_targets: tuple[str, ...] = ()) -> list[Stick]:
        return [Stick("10.0.0.99", "e8:fd:f8:00:00:99", 99)] if extra_targets else []

    monkeypatch.setattr(flow, "discover_sticks", _discover)
    form = await start_user(hass, "local")
    failed = await hass.config_entries.flow.async_configure(
        form["flow_id"], logger_form(logger_kind="solarman", logger_host="10.0.0.9")
    )
    assert failed["errors"]["base"] == "invalid_logger"
    # No stick answered the broadcast, so the address stays a plain text field.
    host = next(
        value for key, value in failed["data_schema"].schema.items() if key == "logger_host"
    )
    assert host is str


async def test_reconfigure_logger_prefills_advanced_and_maps_the_old_preset(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, no_setup: AsyncMock
) -> None:
    """Saved port, slave and serial sit in Advanced. The removed "default" preset reads as Elfin."""

    async def _discover(*, timeout: float, extra_targets: tuple[str, ...] = ()) -> list[Stick]:
        return [Stick("10.0.0.5", "e8:fd:f8:00:00:05", 5)]

    async def _identify(request: LoggerRequest) -> LoggerProbe:
        return _probe("rtu_tcp")

    monkeypatch.setattr(flow, "discover_sticks", _discover)
    monkeypatch.setattr(flow, "identify_logger", _identify)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "source": "local",
            "logger_kind": "rtu_tcp",
            "logger_host": "10.0.0.40",
            "logger_port": 8899,
            "slave_id": 2,
            "logger_serial": "gw-7",
            "logger_preset": "default",
        },
        options=entry_options(),
        unique_id="rtu:gw-7",
        version=3,
    )
    entry.add_to_hass(hass)
    menu = await entry.start_reconfigure_flow(hass)
    form = await pick(hass, menu["flow_id"], "logger")
    suggested = _suggested(form["data_schema"])
    assert suggested["logger_kind"] == "rtu_tcp"
    assert suggested["logger_host"] == "10.0.0.40"
    assert suggested["advanced"] == {
        "logger_serial": "gw-7",
        "logger_port": 8899,
        "slave_id": 2,
        "logger_preset": "elfin",
    }
    host = next(value for key, value in form["data_schema"].schema.items() if key == "logger_host")
    assert [option["value"] for option in host.config["options"]] == ["10.0.0.40", "10.0.0.5"]
    done = await hass.config_entries.flow.async_configure(
        form["flow_id"],
        logger_form(
            logger_kind="rtu_tcp",
            logger_host="10.0.0.41",
            logger_serial="gw-7",
            logger_preset="waveshare",
        ),
    )
    assert done["reason"] == "reconfigure_successful"
    assert entry.data["logger_host"] == "10.0.0.41"
    assert entry.data["logger_preset"] == "waveshare"


async def test_dhcp_offers_a_new_solarman_stick(
    hass: HomeAssistant, socket_enabled: None, no_setup: AsyncMock
) -> None:
    """The stick's own discovery reply names its serial; setup continues prefilled."""
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=48899))
    await emulator.start()
    assert emulator._tcp is not None
    port = emulator._tcp.sockets[0].getsockname()[1]
    try:
        found = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data={"ip": "127.0.0.1", "macaddress": "e8fdf8f57b5c", "hostname": "IGEN"},
        )
        assert found["type"] is FlowResultType.MENU
        assert found["step_id"] == "discovery_confirm"
        assert found["menu_options"] == ["local", "both"]
        assert found["description_placeholders"] == {"host": "127.0.0.1", "serial": str(SERIAL)}
        progress = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert progress[0]["context"]["title_placeholders"] == {"name": f"Solarman {SERIAL}"}
        form = await pick(hass, found["flow_id"], "local")
        suggested = _suggested(form["data_schema"])
        assert suggested["logger_kind"] == "solarman"
        assert suggested["logger_host"] == "127.0.0.1"
        created = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            logger_form(logger_kind="solarman", logger_host="127.0.0.1", logger_port=port),
        )
    finally:
        await emulator.close()
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["result"].unique_id == str(SERIAL)
    assert created["result"].data["source"] == "local"
    assert created["result"].data["logger_mac"] == "bc:54:f9:f5:7b:5c"


async def test_dhcp_moves_a_known_stick_found_again_by_serial(
    hass: HomeAssistant, socket_enabled: None, no_setup: AsyncMock
) -> None:
    """An entry without a saved MAC is matched by the serial in the discovery reply."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "local", "logger_host": "10.0.0.8", "logger_serial": str(SERIAL)},
        unique_id=str(SERIAL),
        version=3,
    )
    entry.add_to_hass(hass)
    emulator = StickEmulator(StickState(tcp_port=0, udp_port=48899))
    await emulator.start()
    try:
        moved = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data={"ip": "127.0.0.1", "macaddress": "bc54f9f57b5c", "hostname": ""},
        )
    finally:
        await emulator.close()
    assert moved["reason"] == "already_configured"
    assert entry.data["logger_host"] == "127.0.0.1"


async def test_dhcp_adopts_the_mac_of_a_stick_at_a_saved_address(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, no_setup: AsyncMock
) -> None:
    """A Solarman MAC at an entry's address is stored once, so the next lease can move it."""

    async def _never(**_kwargs: object) -> list[Stick]:
        raise AssertionError("a configured address needs no discovery")

    monkeypatch.setattr(flow, "discover_sticks", _never)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"source": "local", "logger_host": "10.0.0.20", "logger_serial": "20"},
        unique_id="20",
        version=3,
    )
    entry.add_to_hass(hass)
    adopted = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.0.0.20", "macaddress": "e8:fd:f8:00:00:20", "hostname": ""},
    )
    assert adopted["reason"] == "already_configured"
    assert entry.data["logger_mac"] == "e8:fd:f8:00:00:20"
    other = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_DHCP},
        data={"ip": "10.0.0.20", "macaddress": "e8:fd:f8:00:00:21", "hostname": ""},
    )
    assert other["reason"] == "already_configured"
    assert entry.data["logger_mac"] == "e8:fd:f8:00:00:20"
