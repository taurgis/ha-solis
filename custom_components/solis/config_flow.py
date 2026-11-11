"""Config flow for Solis Cloud.

Authentication is the API key id and secret only. SolisCloud no longer needs
the portal username for reads, which is the failure hultenvp/solis-sensor
issue 517 describes.
"""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import logging
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolisCloudClient
from .const import (
    CONF_API_URL,
    CONF_ENDPOINT_INTERVAL,
    CONF_INVERTER_SERIAL,
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    CONF_LEGACY_NAME,
    CONF_LOGGER_HOST,
    CONF_LOGGER_KIND,
    CONF_LOGGER_MAC,
    CONF_LOGGER_PORT,
    CONF_LOGGER_SERIAL,
    CONF_REQUEST_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONF_SLAVE_ID,
    CONF_SOURCE,
    CONF_STATION_IDS,
    CONF_TIME_ZONE,
    DEFAULT_API_URL,
    DEFAULT_ENDPOINT_INTERVAL,
    DEFAULT_LOCAL_SCAN_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER_AUTO,
    LOGGER_SOLARMAN,
    MIN_LOCAL_SCAN_INTERVAL,
    SOLARMAN_OUI,
    SOURCE_BOTH,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisLoggerBusy,
    SolisLoggerOffline,
    SolisTimeoutError,
)
from .local_transport import (
    identify_logger,
    logger_form_error,
    logger_setup,
    parse_logger_form,
    s2_needs_identity,
)
from .loggers import SELECTABLE_KINDS, kind_label
from .solarman import Stick, discover_sticks, format_mac

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SOURCE, default=SOURCE_CLOUD): vol.In(
            [SOURCE_CLOUD, SOURCE_LOCAL, SOURCE_BOTH]
        ),
        vol.Required(CONF_API_URL, default=DEFAULT_API_URL): str,
        vol.Optional(CONF_KEY_ID, default=""): str,
        vol.Optional(CONF_KEY_SECRET, default=""): str,
    }
)


def _schema(schema: vol.Schema) -> Any:
    """Adapt a voluptuous schema to the config-flow type of this Home Assistant."""
    return cast(Any, schema)


def _normalize_url(url: str) -> str:
    """Return a URL, upgrading public http hosts to https.

    Loopback, private addresses and single-label names (the lab's ``mock``
    host) may stay on http. SolisCloud itself is TLS-only.
    """
    text = url.strip().rstrip("/")
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    parts = urlsplit(text)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() == "http" and host and not _http_allowed(host):
        text = urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
    return text.rstrip("/")


def _http_allowed(host: str) -> bool:
    if host in {"localhost", "mock"} or "." not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def clean_time_zone(value: object) -> tuple[str, str | None]:
    """Return a stored timezone preference and a form error key, if any."""
    text = str(value or "").strip()
    if text in {"", "plant"}:
        return text, None
    try:
        ZoneInfo(text)
    except ZoneInfoNotFoundError:
        return text, "invalid_timezone"
    return text, None


class SolisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Solis config flow."""

    VERSION = 3

    def __init__(self) -> None:
        """Prepare discovered-station state."""
        self._discovered: dict[str, str] = {}
        self._credentials: dict[str, Any] = {}
        self._source = SOURCE_CLOUD
        self._sticks: list[Stick] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolisOptionsFlow:
        """Return the options flow."""
        return SolisOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the API URL and key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._source = str(user_input.get(CONF_SOURCE) or SOURCE_CLOUD)
            if self._source == SOURCE_LOCAL:
                return await self.async_step_logger()
            if not str(user_input.get(CONF_KEY_ID) or "").strip():
                errors["base"] = "invalid_auth"
            else:
                user_input = {
                    **user_input,
                    CONF_API_URL: _normalize_url(
                        str(user_input.get(CONF_API_URL) or DEFAULT_API_URL)
                    ),
                    CONF_KEY_ID: str(user_input[CONF_KEY_ID]).strip(),
                    CONF_KEY_SECRET: str(user_input.get(CONF_KEY_SECRET) or "").strip(),
                    CONF_SOURCE: self._source,
                }
                errors = await self._load_stations(user_input)
                if not errors:
                    self._credentials = user_input
                    return await self.async_step_stations()
        return self.async_show_form(
            step_id="user", data_schema=_schema(_USER_SCHEMA), errors=errors
        )

    async def async_step_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which plants this entry should follow."""
        schema = vol.Schema(
            {
                vol.Required(CONF_STATION_IDS, default=list(self._discovered)): cv.multi_select(
                    self._discovered
                )
            }
        )
        if user_input is not None:
            station_ids = [str(station_id) for station_id in user_input[CONF_STATION_IDS]]
            if not station_ids:
                return self.async_show_form(
                    step_id="stations",
                    data_schema=_schema(schema),
                    errors={"base": "no_stations"},
                )
            title = self._title(station_ids)
            data = {
                CONF_API_URL: self._credentials[CONF_API_URL],
                CONF_KEY_ID: self._credentials[CONF_KEY_ID],
                CONF_KEY_SECRET: self._credentials[CONF_KEY_SECRET],
                CONF_STATION_IDS: station_ids,
                CONF_SOURCE: self._source,
            }
            if self._source == SOURCE_BOTH:
                self._credentials = data
                return await self.async_step_logger()
            await self.async_set_unique_id(self._credentials[CONF_KEY_ID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=title,
                data=data,
                options={
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    CONF_REQUEST_TIMEOUT: DEFAULT_REQUEST_TIMEOUT,
                    CONF_ENDPOINT_INTERVAL: DEFAULT_ENDPOINT_INTERVAL,
                    CONF_TIME_ZONE: "",
                },
            )
        return self.async_show_form(step_id="stations", data_schema=_schema(schema))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start a credential refresh."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the API key id and secret."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {
                CONF_API_URL: entry.data[CONF_API_URL],
                CONF_KEY_ID: str(user_input[CONF_KEY_ID]).strip(),
                CONF_KEY_SECRET: str(user_input[CONF_KEY_SECRET]).strip(),
            }
            errors = await self._load_stations(candidate)
            if not errors:
                await self.async_set_unique_id(candidate[CONF_KEY_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_KEY_ID: candidate[CONF_KEY_ID],
                        CONF_KEY_SECRET: candidate[CONF_KEY_SECRET],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_KEY_ID, default=entry.data.get(CONF_KEY_ID, "")): str,
                vol.Required(CONF_KEY_SECRET): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_schema(schema), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the URL, station selection and polling knobs."""
        entry = self._get_reconfigure_entry()
        if user_input is None and str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD)) == SOURCE_LOCAL:
            self._source = SOURCE_LOCAL
            self._credentials = dict(entry.data)
            return await self.async_step_logger()
        errors: dict[str, str] = {}
        if not self._discovered:
            errors = await self._load_stations(dict(entry.data))
            if errors:
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            url = _normalize_url(str(user_input[CONF_API_URL]))
            candidate = {**dict(entry.data), CONF_API_URL: url}
            errors = await self._load_stations(candidate)
            station_ids = [str(station_id) for station_id in user_input[CONF_STATION_IDS]]
            zone, zone_error = clean_time_zone(
                user_input.get(CONF_TIME_ZONE, entry.options.get(CONF_TIME_ZONE, ""))
            )
            if zone_error:
                errors = {CONF_TIME_ZONE: zone_error}
            elif not errors and station_ids:
                data_updates: dict[str, Any] = {
                    CONF_API_URL: url,
                    CONF_STATION_IDS: station_ids,
                }
                unique_id: str | None = None
                host = str(user_input.get(CONF_LOGGER_HOST) or "").strip()
                serial_text = str(user_input.get(CONF_LOGGER_SERIAL) or "").strip()
                if host and serial_text:
                    fields, unique, failure = await self._logger_updates(
                        user_input, source=SOURCE_BOTH
                    )
                    if failure:
                        errors["base"] = failure
                    else:
                        data_updates.update(fields)
                        unique_id = unique
                if not errors:
                    options = {
                        **entry.options,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_REQUEST_TIMEOUT: int(user_input[CONF_REQUEST_TIMEOUT]),
                        CONF_ENDPOINT_INTERVAL: float(user_input[CONF_ENDPOINT_INTERVAL]),
                        CONF_TIME_ZONE: zone,
                    }
                    if unique_id is None:
                        return self.async_update_reload_and_abort(
                            entry,
                            data_updates=data_updates,
                            options=options,
                        )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data_updates,
                        unique_id=unique_id,
                        options=options,
                    )
            elif not station_ids and not errors:
                errors = {"base": "no_stations"}
        schema = self._reconfigure_schema(entry)
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(schema), errors=errors
        )

    def _reconfigure_schema(self, entry: ConfigEntry) -> vol.Schema:
        options = entry.options
        return vol.Schema(
            {
                vol.Required(
                    CONF_API_URL, default=entry.data.get(CONF_API_URL, DEFAULT_API_URL)
                ): str,
                vol.Required(
                    CONF_STATION_IDS,
                    default=list(entry.data.get(CONF_STATION_IDS, [])),
                ): cv.multi_select(self._discovered),
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_LOCAL_SCAN_INTERVAL, max=3600)),
                vol.Required(
                    CONF_REQUEST_TIMEOUT,
                    default=options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                vol.Required(
                    CONF_ENDPOINT_INTERVAL,
                    default=options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=30)),
                vol.Optional(
                    CONF_TIME_ZONE,
                    default=options.get(CONF_TIME_ZONE, ""),
                ): str,
                vol.Optional(
                    CONF_SOURCE, default=str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
                ): vol.In([SOURCE_CLOUD, SOURCE_LOCAL, SOURCE_BOTH]),
                vol.Optional(
                    CONF_LOGGER_HOST, default=str(entry.data.get(CONF_LOGGER_HOST, ""))
                ): str,
                vol.Optional(
                    CONF_LOGGER_SERIAL, default=str(entry.data.get(CONF_LOGGER_SERIAL, ""))
                ): str,
                vol.Optional(
                    CONF_INVERTER_SERIAL, default=str(entry.data.get(CONF_INVERTER_SERIAL, ""))
                ): str,
                vol.Optional(
                    CONF_LOGGER_PORT, default=str(entry.data.get(CONF_LOGGER_PORT) or "")
                ): str,
                **_logger_kind_field(
                    str(entry.data.get(CONF_LOGGER_KIND) or LOGGER_SOLARMAN),
                    required=False,
                ),
                vol.Optional(CONF_SLAVE_ID, default=str(entry.data.get(CONF_SLAVE_ID) or "")): str,
            }
        )

    async def _load_stations(self, data: Mapping[str, Any]) -> dict[str, str]:
        """Fill ``_discovered`` or return a form error."""
        try:
            stations = await _fetch_stations(self.hass, data)
        except SolisAuthError:
            return {"base": "invalid_auth"}
        except SolisClockError:
            return {"base": "clock_skew"}
        except SolisTimeoutError:
            return {"base": "timeout"}
        except SolisCloudError as err:
            _LOGGER.debug("Solis discovery failed: %s", err)
            return {"base": "cannot_connect"}
        if not stations:
            return {"base": "no_stations"}
        self._discovered = stations
        return {}

    def _logger_defaults(self, found: Stick | None) -> dict[str, str]:
        """Prefill a reconfigure. A new entry still starts from discovery."""
        entry = self._get_reconfigure_entry() if self.source == "reconfigure" else None
        saved = entry.data if entry is not None else {}
        if entry is None:
            return {
                "kind": LOGGER_AUTO,
                "host": "" if found is None else found.host,
                "port": "",
                "serial": "" if found is None else str(found.serial),
                "slave": "",
                "inverter": "",
            }
        return {
            "kind": str(saved.get(CONF_LOGGER_KIND) or LOGGER_AUTO),
            "host": str(saved.get(CONF_LOGGER_HOST) or ""),
            "port": str(saved.get(CONF_LOGGER_PORT) or ""),
            "serial": str(saved.get(CONF_LOGGER_SERIAL) or ""),
            "slave": str(saved.get(CONF_SLAVE_ID) or ""),
            "inverter": str(saved.get(CONF_INVERTER_SERIAL) or ""),
        }

    async def async_step_logger(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm a local logger. The inverter has to be producing."""
        errors: dict[str, str] = {}
        if user_input is None:
            self._sticks = await discover_sticks(timeout=1.2)
        else:
            fields, unique, failure = await self._logger_updates(user_input, source=self._source)
            if failure:
                errors["base"] = failure
            else:
                return await self._finish_logger(fields, unique)
        found = self._sticks[0] if self._sticks else None
        defaults = self._logger_defaults(found)
        schema = vol.Schema(
            {
                **_logger_kind_field(defaults["kind"], required=True),
                vol.Required(CONF_LOGGER_HOST, default=defaults["host"]): str,
                vol.Optional(CONF_LOGGER_PORT, default=defaults["port"]): str,
                vol.Optional(CONF_LOGGER_SERIAL, default=defaults["serial"]): str,
                vol.Optional(CONF_SLAVE_ID, default=defaults["slave"]): str,
                vol.Optional(CONF_INVERTER_SERIAL, default=defaults["inverter"]): str,
            }
        )
        return self.async_show_form(
            step_id="logger",
            data_schema=_schema(schema),
            errors=errors,
            description_placeholders={
                "found": ", ".join(f"{stick.host} ({stick.serial})" for stick in self._sticks)
                or "none",
            },
        )

    async def _logger_updates(
        self, user_input: Mapping[str, Any], *, source: str
    ) -> tuple[dict[str, Any], str, str]:
        """Probe the form and return entry fields, unique id, and an error key."""
        request, error = parse_logger_form(user_input)
        if error or request is None:
            return {}, "", error or "invalid_logger"
        try:
            probe = await identify_logger(request)
        except (SolisLoggerBusy, SolisLoggerOffline, ValueError, TimeoutError) as err:
            return {}, "", logger_form_error(err)
        if s2_needs_identity(probe, request.logger_serial, request.inverter_serial):
            return {}, "", "logger_identity"
        mac = _mac_for(self._sticks, request.logger_serial, probe.kind)
        fields, unique = logger_setup(request, probe, mac=mac, source=source)
        return fields, unique, ""

    async def _finish_logger(self, logger_data: dict[str, Any], unique: str) -> ConfigFlowResult:
        await self.async_set_unique_id(unique)
        if self.source == "reconfigure":
            entry = self._get_reconfigure_entry()
            base = dict(entry.data)
            base.update(logger_data)
            return self.async_update_reload_and_abort(
                entry,
                data=base,
                unique_id=unique,
            )
        self._abort_if_unique_id_configured(
            updates={CONF_LOGGER_HOST: logger_data[CONF_LOGGER_HOST]}
        )
        data = {**self._credentials, **logger_data}
        title = data.get(CONF_INVERTER_SERIAL) or f"Solis {unique}"
        return self.async_create_entry(
            title=str(title),
            data=data,
            options={
                CONF_SCAN_INTERVAL: DEFAULT_LOCAL_SCAN_INTERVAL,
                CONF_REQUEST_TIMEOUT: DEFAULT_REQUEST_TIMEOUT,
                CONF_ENDPOINT_INTERVAL: DEFAULT_ENDPOINT_INTERVAL,
                CONF_TIME_ZONE: "",
            },
        )

    async def async_step_dhcp(self, discovery_info: Any) -> ConfigFlowResult:
        """Update a known logger when DHCP hands it a new address."""
        mac = format_mac(str(_info(discovery_info, "macaddress")))
        host = str(_info(discovery_info, "ip"))
        for entry in self._async_current_entries():
            stored = format_mac(str(entry.data.get(CONF_LOGGER_MAC) or ""))
            serial = str(entry.data.get(CONF_LOGGER_SERIAL) or "")
            if not stored or stored != mac:
                continue
            if entry.data.get(CONF_LOGGER_HOST) == host:
                return self.async_abort(reason="already_configured")
            kind = entry.data.get(CONF_LOGGER_KIND)
            _LOGGER.info(
                "%s logger %s is now at %s",
                kind_label(str(kind or "")),
                serial,
                host,
            )
            if str(entry.unique_id) == serial:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured(updates={CONF_LOGGER_HOST: host})
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_LOGGER_HOST: host},
                reason="already_configured",
            )
        if not mac.startswith(SOLARMAN_OUI):
            return self.async_abort(reason="not_solis_logger")
        return self.async_abort(reason="logger_not_setup")

    def _title(self, station_ids: list[str]) -> str:
        names = [self._discovered.get(station_id, station_id) for station_id in station_ids]
        if len(names) == 1:
            return names[0]
        return "Solis"


