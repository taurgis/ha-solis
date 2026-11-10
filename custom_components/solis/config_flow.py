"""Config flow for Solis Cloud.

Authentication is the API key id and secret only. SolisCloud no longer needs
the portal username for reads, which is the failure hultenvp/solis-sensor
issue 517 describes.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolisCloudClient
from .const import (
    CONF_API_URL,
    CONF_ENDPOINT_INTERVAL,
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    CONF_LEGACY_NAME,
    CONF_REQUEST_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONF_STATION_IDS,
    DEFAULT_API_URL,
    DEFAULT_ENDPOINT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .errors import SolisAuthError, SolisClockError, SolisCloudError, SolisTimeoutError

_LOGGER = logging.getLogger(__name__)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_URL, default=DEFAULT_API_URL): str,
        vol.Required(CONF_KEY_ID): str,
        vol.Required(CONF_KEY_SECRET): str,
    }
)


def _schema(schema: vol.Schema) -> Any:
    """Adapt a voluptuous schema to the config-flow type of this Home Assistant."""
    return cast(Any, schema)


def _normalize_url(url: str) -> str:
    text = url.strip().rstrip("/")
    if not text.startswith("http"):
        text = f"https://{text}"
    return text


class SolisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Solis config flow."""

    VERSION = 2

    def __init__(self) -> None:
        """Prepare discovered-station state."""
        self._discovered: dict[str, str] = {}
        self._credentials: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolisOptionsFlow:
        """Return the options flow."""
        return SolisOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the API URL and key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {
                **user_input,
                CONF_API_URL: _normalize_url(str(user_input[CONF_API_URL])),
                CONF_KEY_ID: str(user_input[CONF_KEY_ID]).strip(),
                CONF_KEY_SECRET: str(user_input[CONF_KEY_SECRET]).strip(),
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
            await self.async_set_unique_id(self._credentials[CONF_KEY_ID])
            self._abort_if_unique_id_configured()
            title = self._title(station_ids)
            return self.async_create_entry(
                title=title,
                data={
                    CONF_API_URL: self._credentials[CONF_API_URL],
                    CONF_KEY_ID: self._credentials[CONF_KEY_ID],
                    CONF_KEY_SECRET: self._credentials[CONF_KEY_SECRET],
                    CONF_STATION_IDS: station_ids,
                },
                options={
                    CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    CONF_REQUEST_TIMEOUT: DEFAULT_REQUEST_TIMEOUT,
                    CONF_ENDPOINT_INTERVAL: DEFAULT_ENDPOINT_INTERVAL,
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
            if not errors and station_ids:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_API_URL: url, CONF_STATION_IDS: station_ids},
                    options={
                        **entry.options,
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_REQUEST_TIMEOUT: int(user_input[CONF_REQUEST_TIMEOUT]),
                        CONF_ENDPOINT_INTERVAL: float(user_input[CONF_ENDPOINT_INTERVAL]),
                    },
                )
            if not station_ids:
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
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                vol.Required(
                    CONF_REQUEST_TIMEOUT,
                    default=options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                vol.Required(
                    CONF_ENDPOINT_INTERVAL,
                    default=options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=30)),
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

    def _title(self, station_ids: list[str]) -> str:
        names = [self._discovered.get(station_id, station_id) for station_id in station_ids]
        if len(names) == 1:
            return names[0]
        return "Solis"


class SolisOptionsFlow(OptionsFlow):
    """Change polling without re-entering the API secret."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit scan interval, timeout and the per-endpoint gap."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_REQUEST_TIMEOUT: int(user_input[CONF_REQUEST_TIMEOUT]),
                    CONF_ENDPOINT_INTERVAL: float(user_input[CONF_ENDPOINT_INTERVAL]),
                }
            )
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                vol.Required(
                    CONF_REQUEST_TIMEOUT,
                    default=options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
                vol.Required(
                    CONF_ENDPOINT_INTERVAL,
                    default=options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=30)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=_schema(schema))


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
    }
    return new_data, new_options
