"""Diagnostics with secrets and location redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_KEY_ID, CONF_KEY_SECRET
from .coordinator import SolisCoordinator

TO_REDACT = {
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    "portal_key_id",
    "portal_secret",
    "portal_password",
    "portal_username",
    "password",
    "username",
    "latitude",
    "longitude",
    "address",
    "key_secret",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: Any,
) -> dict[str, Any]:
    """Return redacted config and the latest snapshot."""
    coordinator: SolisCoordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "snapshot": _snapshot(coordinator),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: Any,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return the snapshot rows that belong to one device."""
    coordinator: SolisCoordinator = entry.runtime_data
    identifiers = {item[1] for item in device.identifiers}
    return {
        "identifiers": sorted(identifiers),
        "snapshot": _snapshot(coordinator, identifiers),
    }


def _snapshot(coordinator: SolisCoordinator, identifiers: set[str] | None = None) -> dict[str, Any]:
    data = coordinator.data
    stations = []
    for station in data.stations:
        if identifiers is not None and f"station:{station.station_id}" not in identifiers:
            continue
        stations.append(
            async_redact_data(
                {
                    "station_id": station.station_id,
                    "name": station.name,
                    "timezone_offset_hours": station.timezone_offset_hours,
                    "day_energy_kwh": station.day_energy_kwh,
                    "grid_import_today_kwh": station.grid_import_today_kwh,
                    "grid_export_today_kwh": station.grid_export_today_kwh,
                    "load_today_kwh": station.load_today_kwh,
                    "latitude": station.latitude,
                    "longitude": station.longitude,
                    "address": station.address,
                },
                TO_REDACT,
            )
        )
    inverters = []
    for inverter in data.inverters:
        if identifiers is not None and f"inverter:{inverter.serial}" not in identifiers:
            continue
        inverters.append(
            {
                "serial": inverter.serial,
                "station_id": inverter.station_id,
                "model": inverter.model,
                "state": inverter.state,
                "condition": inverter.condition(data.fetched_at),
                "ac_power_w": inverter.ac_power_w,
                "energy_today_kwh": inverter.energy_today_kwh,
                "energy_total_kwh": inverter.energy_total_kwh,
                "battery_soc": inverter.battery_soc,
                "battery_power_w": inverter.battery_power_w,
                "data_timestamp": inverter.data_timestamp.isoformat()
                if inverter.data_timestamp
                else None,
            }
        )
    return {
        "fetched_at": data.fetched_at.isoformat(),
        "partial": data.partial,
        "stations": stations,
        "inverters": inverters,
        "alarm_count": len(data.alarms),
        "collector_count": len(data.collectors),
        "epm_count": len(data.epms),
    }