class SolisOptionsFlow(OptionsFlow):
    """Change polling without re-entering the API secret."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit scan interval, timeout, the per-endpoint gap and the timezone."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            zone, zone_error = clean_time_zone(
                user_input.get(CONF_TIME_ZONE, options.get(CONF_TIME_ZONE, ""))
            )
            if zone_error:
                errors[CONF_TIME_ZONE] = zone_error
            else:
                return self.async_create_entry(
                    data={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_REQUEST_TIMEOUT: int(user_input[CONF_REQUEST_TIMEOUT]),
                        CONF_ENDPOINT_INTERVAL: float(user_input[CONF_ENDPOINT_INTERVAL]),
                        CONF_TIME_ZONE: zone,
                    }
                )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_LOCAL_SCAN_INTERVAL, max=3600)),
                vol.Required(
                    CONF_REQUEST_TIMEOUT,
                    default=options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                vol.Required(
                    CONF_ENDPOINT_INTERVAL,
                    default=options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=30)),
                vol.Optional(
                    CONF_TIME_ZONE,
                    default=options.get(CONF_TIME_ZONE, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=_schema(schema), errors=errors)


async def _fetch_stations(hass: Any, data: Mapping[str, Any]) -> dict[str, str]:
    """Return ``{station id: name}`` using a short-lived client on the HA session."""
    client = SolisCloudClient(
        async_get_clientsession(hass),
        base_url=_normalize_url(str(data[CONF_API_URL])),
        key_id=str(data[CONF_KEY_ID]),
        secret=str(data[CONF_KEY_SECRET]),
        timeout=float(data.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)),
        endpoint_interval=0,
    )
    rows = await client.user_station_list()
    stations: dict[str, str] = {}
    for row in rows:
        station_id = str(row.get("id") or "")
        if not station_id:
            continue
        stations[station_id] = str(row.get("stationName") or row.get("sno") or station_id)
    return stations


def _mac_for(sticks: list[Stick], serial: str, kind: str) -> str:
    """Return the discovered MAC when this is the Solarman stick with that serial."""
    if kind != LOGGER_SOLARMAN or not serial.isdigit():
        return ""
    wanted = int(serial)
    for stick in sticks:
        if stick.serial == wanted:
            return stick.mac
    return ""


def _logger_kind_field(default: str, *, required: bool) -> dict[Any, Any]:
    """Dropdown of logger kinds. Labels live in the translation files."""
    marker = vol.Required if required else vol.Optional
    return {
        marker(CONF_LOGGER_KIND, default=default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(SELECTABLE_KINDS),
                translation_key="logger_kind",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    }


def _info(discovery: Any, key: str) -> Any:
    if isinstance(discovery, Mapping):
        return discovery.get(key, "")
    return getattr(discovery, key, "")


def migrate_entry_data(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Convert a solis-sensor 4.x config entry into this integration's shape."""
    station_id = data.get("portal_plant_id")
    station_ids = (
        [str(station_id)] if station_id not in (None, "") else list(data.get(CONF_STATION_IDS, []))
    )
    new_data = {
        CONF_API_URL: _normalize_url(
            str(data.get("portal_domain") or data.get(CONF_API_URL) or DEFAULT_API_URL)
        ),
        CONF_KEY_ID: str(data.get("portal_key_id") or data.get(CONF_KEY_ID) or ""),
        CONF_KEY_SECRET: str(data.get("portal_secret") or data.get(CONF_KEY_SECRET) or ""),
        CONF_STATION_IDS: station_ids,
        CONF_LEGACY_NAME: str(data.get(CONF_NAME) or data.get(CONF_LEGACY_NAME) or "Solis"),
    }
    new_options = {
        CONF_SCAN_INTERVAL: int(
            data.get("refresh_ok") or options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        ),
        CONF_REQUEST_TIMEOUT: int(
            data.get("request_timeout")
            or options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)
        ),
        CONF_ENDPOINT_INTERVAL: float(
            options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL)
        ),
        CONF_TIME_ZONE: str(options.get(CONF_TIME_ZONE, "")),
    }
    return new_data, new_options
