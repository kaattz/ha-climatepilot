"""Switch platform for ClimatePilot.

Two kinds of switch live here:

* one per room, enabling it as the bounded auxiliary heat load (design doc 5.3);
* one system-wide heating-season gate, so the integration can be left installed
  and dormant outside the heating season.

The per-room switch is only a *permission*: whether the load is actually applied
is decided every cycle by the coordinator, which requires other zones to have a
real demand, no manual override on this room, and a temperature below the
configured cap -- so the load can never become the old "current + 2 C" runaway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity

from .base import ClimatePilotEntity, ClimatePilotZoneEntity
from .const import (
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
    DOMAIN,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator


def season_to_control_mode(season_on: bool) -> str:
    """Map the season switch to a control mode.

    ON deliberately selects ``observe`` rather than ``coordinate``. Turning the
    season on resumes monitoring and suggestions; it must not silently begin
    writing to the room thermostats, because that skips the review step observe
    mode exists for and changes behaviour on a live heating system without a
    fresh confirmation. Reaching ``coordinate`` stays an explicit choice in the
    control-mode select.
    """
    return CONTROL_MODE_OBSERVE if season_on else CONTROL_MODE_PAUSED


def control_mode_to_season(mode: str) -> bool:
    """Derive the switch state from the control mode.

    The switch stores nothing of its own. Two independent flags for the same
    decision would eventually disagree, and the reader would have no way to tell
    which one won.
    """
    return mode != CONTROL_MODE_PAUSED


class HeatingSeasonSwitch(ClimatePilotEntity, SwitchEntity):
    """Whether the heating season is running.

    Off means "off season": the integration stops coordinating and withdraws its
    own heat request, but it does not touch the boiler's own power state - the
    state machine only turns off what it turned on.
    """

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "heating_season")
        self._attr_translation_key = "heating_season"

    @property
    def is_on(self) -> bool:
        return control_mode_to_season(self.coordinator.control_mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control_mode(season_to_control_mode(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control_mode(season_to_control_mode(False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose why the season reads the way it does.

        ``writes_enabled`` is the property that actually gates every write, so
        showing it next to the switch removes any doubt about whether "season
        on" means the integration is currently controlling anything.
        """
        return {
            "control_mode": self.coordinator.control_mode,
            "writes_enabled": self.coordinator.writes_enabled,
        }


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

    entities: list[SwitchEntity] = [HeatingSeasonSwitch(coordinator)]
    entities.extend(
        AuxiliaryLoadSwitch(coordinator, zone_id) for zone_id in coordinator.zone_ids
    )
    async_add_entities(entities)
