"""The Solis integration.

This is a rewrite of hultenvp/solis-sensor (Apache-2.0), not a GitHub fork.
The domain stays ``solis`` so replacing that custom component keeps config
entries and, after unique-id migration, entity history.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolisCloudClient
from .config_flow import migrate_entry_data
from .const import (
    CONF_API_URL,
    CONF_ENDPOINT_INTERVAL,
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    CONF_REQUEST_TIMEOUT,
    CONF_STATION_IDS,
    CONF_TIME_ZONE,
    DEFAULT_ENDPOINT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
    ISSUE_ABNORMAL,
    ISSUE_AUTH,
    ISSUE_CLOCK,
    ISSUE_LEGACY_CONTROL,
    ISSUE_NO_INVERTERS,
)
from .coordinator import SolisCoordinator
from .migration import async_migrate_devices
from .transport import SolisCloudTransport

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR, Platform.BINARY_SENSOR)

type SolisConfigEntry = ConfigEntry[SolisCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SolisConfigEntry) -> bool:
    """Set up one SolisCloud account."""
    client = SolisCloudClient(
        async_get_clientsession(hass),
        base_url=entry.data[CONF_API_URL],
        key_id=entry.data[CONF_KEY_ID],
        secret=entry.data[CONF_KEY_SECRET],
        timeout=float(entry.options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)),
        endpoint_interval=float(
            entry.options.get(CONF_ENDPOINT_INTERVAL, DEFAULT_ENDPOINT_INTERVAL)
        ),
    )
    transport = SolisCloudTransport(
        client,
        set(entry.data.get(CONF_STATION_IDS, [])),
        time_zone=str(entry.options.get(CONF_TIME_ZONE, "")),
        hass_time_zone=hass.config.time_zone,
    )
    coordinator = SolisCoordinator(hass, entry, transport)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        if coordinator.auth_failed:
            raise ConfigEntryAuthFailed("Solis Cloud rejected the API key") from err
        raise
    async_migrate_devices(
        hass,
        entry.entry_id,
        {inverter.serial for inverter in coordinator.data.inverters},
    )
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolisConfigEntry) -> bool:
    """Unload a config entry and drop poll-failure repairs."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for key in (ISSUE_AUTH, ISSUE_CLOCK, ISSUE_ABNORMAL, ISSUE_NO_INVERTERS):
            ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a solis-sensor 4.x entry to version 2."""
    _LOGGER.info("Migrating Solis config entry from version %s", entry.version)
    had_control = bool(entry.data.get("portal_control_api"))
    data, options = migrate_entry_data(entry.data, entry.options)
    hass.config_entries.async_update_entry(
        entry, data=data, options=options, version=2, minor_version=1
    )
    if had_control:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{ISSUE_LEGACY_CONTROL}_{entry.entry_id}",
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_CONTROL,
            data={"entry_id": entry.entry_id},
        )
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
