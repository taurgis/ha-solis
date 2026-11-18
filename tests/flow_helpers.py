"""Drive the Solis config flow in its menu + section shape."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.solis.const import DOMAIN

_ADVANCED_KEYS = ("logger_serial", "logger_port", "slave_id", "logger_preset")


def logger_form(**fields: Any) -> dict[str, Any]:
    """Nest port, slave, serial and preset under the Advanced section.

    Empty text values are dropped, as the frontend leaves an untouched optional
    field out of the submitted data.
    """
    main: dict[str, Any] = {}
    advanced: dict[str, Any] = {}
    for key, value in fields.items():
        if value == "":
            continue
        (advanced if key in _ADVANCED_KEYS else main)[key] = value
    if advanced:
        main["advanced"] = advanced
    return main


async def start_user(hass: HomeAssistant, choice: str) -> dict[str, Any]:
    """Open a new flow and pick ``cloud``, ``local`` or ``both`` in the menu."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": choice})
    )


async def pick(hass: HomeAssistant, flow_id: str, step: str) -> dict[str, Any]:
    """Choose one option of a menu step."""
    return dict(await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": step}))
