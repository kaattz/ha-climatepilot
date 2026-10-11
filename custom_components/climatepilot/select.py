"""Select platform for ClimatePilot.

Two selects mirror the two control loops of the design: the system control mode
(``observe`` / ``coordinate`` / ``paused``) and the per-room mode
(``auto`` / ``comfort`` / ``eco`` / ``away`` / ``manual``).

Write access goes exclusively through the coordinator's async commands; the
entities never call Home Assistant services themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity

from .base import ClimatePilotEntity, ClimatePilotZoneEntity
from .const import (
    CONTROL_MODES,
    DOMAIN,
    ZONE_MODES,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator


class ControlModeSelect(ClimatePilotEntity, SelectEntity):
    """System control mode (design doc 10.2)."""

    _attr_options = CONTROL_MODES

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "control_mode")
        self._attr_translation_key = "control_mode"

    @property
    def current_option(self) -> str | None:
        mode = self.coordinator.control_mode
        return mode if mode in CONTROL_MODES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_control_mode(option)


class ZoneModeSelect(ClimatePilotZoneEntity, SelectEntity):
    """Per-room temperature mode (design doc 5.1)."""

    _attr_options = ZONE_MODES

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "zone_mode")
        self._attr_translation_key = "zone_mode"

    @property
    def current_option(self) -> str | None:
        mode = self.zone.mode
        return mode if mode in ZONE_MODES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_zone_mode(self._zone_id, option)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot select entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SelectEntity] = [ControlModeSelect(coordinator)]
    entities.extend(ZoneModeSelect(coordinator, zone_id) for zone_id in coordinator.zone_ids)

    async_add_entities(entities)
