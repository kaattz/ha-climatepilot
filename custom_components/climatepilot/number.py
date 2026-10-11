"""Number platform for ClimatePilot.

One number entity exposes the supply-temperature setpoint so an installer can
pin the boiler water temperature by hand.  The value is clamped twice: the
entity range follows the heat source's own reported limits when they are known
(design doc 6.3), and the coordinator re-clamps before writing.

The entity never talks to the boiler itself -- it delegates to
``coordinator.async_set_supply_temperature``, which also refuses the write while
the device semantics are unverified.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature

from .base import ClimatePilotDiagnosticEntity
from .const import (
    DEFAULT_SUPPLY_MAX,
    DEFAULT_SUPPLY_MIN,
    DOMAIN,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator


class SupplyTemperatureOverrideNumber(ClimatePilotDiagnosticEntity, NumberEntity):
    """Manual supply-temperature setpoint for the heat source."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX
    _attr_native_step = 0.5
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "supply_temperature_override")
        self._attr_translation_key = "supply_temperature_override"

    @property
    def _limits(self) -> tuple[float, float]:
        """Prefer the heat source's real limits over the built-in defaults."""
        capabilities = self.coordinator.state_machine.capabilities
        minimum = DEFAULT_SUPPLY_MIN
        maximum = DEFAULT_SUPPLY_MAX
        if capabilities is not None:
            if capabilities.min_temp is not None:
                minimum = capabilities.min_temp
            if capabilities.max_temp is not None:
                maximum = capabilities.max_temp
        if maximum <= minimum:
            return DEFAULT_SUPPLY_MIN, DEFAULT_SUPPLY_MAX
        return minimum, maximum

    @property
    def native_min_value(self) -> float:
        return self._limits[0]

    @property
    def native_max_value(self) -> float:
        return self._limits[1]

    @property
    def native_value(self) -> float | None:
        """The supply target currently in force.

        Reports the last value actually applied, falling back to the weather
        suggestion, and ``None`` when neither exists -- an unevaluated system
        must not pretend to hold a setpoint.
        """
        applied = self.coordinator.weather.last_applied
        if applied is not None:
            return applied
        compensation = self.coordinator.compensation
        return None if compensation is None else compensation.suggested

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_supply_temperature(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        capabilities = self.coordinator.state_machine.capabilities
        compensation = self.coordinator.compensation
        return {
            "boiler_entity": capabilities.entity_id if capabilities else None,
            "semantics_verified": bool(capabilities and capabilities.semantics_verified),
            "last_applied": self.coordinator.weather.last_applied,
            "suggested": None if compensation is None else compensation.suggested,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot number entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SupplyTemperatureOverrideNumber(coordinator)])
