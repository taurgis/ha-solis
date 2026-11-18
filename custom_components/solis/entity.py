"""Shared entity base."""

from __future__ import annotations

from typing import Any, cast

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOCAL_STATION, MANUFACTURER
from .coordinator import SolisCoordinator
from .models import CollectorSnapshot, EpmSnapshot, InverterSnapshot, StationSnapshot
from .registers import device_model_name


class SolisEntity(CoordinatorEntity[SolisCoordinator]):
    """Base for Solis entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SolisCoordinator,
        description: EntityDescription,
        unique_id: str,
        device_info: DeviceInfo,
    ) -> None:
        """Bind one description to one device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info


def station_device(station: StationSnapshot) -> DeviceInfo:
    """Return the device registry entry for a plant."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"station:{station.station_id}")},
        manufacturer=MANUFACTURER,
        name=station.name,
        model="SolisCloud plant",
    )


def inverter_device(
    inverter: InverterSnapshot,
    station_name: str | None,
    via_device_id: str | None = None,
) -> DeviceInfo:
    """Return the device registry entry for an inverter."""
    name = inverter.name or inverter.serial
    if name == inverter.serial or (station_name and name == station_name):
        model = inverter.model or inverter.serial
        name = f"{station_name} {model}".strip() if station_name else model
    info = DeviceInfo(
        identifiers={(DOMAIN, f"inverter:{inverter.serial}")},
        manufacturer=MANUFACTURER,
        model=device_model_name(inverter.model, inverter.model_code)
        if inverter.model or inverter.model_code
        else _product_model(inverter.product_model),
        name=name,
        serial_number=inverter.serial,
        sw_version=inverter.hmi_version,
    )
    _attach_via(info, inverter.station_id, via_device_id)
    return info


def collector_device(
    collector: CollectorSnapshot,
    via_device_id: str | None = None,
    mac: str | None = None,
) -> DeviceInfo:
    """Return the device registry entry for a data logger.

    ``mac`` is the stick address stored on a local entry. It lets Home Assistant
    match the logger with the same device seen by a router or DHCP integration.
    """
    info = DeviceInfo(
        identifiers={(DOMAIN, f"collector:{collector.serial}")},
        manufacturer=MANUFACTURER,
        model=collector.model or "Data logger",
        serial_number=collector.serial,
    )
    _name(info, "logger", "Logger", collector.serial)
    if mac:
        info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
    _attach_via(info, collector.station_id, via_device_id)
    return info


def epm_device(epm: EpmSnapshot, via_device_id: str | None = None) -> DeviceInfo:
    """Return the device registry entry for an export power manager."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"epm:{epm.serial}")},
        manufacturer=MANUFACTURER,
        model="EPM",
        serial_number=epm.serial,
    )
    _name(info, "epm", "EPM", epm.serial)
    _attach_via(info, epm.station_id, via_device_id)
    return info


def _name(info: DeviceInfo, translation_key: str, prefix: str, serial: str) -> None:
    """Name a subdevice ``{prefix} {serial}`` unless the serial already starts with it."""
    if serial.casefold().startswith(prefix.casefold()):
        info["name"] = serial
        return
    info["translation_key"] = translation_key
    info["translation_placeholders"] = {"serial": serial}


def _attach_via(info: DeviceInfo, station_id: str | None, device_id: str | None) -> None:
    """Link a subdevice to its plant on both 2025.10 and current Home Assistant.

    ``via_device_id`` is what ``async_get_or_create`` accepts from 2026.8.
    2025.10 still takes ``via_device`` as a ``(domain, identifier)`` tuple.
    """
    if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8):
        if device_id:
            info["via_device_id"] = device_id
        return
    # The local placeholder plant has no device, so a local inverter has no parent.
    if station_id and station_id != LOCAL_STATION:
        cast("dict[str, Any]", info)["via_device"] = (DOMAIN, f"station:{station_id}")


def _product_model(value: str | None) -> str:
    if value == "2":
        return "Hybrid inverter"
    if value == "1":
        return "Grid inverter"
    return "Solis inverter"
