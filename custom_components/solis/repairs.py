"""Repair flows for credential and legacy-control issues."""

from __future__ import annotations

from typing import Any, cast

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant


class _StartReauthFlow(RepairsFlow):
    """Send the user into the reauth config flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start reauthentication and close the repair prompt."""
        entry_id = None if self.data is None else self.data.get("entry_id")
        if isinstance(entry_id, str):
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is not None:
                entry.async_start_reauth(self.hass)
        return cast(
            "data_entry_flow.FlowResult",
            self.async_create_entry(data={}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a Solis repair."""
    if issue_id.startswith("invalid_auth"):
        return _StartReauthFlow()
    return ConfirmRepairFlow()
