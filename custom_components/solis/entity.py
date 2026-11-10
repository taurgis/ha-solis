"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import SolisCoordinator
from .models import CollectorSnapshot, EpmSnapshot, InverterSnapshot, StationSnapshot


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
        model=inverter.model or _product_model(inverter.product_model),
        name=name,
        serial_number=inverter.serial,
        sw_version=inverter.hmi_version,
    )
    if via_device_id:
        info["via_device_id"] = via_device_id
    return info


def collector_device(collector: CollectorSnapshot, via_device_id: str | None = None) -> DeviceInfo:
    """Return the device registry entry for a data logger."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"collector:{collector.serial}")},
        manufacturer=MANUFACTURER,
        model=collector.model or "Data logger",
        name=f"Logger {collector.serial}",
        serial_number=collector.serial,
    )
    if via_device_id:
        info["via_device_id"] = via_device_id
    return info


def epm_device(epm: EpmSnapshot, via_device_id: str | None = None) -> DeviceInfo:
    """Return the device registry entry for an export power manager."""
    info = DeviceInfo(
        identifiers={(DOMAIN, f"epm:{epm.serial}")},
        manufacturer=MANUFACTURER,
        model="EPM",
        name=f"EPM {epm.serial}",
        serial_number=epm.serial,
    )
    if via_device_id:
        info["via_device_id"] = via_device_id
    return info


def _product_model(value: str | None) -> str:
    if value == "2":
        return "Hybrid inverter"
    if value == "1":
        return "Grid inverter"
    return "Solis inverter"
