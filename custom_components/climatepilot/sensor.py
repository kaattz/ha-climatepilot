"""Sensor platform for ClimatePilot.

Every entity reads the coordinator's public surface only (``docs/INTERFACES.md``
section 2.8) and carries a ``translation_key`` that exists in ``strings.json``
under ``entity.sensor``.  Names are never hardcoded so the frontend stays
localisable.

Two sensors publish raw engine constants which ``strings.json`` translates:
``heating_state`` (``FSM_*``) and ``setpoint_source`` (``SOURCE_*``).  Both fall
back to ``unknown`` instead of inventing a state when an engine has not produced
a decision yet, and every value that could be misread as "zero" is reported as
``None`` rather than a fabricated number (design doc 6.1, 11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.util import dt as dt_util

from .base import (
    ClimatePilotDiagnosticEntity,
    ClimatePilotEntity,
    ClimatePilotZoneEntity,
)
from .const import (
    DOMAIN,
    FEEDBACK_VALID,
    FSM_STATES,
    SOURCE_AUX_LOAD,
    SOURCE_COLD_ADJUSTMENT,
    SOURCE_FALLBACK,
    SOURCE_FIXED_MODE,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator

#: Order mirrors the ``state`` translations in ``strings.json``.
SETPOINT_SOURCES: list[str] = [
    SOURCE_MANUAL,
    SOURCE_FIXED_MODE,
    SOURCE_SCHEDULE,
    SOURCE_COLD_ADJUSTMENT,
    SOURCE_AUX_LOAD,
    SOURCE_FALLBACK,
]


def _burner_snapshot(coordinator: ClimatePilotCoordinator) -> dict[str, Any]:
    """Return the burner diagnostics block, or an empty mapping.

    The block only exists after the first evaluation cycle, so every consumer
    must tolerate its absence instead of assuming "no burns".
    """
    snapshot = coordinator.diagnostics_snapshot or {}
    burner = snapshot.get("burner")
    return burner if isinstance(burner, dict) else {}


def _burner_quality_is_valid(coordinator: ClimatePilotCoordinator) -> bool:
    """Whether the burner reading is trustworthy (design doc 8)."""
    return _burner_snapshot(coordinator).get("quality") == FEEDBACK_VALID


class _SystemDiagnosticSensor(ClimatePilotDiagnosticEntity, SensorEntity):
    """Diagnostic sensor attached to the system device."""


# ---------------------------------------------------------------------------
# System level
# ---------------------------------------------------------------------------


class HeatingStateSensor(ClimatePilotEntity, SensorEntity):
    """Heat-source FSM state (``OBSERVE``/``IDLE``/.../``DEGRADED``)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = FSM_STATES

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "heating_state")
        self._attr_translation_key = "heating_state"

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.heat_state
        # Never publish a value the enum translation cannot render.
        return state if state in FSM_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.coordinator.diagnostics_snapshot or {}
        attributes: dict[str, Any] = {
            "active_zones": snapshot.get("active_zones"),
            "unknown_zones": snapshot.get("unknown_zones"),
            "control_mode": snapshot.get("control_mode"),
        }
        decision = self.coordinator.heat_decision
        if decision is not None:
            attributes["decision"] = decision.decision
            attributes["target_mode"] = decision.target_mode
            attributes["target_supply"] = decision.target_supply
            attributes["reason_codes"] = list(decision.reason_codes)
        return attributes


class ActiveZonesSensor(ClimatePilotEntity, SensorEntity):
    """Number of zones with a *real* heating demand (design doc 6.1)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "active_zones")
        self._attr_translation_key = "active_zones"

    @property
    def native_value(self) -> int | None:
        """Zones asking for heat.

        Read from the diagnostics snapshot so the number always describes the
        same evaluation cycle as the rest of the panel.  ``None`` before the
        first cycle: unknown is not zero.
        """
        value = (self.coordinator.diagnostics_snapshot or {}).get("active_zones")
        return int(value) if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.coordinator.diagnostics_snapshot or {}
        return {
            "unknown_zones": snapshot.get("unknown_zones"),
            "zone_count": len(self.coordinator.zones),
        }


class SuggestedSupplyTempSensor(_SystemDiagnosticSensor):
    """Weather-compensation result (design doc 7)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "suggested_supply_temp")
        self._attr_translation_key = "suggested_supply_temp"

    @property
    def native_value(self) -> float | None:
        compensation = self.coordinator.compensation
        # No evaluation yet, or a stale reading kept the last safe value.
        return None if compensation is None else compensation.suggested

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        compensation = self.coordinator.compensation
        if compensation is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "outdoor": compensation.outdoor,
            "quality": compensation.quality,
            "should_apply": compensation.should_apply,
            "reason_codes": list(compensation.reason_codes),
            "last_applied": self.coordinator.weather.last_applied,
        }


class OutdoorTemperatureSensor(_SystemDiagnosticSensor):
    """Outdoor reading the compensation used."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "outdoor_temperature")
        self._attr_translation_key = "outdoor_temperature"

    @property
    def native_value(self) -> float | None:
        compensation = self.coordinator.compensation
        return None if compensation is None else compensation.outdoor

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        compensation = self.coordinator.compensation
        return {"quality": None if compensation is None else compensation.quality}


class SupplyTemperatureSensor(_SystemDiagnosticSensor):
    """Supply-temperature value reported by the heat source."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "supply_temperature")
        self._attr_translation_key = "supply_temperature"

    @property
    def native_value(self) -> float | None:
        return self.coordinator.supply_temperature_reading()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        compensation = self.coordinator.compensation
        return {
            "target": None if compensation is None else compensation.suggested,
            "last_applied": self.coordinator.weather.last_applied,
        }


