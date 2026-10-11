"""Shared entity base classes for ClimatePilot.

Every platform module (``sensor``, ``select``, ``number``, ...) builds on these
so device registration, unique ids and availability stay consistent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER, MODEL
from .models import ZoneState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .coordinator import ClimatePilotCoordinator


class ClimatePilotEntity(CoordinatorEntity["ClimatePilotCoordinator"]):
    """Base entity attached to the ClimatePilot system device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ClimatePilotCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.system_id}_{key}"
        self._key = key

    @property
    def device_info(self) -> DeviceInfo:
        """Return the system-level device this entity belongs to."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.system_id)},
            name=self.coordinator.name or DEFAULT_NAME,
            manufacturer=MANUFACTURER,
            model=MODEL,
            entry_type=None,
        )


class ClimatePilotZoneEntity(ClimatePilotEntity):
    """Base entity for entities owned by a single heating zone."""

    def __init__(
        self,
        coordinator: ClimatePilotCoordinator,
        zone_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, f"{zone_id}_{key}")
        self._zone_id = zone_id

    @property
    def zone(self) -> ZoneState:
        """Return the live zone state.

        Raises:
            KeyError: when the zone was removed from the options.
        """
        return self.coordinator.zones[self._zone_id]

    @property
    def device_info(self) -> DeviceInfo:
        """Zones are sub-devices of the system device."""
        zone = self.coordinator.zones.get(self._zone_id)
        name = zone.name if zone else self._zone_id
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.system_id}_{self._zone_id}")},
            name=name,
            manufacturer=MANUFACTURER,
            model="Heating Zone",
            via_device=(DOMAIN, self.coordinator.system_id),
        )

    @property
    def available(self) -> bool:
        """A zone entity is available while its climate entity reports state."""
        if not super().available:
            return False
        zone = self.coordinator.zones.get(self._zone_id)
        return zone is not None


class ClimatePilotDiagnosticEntity(ClimatePilotEntity):
    """Base entity for diagnostic entities."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
