"""Button platform for ClimatePilot.

Buttons are deliberately thin: each one calls exactly one coordinator command
and lets the coordinator perform the arbitration, persistence and refresh.

* ``resume_auto`` -- drop a room's manual override (design doc 4.3);
* ``reassess`` -- re-run the evaluation cycle without waiting for the audit
  interval (design doc 4.7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity

from .base import ClimatePilotEntity, ClimatePilotZoneEntity
from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator


class ResumeAutoButton(ClimatePilotZoneEntity, ButtonEntity):
    """Clear the manual override of one room and return to its schedule."""

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "resume_auto")
        self._attr_translation_key = "resume_auto"

    async def async_press(self) -> None:
        await self.coordinator.async_resume_auto(self._zone_id)


class ReassessButton(ClimatePilotEntity, ButtonEntity):
    """Trigger an immediate arbitration and heat-source evaluation."""

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "reassess")
        self._attr_translation_key = "reassess"

    async def async_press(self) -> None:
        await self.coordinator.async_reassess()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot button entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[ButtonEntity] = [ReassessButton(coordinator)]
    entities.extend(ResumeAutoButton(coordinator, zone_id) for zone_id in coordinator.zone_ids)

    async_add_entities(entities)