class BurnerStarts24hSensor(_SystemDiagnosticSensor):
    """Burner starts within the trailing 24 hours."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "burner_starts_24h")
        self._attr_translation_key = "burner_starts_24h"

    @property
    def native_value(self) -> int | None:
        value = _burner_snapshot(self.coordinator).get("starts_24h")
        return int(value) if isinstance(value, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether domestic hot water is mixed into the count.

        Design doc 8: when the device only reports a generic flame state the
        statistics must be labelled "total burns, may include DHW" so a shower
        is never mistaken for floor-heating short cycling.
        """
        burner = _burner_snapshot(self.coordinator)
        return {
            "includes_dhw": burner.get("includes_dhw"),
            "starts_1h": burner.get("starts_1h"),
            "starts_total": burner.get("starts_total"),
            "heating_starts_24h": burner.get("heating_starts_24h"),
            "dhw_starts_24h": burner.get("dhw_starts_24h"),
            "burns_24h": burner.get("burns_24h"),
            "quality": burner.get("quality"),
        }


class LastBurnDurationSensor(_SystemDiagnosticSensor):
    """Duration of the most recent burn, in seconds."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "last_burn_duration")
        self._attr_translation_key = "last_burn_duration"

    @property
    def native_value(self) -> float | None:
        value = _burner_snapshot(self.coordinator).get("last_duration")
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        burner = _burner_snapshot(self.coordinator)
        return {
            "kind": burner.get("kind"),
            "includes_dhw": burner.get("includes_dhw"),
            "burning": burner.get("burning"),
            "current_duration": burner.get("current_duration"),
            "runtime_24h": burner.get("runtime_24h"),
            "short_cycles_24h": burner.get("short_cycles_24h"),
        }


# ---------------------------------------------------------------------------
# Zone level
# ---------------------------------------------------------------------------


class EffectiveSetpointSensor(ClimatePilotZoneEntity, SensorEntity):
    """The single arbitrated setpoint for a zone (design doc 4)."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "effective_setpoint")
        self._attr_translation_key = "effective_setpoint"

    @property
    def native_value(self) -> float | None:
        decision = self.coordinator.zone_decisions.get(self._zone_id)
        if decision is not None:
            return decision.effective
        return self.zone.effective_setpoint

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the value (design doc 10.3)."""
        decision = self.coordinator.zone_decisions.get(self._zone_id)
        attributes: dict[str, Any] = {
            "actual_setpoint": self.zone.actual_setpoint,
            "feedback_quality": self.zone.feedback_quality,
        }
        if decision is not None:
            attributes.update(
                {
                    "base": decision.base,
                    "source": decision.source,
                    "adjustment_delta": decision.adjustment_delta,
                    "clamped": decision.clamped,
                    "reason_codes": list(decision.reason_codes),
                    "explanation": decision.explanation,
                }
            )
        return attributes


class SetpointSourceSensor(ClimatePilotZoneEntity, SensorEntity):
    """Which actor owns the effective setpoint (``SOURCE_*``)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SETPOINT_SOURCES

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "setpoint_source")
        self._attr_translation_key = "setpoint_source"

    @property
    def native_value(self) -> str | None:
        source = self.coordinator.setpoint_source(self._zone_id)
        return source if source in SETPOINT_SOURCES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"reason_codes": self.coordinator.reason_codes_for(self._zone_id)}


class ZoneTemperatureSensor(ClimatePilotZoneEntity, SensorEntity):
    """Measured room temperature as reported by the thermostat."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "zone_temperature")
        self._attr_translation_key = "zone_temperature"

    @property
    def native_value(self) -> float | None:
        return self.zone.current_temperature

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        return {
            "hvac_action": zone.hvac_action,
            "hvac_mode": zone.hvac_mode,
            "feedback_quality": zone.feedback_quality,
            "tolerance": zone.tolerance,
        }


class OverrideRemainingSensor(ClimatePilotZoneEntity, SensorEntity):
    """Remaining time of a fixed-duration manual override."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "override_remaining")
        self._attr_translation_key = "override_remaining"

    @property
    def native_value(self) -> float | None:
        """Minutes left, or ``None`` when the override has no deadline.

        ``next_schedule`` and ``indefinite`` overrides carry no expiry, so they
        honestly report unknown instead of a made-up countdown.
        """
        override = self.zone.manual_override
        if override is None:
            return None
        remaining = override.remaining(dt_util.now())
        if remaining is None:
            return None
        return round(remaining.total_seconds() / 60.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        override = self.zone.manual_override
        if override is None:
            return {"active": False}
        return {
            "active": True,
            "value": override.value,
            "policy": override.policy,
            "until": override.until.isoformat() if override.until else None,
            "source_entity": override.source_entity,
        }


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot sensor entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        HeatingStateSensor(coordinator),
        ActiveZonesSensor(coordinator),
        SuggestedSupplyTempSensor(coordinator),
        OutdoorTemperatureSensor(coordinator),
        SupplyTemperatureSensor(coordinator),
        BurnerStarts24hSensor(coordinator),
        LastBurnDurationSensor(coordinator),
    ]

    for zone_id in coordinator.zone_ids:
        entities.extend(
            (
                EffectiveSetpointSensor(coordinator, zone_id),
                SetpointSourceSensor(coordinator, zone_id),
                ZoneTemperatureSensor(coordinator, zone_id),
                OverrideRemainingSensor(coordinator, zone_id),
            )
        )

    async_add_entities(entities)
