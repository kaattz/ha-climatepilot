"""Optional whole-home climate entity for ClimatePilot.

This is **not** a copy of the KNX room thermostats - duplicating those would
create a second way to drive the same actuator, which design doc 10.2 explicitly
warns against. Instead it is a single system-level view:

* current temperature = the average of the zones that report one;
* target temperature = the highest arbitrated zone target (the system-wide
  "hotter room wins" reading), and setting it applies to every non-overridden
  zone through the normal arbitration path;
* hvac_action = ``heating`` while the heat source is actually heating;
* preset modes map onto the system control mode.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base import ClimatePilotEntity
from .const import (
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
    DOMAIN,
    FSM_HEATING,
    FSM_PREPARE,
    FSM_SATISFIED,
)
from .coordinator import ClimatePilotCoordinator

PRESET_TO_CONTROL: dict[str, str] = {
    "observe": CONTROL_MODE_OBSERVE,
    "coordinate": CONTROL_MODE_COORDINATE,
    "paused": CONTROL_MODE_PAUSED,
}
CONTROL_TO_PRESET: dict[str, str] = {v: k for k, v in PRESET_TO_CONTROL.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the whole-home climate entity."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ClimatePilotSystemThermostat(coordinator)])


class ClimatePilotSystemThermostat(ClimatePilotEntity, ClimateEntity):
    """System-level heating control and status."""

    _attr_translation_key = "system_thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_min_temp = 10.0
    _attr_max_temp = 30.0
    _attr_target_temperature_step = 0.5

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "system_thermostat")

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode.HEAT, HVACMode.OFF]

    @property
    def preset_modes(self) -> list[str]:
        return list(PRESET_TO_CONTROL)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def current_temperature(self) -> float | None:
        readings = [
            zone.current_temperature
            for zone in self.coordinator.zones.values()
            if zone.current_temperature is not None
        ]
        if not readings:
            return None
        return round(sum(readings) / len(readings), 1)

    @property
    def target_temperature(self) -> float | None:
        targets = [
            zone.effective_setpoint
            for zone in self.coordinator.zones.values()
            if zone.effective_setpoint is not None
        ]
        if not targets:
            return None
        return max(targets)

    @property
    def hvac_mode(self) -> HVACMode:
        if self.coordinator.control_mode == CONTROL_MODE_PAUSED:
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        state = self.coordinator.heat_state
        if state in (FSM_HEATING, FSM_PREPARE):
            return HVACAction.HEATING
        if state == FSM_SATISFIED:
            return HVACAction.IDLE
        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        return CONTROL_TO_PRESET.get(self.coordinator.control_mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the explainability surface promised by design doc 10.3."""
        coordinator = self.coordinator
        compensation = coordinator.compensation
        decision = coordinator.heat_decision
        attributes: dict[str, Any] = {
            "control_mode": coordinator.control_mode,
            "heat_source_state": coordinator.heat_state,
            "active_zones": coordinator.active_zone_count,
            "unknown_zones": coordinator.unknown_zone_count,
            "zone_reason_codes": {
                zone_id: list(zone.reason_codes) for zone_id, zone in coordinator.zones.items()
            },
            "zone_sources": {
                zone_id: coordinator.setpoint_source(zone_id) for zone_id in coordinator.zones
            },
        }
        if compensation is not None:
            attributes["suggested_supply_temperature"] = compensation.suggested
            attributes["outdoor_temperature"] = compensation.outdoor
            attributes["weather_quality"] = compensation.quality
            attributes["weather_reason_codes"] = list(compensation.reason_codes)
        if decision is not None:
            attributes["heat_source_decision"] = decision.decision
            attributes["heat_source_reason_codes"] = list(decision.reason_codes)
        capabilities = coordinator.state_machine.capabilities
        attributes["heat_source_semantics_verified"] = bool(
            capabilities and capabilities.semantics_verified
        )
        attributes["min_flow_verified"] = bool(capabilities and capabilities.min_flow_verified)
        return attributes

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Apply a target to every zone that is not under a manual override."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        for zone in self.coordinator.zones.values():
            if zone.manual_override is not None:
                # A system-wide command must not silently steal a room the user
                # has explicitly taken over.
                continue
            zone.base_setpoint = float(temperature)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_set_control_mode(CONTROL_MODE_PAUSED)
        else:
            await self.coordinator.async_set_control_mode(CONTROL_MODE_COORDINATE)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        mode = PRESET_TO_CONTROL.get(preset_mode)
        if mode is not None:
            await self.coordinator.async_set_control_mode(mode)

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.zones)

    @property
    def min_temp(self) -> float:
        lows = [zone.min_temp for zone in self.coordinator.zones.values()]
        return min(lows) if lows else self._attr_min_temp

    @property
    def max_temp(self) -> float:
        highs = [zone.max_temp for zone in self.coordinator.zones.values()]
        return max(highs) if highs else self._attr_max_temp
