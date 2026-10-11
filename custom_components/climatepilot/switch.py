"""Switch platform for ClimatePilot.

One switch per room enables it as the bounded auxiliary heat load (design doc
5.3).  The switch is only a *permission*: whether the load is actually applied
is decided every cycle by the coordinator, which requires other zones to have a
real demand, no manual override on this room, and a temperature below the
configured cap -- so the load can never become the old "current + 2 C" runaway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .base import ClimatePilotZoneEntity
from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator


class AuxiliaryLoadSwitch(ClimatePilotZoneEntity, SwitchEntity):
    """Allow this room to act as the auxiliary heat load."""

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "auxiliary_load")
        self._attr_translation_key = "auxiliary_load"

    @property
    def is_on(self) -> bool:
        return bool(self.zone.aux_load_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_zone_aux_load(self._zone_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_zone_aux_load(self._zone_id, False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Distinguish "allowed" from "applied right now" (design doc 5.3)."""
        zone = self.zone
        delta = zone.auto_adjustments.get("aux_load")
        return {
            "cap": zone.aux_load_cap,
            "applied": delta is not None,
            "applied_delta": delta,
            "manual_override": zone.manual_override is not None,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot switch entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        AuxiliaryLoadSwitch(coordinator, zone_id) for zone_id in coordinator.zone_ids
    )
