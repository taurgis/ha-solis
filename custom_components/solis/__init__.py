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
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SolisCloudClient
from .config_flow import migrate_entry_data
from .const import (
    CONF_API_URL,
    CONF_ENDPOINT_INTERVAL,
    CONF_INVERTER_SERIAL,
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    CONF_LOGGER_HOST,
    CONF_LOGGER_MAC,
    CONF_LOGGER_PROTOCOL,
    CONF_LOGGER_SERIAL,
    CONF_REGISTER_MAP,
    CONF_REQUEST_TIMEOUT,
    CONF_SERIAL_UNSUPPORTED,
    CONF_SLAVE_ID,
    CONF_SOURCE,
    CONF_STATION_IDS,
    CONF_TIME_ZONE,
    DEFAULT_ENDPOINT_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
    ISSUE_LEGACY_CONTROL,
    LOCAL_REQUEST_GAP,
    LOCAL_TIMEOUT_CAP,
    POLL_REPAIRS,
    REGISTER_STRING,
    SOURCE_BOTH,
    SOURCE_CLOUD,
)
from .coordinator import SolisCoordinator
from .local_transport import SolisLocalTransport, SolisPriorityTransport
from .migration import async_adopt_inverter_serial, async_migrate_devices
from .transport import SolisCloudTransport

_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR, Platform.BINARY_SENSOR)

type SolisConfigEntry = ConfigEntry[SolisCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SolisConfigEntry) -> bool:
    """Set up one Solis account, local logger first when it is configured."""
    transport = _build_transport(hass, entry)
    coordinator = SolisCoordinator(hass, entry, transport)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        closer = getattr(transport, "close", None)
        if closer is not None:
            await closer()
        if coordinator.auth_failed:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="update_auth",
            ) from err
        raise
    async_migrate_devices(
        hass,
        entry.entry_id,
        {inverter.serial for inverter in coordinator.data.inverters},
    )
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    closer = getattr(transport, "close", None)
    if closer is not None:
        entry.async_on_unload(closer)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolisConfigEntry) -> bool:
    """Unload a config entry and drop poll-failure repairs."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for key in POLL_REPAIRS:
            ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")
    return unloaded


def _build_transport(
    hass: HomeAssistant, entry: SolisConfigEntry
) -> SolisCloudTransport | SolisPriorityTransport:
    """Cloud only, local only, or local with cloud totals as a fallback."""
    source = str(entry.data.get(CONF_SOURCE, SOURCE_CLOUD))
    cloud: SolisCloudTransport | None = None
    if source in {SOURCE_CLOUD, SOURCE_BOTH} and entry.data.get(CONF_KEY_ID):
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
        cloud = SolisCloudTransport(
            client,
            set(entry.data.get(CONF_STATION_IDS, [])),
            time_zone=str(entry.options.get(CONF_TIME_ZONE, "")),
            hass_time_zone=hass.config.time_zone,
        )
    if source == SOURCE_CLOUD or not entry.data.get(CONF_LOGGER_HOST):
        if cloud is None:
            raise ConfigEntryNotReady("Solis has no cloud account or local logger")
        return cloud
    local = SolisLocalTransport(
        str(entry.data[CONF_LOGGER_HOST]),
        int(entry.data[CONF_LOGGER_SERIAL]),
        slave=int(entry.data.get(CONF_SLAVE_ID, 1)),
        register_map=str(entry.data.get(CONF_REGISTER_MAP, REGISTER_STRING)),
        inverter_serial=str(entry.data.get(CONF_INVERTER_SERIAL) or ""),
        mac=str(entry.data.get(CONF_LOGGER_MAC) or ""),
        protocol=str(entry.data.get(CONF_LOGGER_PROTOCOL) or ""),
        serial_unsupported=bool(entry.data.get(CONF_SERIAL_UNSUPPORTED)),
        timeout=min(
            LOCAL_TIMEOUT_CAP,
            float(entry.options.get(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT)),
        ),
        gap=LOCAL_REQUEST_GAP,
        on_host=lambda host, mac: _store_logger_host(hass, entry, host, mac),
        on_protocol=lambda protocol: _store_text(hass, entry, CONF_LOGGER_PROTOCOL, protocol),
        on_serial=lambda serial: _store_inverter_serial(hass, entry, serial),
    )
    return SolisPriorityTransport(source, local, cloud)


def _store_text(hass: HomeAssistant, entry: SolisConfigEntry, key: str, value: str) -> None:
    """Persist one string field when it changes. The update listener reloads later."""
    if entry.data.get(key) == value:
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, key: value})


def _store_inverter_serial(hass: HomeAssistant, entry: SolisConfigEntry, serial: str) -> None:
    """Point existing entities at the inverter serial shared with SolisCloud."""
    old = str(entry.data.get(CONF_INVERTER_SERIAL) or "")
    if not serial or old == serial:
        return
    if old:
        async_adopt_inverter_serial(hass, entry.entry_id, old, serial)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_INVERTER_SERIAL: serial})


def _store_logger_host(hass: HomeAssistant, entry: SolisConfigEntry, host: str, mac: str) -> None:
    """Persist a DHCP or discovery address change. Logged once per new address."""
    if entry.data.get(CONF_LOGGER_HOST) == host and (
        not mac or entry.data.get(CONF_LOGGER_MAC) == mac
    ):
        return
    _LOGGER.info("Solarman logger %s is now at %s", entry.data.get(CONF_LOGGER_SERIAL), host)
    data = {**entry.data, CONF_LOGGER_HOST: host}
    if mac:
        data[CONF_LOGGER_MAC] = mac
    hass.config_entries.async_update_entry(entry, data=data)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a solis-sensor 4.x entry, then mark cloud-only entries as version 3."""
    _LOGGER.info("Migrating Solis config entry from version %s", entry.version)
    if entry.version < 2:
        had_control = bool(entry.data.get("portal_control_api"))
        data, options = migrate_entry_data(entry.data, entry.options)
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=2, minor_version=1
        )
    else:
        had_control = False
    if entry.version < 3:
        data = {**entry.data, CONF_SOURCE: entry.data.get(CONF_SOURCE, SOURCE_CLOUD)}
        hass.config_entries.async_update_entry(entry, data=data, version=3, minor_version=1)
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


def _known_device_keys(entry: SolisConfigEntry) -> set[str]:
    """Return the device identifiers present in the latest snapshot."""
    data = entry.runtime_data.data
    if data is None:
        return set()
    present = {f"inverter:{inverter.serial}" for inverter in data.inverters}
    present.update(f"station:{station.station_id}" for station in data.stations)
    present.update(f"collector:{collector.serial}" for collector in data.collectors)
    present.update(f"epm:{epm.serial}" for epm in data.epms)
    return present


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: SolisConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Let the user delete a device the latest poll no longer contains."""
    del hass
    keys = {item[1] for item in device_entry.identifiers if item[0] == DOMAIN}
    if not keys:
        return False
    return keys.isdisjoint(_known_device_keys(entry))


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
