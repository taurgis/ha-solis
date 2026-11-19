"""Config flow for Solis Cloud.

Authentication is the API key id and secret only. SolisCloud no longer needs
the portal username for reads, which is the failure hultenvp/solis-sensor
issue 517 describes.

The first step is a menu: SolisCloud only, a local stick only, or a local
stick with SolisCloud as a fallback. The stored ``source`` values and data
keys are the ones earlier versions wrote, so entries and migrations do not
change shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import ipaddress
import logging
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

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
    CONF_LOGGER_PASSWORD,
    CONF_LOGGER_PORT,
    CONF_LOGGER_PRESET,
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
    LOGGER_MBTCP,
    LOGGER_S2,
    LOGGER_SOLARMAN,
    LOGGER_WEB,
    MIN_CLOUD_SCAN_INTERVAL,
    MIN_LOCAL_SCAN_INTERVAL,
    SOLARMAN_OUIS,
    SOURCE_BOTH,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
    WEB_DEFAULT_PASSWORD,
)
from .errors import (
    SolisAuthError,
    SolisClockError,
    SolisCloudError,
    SolisTimeoutError,
)
from .local_transport import (
    LoggerProbe,
    LoggerRequest,
    identify_logger,
    logger_form_error,
    logger_setup,
    parse_logger_form,
    s2_needs_identity,
)
from .loggers import SELECTABLE_KINDS, kind_label
from .solarman import Stick, discover_sticks, format_mac

_LOGGER = logging.getLogger(__name__)

# Collapsed form section with the fields most setups never touch.
ADVANCED = "advanced"
# Shown in the timezone dropdown. Stored as "" so older entries read the same.
TZ_AUTO = "auto"
TZ_PLANT = "plant"
# RS485 gateway models. Older entries may hold "default", the same Elfin /
# Hi-Flying factory port 8899; the form shows it as "elfin" and the runtime
# resolves either to 8899.
PRESETS = ("elfin", "waveshare", "usr", "esp")
# Firmware details for the logger errors live in the docs, not in the strings.
DOCS_TROUBLESHOOTING = "https://github.com/taurgis/ha-solis/blob/main/docs/troubleshooting.md"

_SECRET = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)
_URL = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))
_PORT = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX)
)
_SLAVE = selector.NumberSelector(
    selector.NumberSelectorConfig(min=1, max=247, step=1, mode=selector.NumberSelectorMode.BOX)
)
_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_URL, default=DEFAULT_API_URL): _URL,
        vol.Required(CONF_KEY_ID): str,
        vol.Required(CONF_KEY_SECRET): _SECRET,
    }
)
_LOCAL_OPTIONS = {
    CONF_SCAN_INTERVAL: DEFAULT_LOCAL_SCAN_INTERVAL,
    CONF_REQUEST_TIMEOUT: DEFAULT_REQUEST_TIMEOUT,
    CONF_ENDPOINT_INTERVAL: DEFAULT_ENDPOINT_INTERVAL,
    CONF_TIME_ZONE: "",
}
_CLOUD_OPTIONS = {**_LOCAL_OPTIONS, CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL}


def _schema(schema: vol.Schema | dict[Any, Any]) -> Any:
    """Adapt a voluptuous schema to the config-flow type of this Home Assistant."""
    if isinstance(schema, dict):
        return vol.Schema(schema)
    return cast(Any, schema)


def _scan_bounds(entry: ConfigEntry) -> tuple[int, int]:
    """Cloud stays at 60 seconds or slower. A local logger may poll every 30."""
    source = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
    if source == SOURCE_CLOUD:
        return MIN_CLOUD_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
    return MIN_LOCAL_SCAN_INTERVAL, DEFAULT_LOCAL_SCAN_INTERVAL


def _options_after(entry: ConfigEntry, source: str) -> dict[str, Any]:
    """Options for ``entry`` once it reads ``source``.

    A poll interval still on the default of the connection it leaves moves to
    the default of the new one: five minutes would leave a stick idle, and one
    minute is SolisCloud's floor. An interval the user chose stays.
    """
    options = dict(entry.options)
    before = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD)) == SOURCE_CLOUD
    after = source == SOURCE_CLOUD
    old, new = (
        (DEFAULT_SCAN_INTERVAL, DEFAULT_LOCAL_SCAN_INTERVAL)
        if before
        else (DEFAULT_LOCAL_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    if before != after and int(options.get(CONF_SCAN_INTERVAL, old)) == old:
        options[CONF_SCAN_INTERVAL] = new
    return options


def _shown_scan(entry: ConfigEntry) -> int:
    floor, default = _scan_bounds(entry)
    saved = int(entry.options.get(CONF_SCAN_INTERVAL, default))
    return max(floor, min(3600, saved))


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
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in {"localhost", "mock"} or ("." not in host and not host.isdigit())
    return address.is_private or address.is_loopback or address.is_link_local


def _is_private_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


async def async_clean_time_zone(value: object) -> tuple[str, str | None]:
    """Return a stored timezone preference and a form error key, if any.

    ``auto`` (or empty) follows Home Assistant and is stored as ``""``.
    ``plant`` keeps the Solis offset. Anything else must be an IANA zone; the
    zone database is read off the event loop.
    """
    text = str(value or "").strip()
    if text in {"", TZ_AUTO}:
        return "", None
    if text == TZ_PLANT:
        return text, None
    try:
        zone = await dt_util.async_get_time_zone(text)
    except ValueError:
        zone = None
    if zone is None:
        return text, "invalid_timezone"
    return text, None


def _sources_for(data: Mapping[str, Any]) -> list[str]:
    """Connections this entry has the settings for."""
    cloud = bool(str(data.get(CONF_KEY_ID) or "").strip())
    local = bool(str(data.get(CONF_LOGGER_HOST) or "").strip())
    sources = [SOURCE_CLOUD] if cloud else []
    if local:
        sources.append(SOURCE_LOCAL)
    if cloud and local:
        sources.append(SOURCE_BOTH)
    return sources


class SolisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Solis config flow."""

    VERSION = 3

    def __init__(self) -> None:
        """Prepare discovered-station state."""
        self._discovered: dict[str, str] = {}
        self._credentials: dict[str, Any] = {}
        self._source = SOURCE_CLOUD
        self._sticks: list[Stick] = []
        self._found: Stick | None = None
        self._choice: tuple[LoggerRequest, LoggerProbe, str] | None = None
        self._pending_logger: dict[str, Any] = {}
        self._pending_updates: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolisOptionsFlow:
        """Return the options flow."""
        return SolisOptionsFlow()

    # New entry -------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask how Home Assistant reads the inverter."""
        return self.async_show_menu(step_id="user", menu_options=["cloud", "local", "both"])

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Local stick only. No SolisCloud key."""
        self._source = SOURCE_LOCAL
        return await self.async_step_logger()

    async def async_step_both(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Local stick first, SolisCloud as the fallback."""
        self._source = SOURCE_BOTH
        return await self.async_step_cloud()

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the API URL and key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            candidate = {
                CONF_API_URL: _normalize_url(str(user_input.get(CONF_API_URL) or DEFAULT_API_URL)),
                CONF_KEY_ID: str(user_input.get(CONF_KEY_ID) or "").strip(),
                CONF_KEY_SECRET: str(user_input.get(CONF_KEY_SECRET) or "").strip(),
                CONF_SOURCE: self._source,
            }
            if not candidate[CONF_KEY_ID] or not candidate[CONF_KEY_SECRET]:
                errors["base"] = "key_required"
            elif self._key_taken(candidate[CONF_KEY_ID], self._source, None):
                # With a stick, the clash is a SolisCloud-only entry that can gain it.
                reason = (
                    "cloud_entry_exists" if self._source == SOURCE_BOTH else "already_configured"
                )
                return self.async_abort(reason=reason)
            else:
                errors = await self._load_stations(candidate)
                if not errors:
                    self._credentials = candidate
                    return await self.async_step_stations()
        schema = _schema(_CLOUD_SCHEMA)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="cloud", data_schema=schema, errors=errors)

    async def async_step_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which plants this entry should follow."""
        reconfigure = self.source == "reconfigure"
        if reconfigure and not self._discovered:
            entry = self._get_reconfigure_entry()
            if await self._load_stations(dict(entry.data)):
                return self.async_abort(reason="cannot_connect")
        if reconfigure:
            saved = self._get_reconfigure_entry().data.get(CONF_STATION_IDS, [])
            default = [str(item) for item in saved if str(item) in self._discovered]
        else:
            default = list(self._discovered)
        schema = vol.Schema(
            {vol.Required(CONF_STATION_IDS, default=default): cv.multi_select(self._discovered)}
        )
        if user_input is not None:
            station_ids = [str(station_id) for station_id in user_input[CONF_STATION_IDS]]
            if not station_ids:
                return self.async_show_form(
                    step_id="stations",
                    data_schema=_schema(schema),
                    errors={"base": "select_station"},
                )
            if reconfigure:
                return self._save_stations(station_ids)
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
                title=self._title(station_ids), data=data, options=dict(_CLOUD_OPTIONS)
            )
        return self.async_show_form(step_id="stations", data_schema=_schema(schema))

    def _save_stations(self, station_ids: list[str]) -> ConfigFlowResult:
        """Store the plants, and any account change that led here."""
        entry = self._get_reconfigure_entry()
        updates: dict[str, Any] = {**self._pending_updates, CONF_STATION_IDS: station_ids}
        if str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD)) == SOURCE_LOCAL:
            # A SolisCloud account on a local entry becomes its fallback.
            updates[CONF_SOURCE] = SOURCE_BOTH
        key_id = str(updates.get(CONF_KEY_ID) or entry.data.get(CONF_KEY_ID) or "")
        return self.async_update_reload_and_abort(
            entry, unique_id=self._unique_id_after(entry, key_id), data_updates=updates
        )

    @staticmethod
    def _unique_id_after(entry: ConfigEntry, key_id: str) -> str | None:
        """A SolisCloud-only entry is keyed by its key id; a stick entry keeps its own."""
        saved_key = str(entry.data.get(CONF_KEY_ID) or "")
        if key_id and saved_key and entry.unique_id == saved_key:
            return key_id
        return entry.unique_id

    def _key_taken(self, key_id: str, source: str, entry: ConfigEntry | None) -> bool:
        """True when another entry already reads this SolisCloud account.

        Two local-with-fallback entries may share a key: each has its own
        stick. A SolisCloud-only entry next to any other entry of the same key
        would read the same plants twice.
        """
        for other in self._async_current_entries(include_ignore=False):
            if entry is not None and other.entry_id == entry.entry_id:
                continue
            if str(other.data.get(CONF_KEY_ID) or "") != key_id:
                continue
            if SOURCE_CLOUD in {source, str(other.data.get(CONF_SOURCE, SOURCE_CLOUD))}:
                return True
        return False

    # Reauth ----------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start a credential refresh. A web-page stick may need its password."""
        if str(entry_data.get(CONF_LOGGER_KIND) or "") != LOGGER_WEB:
            return await self.async_step_reauth_confirm()
        if not str(entry_data.get(CONF_KEY_ID) or "") or (
            str(entry_data.get(CONF_SOURCE, SOURCE_CLOUD)) == SOURCE_LOCAL
        ):
            return await self.async_step_reauth_web()
        return self.async_show_menu(step_id="reauth", menu_options=["reauth_web", "reauth_confirm"])

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
                saved_key = str(entry.data.get(CONF_KEY_ID) or "")
                if candidate[CONF_KEY_ID] == saved_key:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_KEY_SECRET: candidate[CONF_KEY_SECRET]}
                    )
                # A rotated key has a new key id. It is the same account when it
                # still sees every plant this entry reads.
                saved = {str(item) for item in entry.data.get(CONF_STATION_IDS, [])}
                if not saved or not saved <= set(self._discovered):
                    return self.async_abort(reason="wrong_account")
                source = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
                if self._key_taken(candidate[CONF_KEY_ID], source, entry):
                    return self.async_abort(reason="already_configured")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=self._unique_id_after(entry, candidate[CONF_KEY_ID]),
                    data_updates={
                        CONF_KEY_ID: candidate[CONF_KEY_ID],
                        CONF_KEY_SECRET: candidate[CONF_KEY_SECRET],
                    },
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_KEY_ID, default=entry.data.get(CONF_KEY_ID, "")): str,
                vol.Required(CONF_KEY_SECRET): _SECRET,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_schema(schema), errors=errors
        )

    async def async_step_reauth_web(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the web password of a stick that only serves a status page."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = str(user_input.get(CONF_LOGGER_PASSWORD) or "").strip()
            port = str(entry.data.get(CONF_LOGGER_PORT) or "")
            request = LoggerRequest(
                str(entry.data.get(CONF_LOGGER_HOST) or ""),
                LOGGER_WEB,
                int(port) if port.isdigit() else None,
                inverter_serial=str(entry.data.get(CONF_INVERTER_SERIAL) or ""),
                password=password or WEB_DEFAULT_PASSWORD,
            )
            try:
                await identify_logger(request)
            except (SolisCloudError, ValueError, TimeoutError) as err:
                errors["base"] = logger_form_error(err)
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_LOGGER_PASSWORD: request.password}
                )
        return self.async_show_form(
            step_id="reauth_web",
            data_schema=_schema(vol.Schema({vol.Optional(CONF_LOGGER_PASSWORD): _SECRET})),
            errors=errors,
            description_placeholders={"host": str(entry.data.get(CONF_LOGGER_HOST) or "")},
        )

    # Reconfigure -----------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the part of the entry to change."""
        entry = self._get_reconfigure_entry()
        source = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
        if source == SOURCE_LOCAL:
            options = ["logger", "reconfigure_cloud"]
        else:
            options = ["reconfigure_cloud", "stations", "logger"]
        if len(_sources_for(entry.data)) > 1:
            options.append("connection")
        return self.async_show_menu(step_id="reconfigure", menu_options=options)

    async def async_step_reconfigure_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the API host or key, or add SolisCloud to a local entry."""
        entry = self._get_reconfigure_entry()
        saved_key = str(entry.data.get(CONF_KEY_ID) or "")
        errors: dict[str, str] = {}
        if user_input is not None:
            key_id = str(user_input.get(CONF_KEY_ID) or "").strip()
            secret = str(user_input.get(CONF_KEY_SECRET) or "").strip()
            if not secret and key_id == saved_key:
                secret = str(entry.data.get(CONF_KEY_SECRET) or "")
            candidate = {
                CONF_API_URL: _normalize_url(str(user_input.get(CONF_API_URL) or DEFAULT_API_URL)),
                CONF_KEY_ID: key_id,
                CONF_KEY_SECRET: secret,
            }
            current = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
            after = SOURCE_BOTH if current == SOURCE_LOCAL else current
            if not key_id or not secret:
                errors["base"] = "key_required"
            elif self._key_taken(key_id, after, entry):
                return self.async_abort(reason="already_configured")
            else:
                errors = await self._load_stations(candidate)
            if not errors:
                saved = [str(item) for item in entry.data.get(CONF_STATION_IDS, [])]
                if current == SOURCE_LOCAL or not saved or not set(saved) <= set(self._discovered):
                    # New account, or a local entry gaining SolisCloud: choose plants.
                    self._pending_updates = candidate
                    return await self.async_step_stations()
                return self.async_update_reload_and_abort(
                    entry, unique_id=self._unique_id_after(entry, key_id), data_updates=candidate
                )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_API_URL, default=str(entry.data.get(CONF_API_URL) or DEFAULT_API_URL)
                ): _URL,
                vol.Required(CONF_KEY_ID, default=saved_key): str,
                vol.Optional(CONF_KEY_SECRET): _SECRET,
            }
        )
        return self.async_show_form(
            step_id="reconfigure_cloud", data_schema=_schema(schema), errors=errors
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Switch between cloud, local and local with fallback. Saved fields stay."""
        entry = self._get_reconfigure_entry()
        allowed = _sources_for(entry.data)
        if user_input is not None:
            chosen = str(user_input[CONF_SOURCE])
            key_id = str(entry.data.get(CONF_KEY_ID) or "")
            if chosen != SOURCE_LOCAL and key_id and self._key_taken(key_id, chosen, entry):
                # Another entry already reads this account; both would poll the same plants.
                return self.async_abort(reason="already_configured")
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_SOURCE: chosen}, options=_options_after(entry, chosen)
            )
        current = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SOURCE, default=current if current in allowed else allowed[0]
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=allowed,
                        translation_key="source",
                        mode=selector.SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="connection", data_schema=_schema(schema))

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

    # Local logger ----------------------------------------------------------

    def _logger_source(self) -> str:
        """The connection a logger form stores."""
        if self.source != "reconfigure":
            return self._source
        current = str(self._get_reconfigure_entry().data.get(CONF_SOURCE, SOURCE_CLOUD))
        # A stick on a cloud entry makes it local with a cloud fallback.
        return SOURCE_BOTH if current == SOURCE_CLOUD else current

    def _logger_defaults(self) -> dict[str, Any]:
        """Prefill a reconfigure. A new entry starts from discovery."""
        if self.source == "reconfigure":
            saved = self._get_reconfigure_entry().data
            return {
                CONF_LOGGER_KIND: str(saved.get(CONF_LOGGER_KIND) or LOGGER_AUTO),
                CONF_LOGGER_HOST: str(saved.get(CONF_LOGGER_HOST) or ""),
                CONF_INVERTER_SERIAL: str(saved.get(CONF_INVERTER_SERIAL) or ""),
                ADVANCED: _advanced_defaults(saved),
            }
        found = self._found or (self._sticks[0] if self._sticks else None)
        return {
            CONF_LOGGER_KIND: LOGGER_SOLARMAN if self._found else LOGGER_AUTO,
            CONF_LOGGER_HOST: "" if found is None else found.host,
            CONF_INVERTER_SERIAL: "",
            ADVANCED: {CONF_LOGGER_PRESET: PRESETS[0]},
        }

    async def async_step_logger(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm a local logger. The inverter has to be producing."""
        errors: dict[str, str] = {}
        suggested: Mapping[str, Any]
        if user_input is None:
            if self._found is None:
                self._sticks = await discover_sticks(timeout=1.2)
            suggested = self._logger_defaults()
        else:
            suggested = user_input
            flat = await self._with_stick_serial(_flatten(user_input))
            outcome = await self._submit_logger(flat)
            if not isinstance(outcome, str):
                return outcome
            errors["base"] = outcome
        schema = self.add_suggested_values_to_schema(
            _schema(_logger_schema(self._sticks, str(suggested.get(CONF_LOGGER_HOST) or ""))),
            suggested,
        )
        return self.async_show_form(
            step_id="logger",
            data_schema=schema,
            errors=errors,
            description_placeholders={"docs_url": DOCS_TROUBLESHOOTING},
        )

    async def _submit_logger(self, flat: dict[str, Any]) -> ConfigFlowResult | str:
        """Probe one logger form. A string is the error to show on the form."""
        fields, unique, failure = await self._logger_updates(flat, source=self._logger_source())
        if failure == "logger_choice":
            return await self.async_step_logger_choice()
        if failure == "logger_web_password":
            # Only now is the password relevant: the stick refused the saved or factory one.
            self._pending_logger = {**flat, CONF_LOGGER_KIND: LOGGER_WEB}
            return self._show_password({"base": failure})
        if failure:
            return failure
        return await self._finish_logger(fields, unique)

    async def async_step_logger_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the web password of a stick that only serves a status page."""
        assert user_input is not None
        password = str(user_input.get(CONF_LOGGER_PASSWORD) or "").strip()
        flat = {**self._pending_logger, CONF_LOGGER_PASSWORD: password}
        fields, unique, failure = await self._logger_updates(flat, source=self._logger_source())
        if failure:
            return self._show_password({"base": failure})
        return await self._finish_logger(fields, unique)

    def _show_password(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="logger_password",
            data_schema=_schema(vol.Schema({vol.Optional(CONF_LOGGER_PASSWORD): _SECRET})),
            errors=errors,
            description_placeholders={
                "host": str(self._pending_logger.get(CONF_LOGGER_HOST) or "")
            },
        )

    async def _with_stick_serial(self, flat: dict[str, Any]) -> dict[str, Any]:
        """Fill the Solarman serial from discovery when the form left it empty."""
        if str(flat.get(CONF_LOGGER_SERIAL) or "").strip():
            return flat
        if str(flat.get(CONF_LOGGER_KIND) or LOGGER_AUTO) not in (LOGGER_AUTO, LOGGER_SOLARMAN):
            return flat
        host = str(flat.get(CONF_LOGGER_HOST) or "").strip()
        stick = next((item for item in self._sticks if item.host == host), None)
        if stick is None and host:
            stick = await _stick_at(host)
            if stick is not None:
                self._sticks.append(stick)
        if stick is None:
            return flat
        return {**flat, CONF_LOGGER_SERIAL: str(stick.serial)}

    async def async_step_logger_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which device answered port 502 when the web page did not."""
        pending = self._choice
        assert pending is not None
        _request, probe, _source = pending
        suggested = probe.suggested_kind or LOGGER_S2
        if user_input is not None:
            chosen = str(user_input.get(CONF_LOGGER_KIND) or suggested)
            return await self._finish_choice(pending, chosen)
        schema = vol.Schema({**_choice_field(suggested)})
        return self.async_show_form(step_id="logger_choice", data_schema=_schema(schema))

    async def _finish_choice(
        self,
        pending: tuple[LoggerRequest, LoggerProbe, str],
        chosen: str,
    ) -> ConfigFlowResult:
        request, probe, source = pending
        detection = "confirmed_s2_wl_st" if chosen == LOGGER_S2 else "confirmed_modbus_tcp"
        chosen_probe = replace(
            probe,
            kind=chosen,
            needs_choice=False,
            confidence="confirmed",
            detection=detection,
        )
        mac = _mac_for(self._sticks, request.logger_serial, chosen_probe.kind)
        fields, unique = logger_setup(request, chosen_probe, mac=mac, source=source)
        return await self._finish_logger(fields, unique)

    async def _logger_updates(
        self, user_input: Mapping[str, Any], *, source: str
    ) -> tuple[dict[str, Any], str, str]:
        """Probe the form and return entry fields, unique id, and an error key."""
        request, error = parse_logger_form(user_input)
        if error or request is None:
            return {}, "", error or "invalid_logger"
        if self.source == "reconfigure" and not request.password:
            # The saved password goes only to the host it was saved for.
            saved_entry = self._get_reconfigure_entry().data
            if request.host == str(saved_entry.get(CONF_LOGGER_HOST) or ""):
                request = replace(
                    request, password=str(saved_entry.get(CONF_LOGGER_PASSWORD) or "")
                )
        try:
            probe = await identify_logger(request)
        except (SolisCloudError, ValueError, TimeoutError) as err:
            return {}, "", logger_form_error(err)
        if s2_needs_identity(probe, request.logger_serial, request.inverter_serial):
            return {}, "", "logger_identity"
        if probe.needs_choice:
            self._choice = (request, probe, source)
            return {}, "", "logger_choice"
        mac = probe.mac or _mac_for(self._sticks, request.logger_serial, probe.kind)
        fields, unique = logger_setup(request, probe, mac=mac, source=source)
        return fields, unique, ""

    def _unique_id_taken(self, unique: str, entry: ConfigEntry) -> bool:
        """True when another entry already uses this unique id."""
        other = self.hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, unique)
        return other is not None and other.entry_id != entry.entry_id

    async def _finish_logger(self, logger_data: dict[str, Any], unique: str) -> ConfigFlowResult:
        if self.source == "reconfigure":
            entry = self._get_reconfigure_entry()
            if self._unique_id_taken(unique, entry):
                return self.async_abort(reason="already_configured")
            base = dict(entry.data)
            base.update(logger_data)
            if logger_data.get(CONF_LOGGER_KIND) != LOGGER_WEB:
                # A web-page stick swapped for a Modbus one: its password has no use.
                base.pop(CONF_LOGGER_PASSWORD, None)
            return self.async_update_reload_and_abort(
                entry,
                data=base,
                options=_options_after(entry, str(base.get(CONF_SOURCE, SOURCE_CLOUD))),
                unique_id=unique,
            )
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured(
            updates={CONF_LOGGER_HOST: logger_data[CONF_LOGGER_HOST]}
        )
        data = {**self._credentials, **logger_data}
        title = data.get(CONF_INVERTER_SERIAL) or f"Solis {unique}"
        return self.async_create_entry(title=str(title), data=data, options=dict(_LOCAL_OPTIONS))

    # Discovery -------------------------------------------------------------

    async def async_step_dhcp(self, discovery_info: Any) -> ConfigFlowResult:
        """Follow a known logger to a new address, or offer a new Solarman stick."""
        mac = format_mac(str(_info(discovery_info, "macaddress")))
        host = str(_info(discovery_info, "ip"))
        if not _is_private_host(host):
            return self.async_abort(reason="not_solis_logger")
        for entry in self._async_current_entries(include_ignore=False):
            stored = format_mac(str(entry.data.get(CONF_LOGGER_MAC) or ""))
            serial = str(entry.data.get(CONF_LOGGER_SERIAL) or "")
            if not stored or stored != mac:
                continue
            if entry.data.get(CONF_LOGGER_HOST) == host:
                return self.async_abort(reason="already_configured")
            kind = entry.data.get(CONF_LOGGER_KIND)
            if kind == LOGGER_WEB:
                # This entry sends a password to its host, so a broadcast cannot move it.
                return self.async_abort(reason="already_configured")
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
        if not mac.startswith(SOLARMAN_OUIS):
            return self.async_abort(reason="not_solis_logger")
        adopted = _entry_at_host(self._async_current_entries(include_ignore=False), host)
        if adopted is not None:
            if format_mac(str(adopted.data.get(CONF_LOGGER_MAC) or "")):
                return self.async_abort(reason="already_configured")
            _LOGGER.info(
                "%s logger %s is at %s (%s)",
                kind_label(str(adopted.data.get(CONF_LOGGER_KIND) or "")),
                adopted.data.get(CONF_LOGGER_SERIAL),
                host,
                mac,
            )
            return self.async_update_reload_and_abort(
                adopted,
                data_updates={CONF_LOGGER_MAC: mac},
                reason="already_configured",
            )
        # A new stick. Its own discovery reply names the serial, which is the unique id.
        stick = await _stick_at(host)
        if stick is None:
            return self.async_abort(reason="not_solis_logger")
        await self.async_set_unique_id(str(stick.serial))
        self._abort_if_unique_id_configured(updates={CONF_LOGGER_HOST: stick.host})
        self._found = stick
        self._sticks = [stick]
        self.context["title_placeholders"] = {"name": f"Solarman {stick.serial}"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the discovered stick and continue into the local setup."""
        found = self._found
        assert found is not None
        return self.async_show_menu(
            step_id="discovery_confirm",
            menu_options=["local", "both"],
            description_placeholders={"host": found.host, "serial": str(found.serial)},
        )

    def _title(self, station_ids: list[str]) -> str:
        names = [self._discovered.get(station_id, station_id) for station_id in station_ids]
        if len(names) == 1:
            return names[0]
        return "Solis"


class SolisOptionsFlow(OptionsFlowWithReload):
    """Change polling without re-entering the API secret. Saving reloads the entry."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit scan interval, timezone, and in Advanced the timeout and endpoint gap."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        floor, _default_scan = _scan_bounds(self.config_entry)
        timeout = options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)
        gap = options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL)
        if user_input is not None:
            zone, zone_error = await async_clean_time_zone(
                user_input.get(CONF_TIME_ZONE, options.get(CONF_TIME_ZONE, ""))
            )
            advanced = user_input.get(ADVANCED) or {}
            if zone_error:
                errors[CONF_TIME_ZONE] = zone_error
            else:
                return self.async_create_entry(
                    data={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_REQUEST_TIMEOUT: int(advanced.get(CONF_REQUEST_TIMEOUT, timeout)),
                        CONF_ENDPOINT_INTERVAL: float(advanced.get(CONF_ENDPOINT_INTERVAL, gap)),
                        CONF_TIME_ZONE: zone,
                    }
                )
        saved_zone = str(options.get(CONF_TIME_ZONE) or "") or TZ_AUTO
        schema: Any = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=_shown_scan(self.config_entry),
                ): vol.All(vol.Coerce(int), vol.Range(min=floor, max=3600)),
                vol.Optional(CONF_TIME_ZONE, default=saved_zone): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[TZ_AUTO, TZ_PLANT],
                        translation_key="time_zone",
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(ADVANCED): section(
                    _schema(
                        {
                            vol.Required(CONF_REQUEST_TIMEOUT, default=timeout): vol.All(
                                vol.Coerce(int), vol.Range(min=5, max=120)
                            ),
                            vol.Required(CONF_ENDPOINT_INTERVAL, default=gap): vol.All(
                                vol.Coerce(float), vol.Range(min=0.5, max=30)
                            ),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(_schema(schema), user_input)
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


async def _stick_at(host: str) -> Stick | None:
    """Ask one address for its Solarman discovery reply."""
    for stick in await discover_sticks(timeout=0.6, extra_targets=(host,)):
        if stick.host == host:
            return stick
    return None


def _mac_for(sticks: list[Stick], serial: str, kind: str) -> str:
    """Return the discovered MAC when this is the Solarman stick with that serial."""
    if kind != LOGGER_SOLARMAN or not serial.isdigit():
        return ""
    wanted = int(serial)
    for stick in sticks:
        if stick.serial == wanted:
            return stick.mac
    return ""


def _flatten(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the Advanced section into one mapping with text port and slave."""
    flat = {key: value for key, value in user_input.items() if key != ADVANCED}
    flat.update(user_input.get(ADVANCED) or {})
    for key in (CONF_LOGGER_PORT, CONF_SLAVE_ID):
        # The number selector hands back a float, the parser reads digits.
        if (value := flat.get(key)) is not None:
            flat[key] = str(int(value))
    return flat


def _advanced_defaults(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Saved Advanced values. Empty ones stay empty so a clear field is kept."""
    shown: dict[str, Any] = {CONF_LOGGER_PRESET: _preset(saved.get(CONF_LOGGER_PRESET))}
    if serial := str(saved.get(CONF_LOGGER_SERIAL) or ""):
        shown[CONF_LOGGER_SERIAL] = serial
    for key in (CONF_LOGGER_PORT, CONF_SLAVE_ID):
        text = str(saved.get(key) or "")
        if text.isdigit():
            shown[key] = int(text)
    return shown


def _preset(value: object) -> str:
    """Older entries stored ``default`` for the Elfin / Hi-Flying factory port."""
    text = str(value or "")
    return text if text in PRESETS else PRESETS[0]


def _logger_schema(sticks: list[Stick], host: str) -> vol.Schema:
    """Logger form: kind, address and inverter serial; the rest is Advanced."""
    host_field: Any = str
    if sticks:
        found = [
            selector.SelectOptionDict(value=stick.host, label=f"{stick.host} ({stick.serial})")
            for stick in sticks
        ]
        if host and all(stick.host != host for stick in sticks):
            found.insert(0, selector.SelectOptionDict(value=host, label=host))
        host_field = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=found,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
    return vol.Schema(
        {
            **_logger_kind_field(),
            vol.Required(CONF_LOGGER_HOST): host_field,
            vol.Optional(CONF_INVERTER_SERIAL): str,
            vol.Optional(ADVANCED): section(
                _schema(
                    {
                        vol.Optional(CONF_LOGGER_SERIAL): str,
                        vol.Optional(CONF_LOGGER_PORT): _PORT,
                        vol.Optional(CONF_SLAVE_ID): _SLAVE,
                        vol.Optional(CONF_LOGGER_PRESET, default=PRESETS[0]): (
                            selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=list(PRESETS),
                                    translation_key="logger_preset",
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _choice_field(default: str) -> dict[Any, Any]:
    """S2 or gateway. Auto-detect asks this when port 80 did not name the device."""
    return {
        vol.Required(CONF_LOGGER_KIND, default=default): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[LOGGER_S2, LOGGER_MBTCP],
                translation_key="logger_choice",
                mode=selector.SelectSelectorMode.LIST,
            )
        )
    }


def _entry_at_host(entries: list[ConfigEntry], host: str) -> ConfigEntry | None:
    """Return the local entry already using this address, if there is one."""
    for entry in entries:
        if str(entry.data.get(CONF_LOGGER_HOST) or "") == host:
            return entry
    return None


def _logger_kind_field() -> dict[Any, Any]:
    """Dropdown of logger kinds. Labels live in the translation files."""
    return {
        vol.Required(CONF_LOGGER_KIND, default=LOGGER_AUTO): selector.SelectSelector(
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
