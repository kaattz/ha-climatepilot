"""Binary sensor platform for ClimatePilot.

These entities answer "is this condition true right now?" for the dashboard and
for automations:

* ``manual_override`` / ``heat_request`` -- per room (design doc 4, 6.1);
* ``short_cycle_alert`` / ``degraded`` -- system diagnostics (design doc 8, 11).

Unknown is never reported as "off": when the coordinator cannot tell whether a
zone requests heat, or when the burner reading is not trustworthy, the entity
reports ``None`` (``unknown``) or becomes unavailable instead of claiming the
safe-looking false value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .base import (
    ClimatePilotDiagnosticEntity,
    ClimatePilotZoneEntity,
)
from .const import (
    DOMAIN,
    FEEDBACK_UNAVAILABLE,
    FSM_DEGRADED,
    HVAC_ACTION_HEATING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import ClimatePilotCoordinator

_UNREADABLE = (STATE_UNAVAILABLE, STATE_UNKNOWN, None)

#: FSM states that mean the coordinator is not running normally.
_DEGRADED_STATES = (FSM_DEGRADED,)


def _burner_block(coordinator: ClimatePilotCoordinator) -> dict[str, Any]:
    """Return the burner diagnostics block, or an empty mapping."""
    snapshot = coordinator.diagnostics_snapshot or {}
    burner = snapshot.get("burner")
    return burner if isinstance(burner, dict) else {}


class _SystemDiagnosticBinarySensor(ClimatePilotDiagnosticEntity, BinarySensorEntity):
    """Diagnostic binary sensor attached to the system device."""


# ---------------------------------------------------------------------------
# System level
# ---------------------------------------------------------------------------


class ShortCycleAlertBinarySensor(_SystemDiagnosticBinarySensor):
    """Diagnostic-only short-cycling alert (design doc 8).

    This never drives a boiler write; it only surfaces the statistics.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "short_cycle_alert")
        self._attr_translation_key = "short_cycle_alert"

    @property
    def available(self) -> bool:
        """Only meaningful while the burner reading is trustworthy.

        A lost burner state must not be shown as "no short cycling"
        (design doc 8, 11).
        """
        if not super().available:
            return False
        return _burner_block(self.coordinator).get("quality") == "valid"

    @property
    def is_on(self) -> bool | None:
        value = _burner_block(self.coordinator).get("short_cycle_alert")
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        burner = _burner_block(self.coordinator)
        return {
            "short_cycles_24h": burner.get("short_cycles_24h"),
            "short_cycle_seconds": burner.get("short_cycle_seconds"),
            "includes_dhw": burner.get("includes_dhw"),
            "quality": burner.get("quality"),
            "histogram": burner.get("histogram"),
        }


class DegradedBinarySensor(_SystemDiagnosticBinarySensor):
    """Whether the coordinator is running in a restricted state."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "degraded")
        self._attr_translation_key = "degraded"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.degraded)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the specific fault so the user knows what to inspect."""
        snapshot = self.coordinator.diagnostics_snapshot or {}
        compensation = self.coordinator.compensation
        heat_state = self.coordinator.heat_state
        reasons: list[str] = []
        if heat_state in _DEGRADED_STATES:
            reasons.append("heat_source_degraded")
        unknown_zones = snapshot.get("unknown_zones") or 0
        if unknown_zones:
            reasons.append("zone_state_unknown")
        if compensation is not None and compensation.quality != "valid":
            reasons.append("weather_data_unusable")
        return {
            "heat_state": heat_state,
            "unknown_zones": unknown_zones,
            "control_mode": snapshot.get("control_mode"),
            "reasons": reasons,
        }


# ---------------------------------------------------------------------------
# Zone level
# ---------------------------------------------------------------------------


class ManualOverrideBinarySensor(ClimatePilotZoneEntity, BinarySensorEntity):
    """Whether a user-set target currently outranks every automatic source."""

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "manual_override")
        self._attr_translation_key = "manual_override"

    @property
    def is_on(self) -> bool:
        return self.zone.manual_override is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        override = self.zone.manual_override
        if override is None:
            return {}
        return {
            "value": override.value,
            "policy": override.policy,
            "until": override.until.isoformat() if override.until else None,
            "source_entity": override.source_entity,
        }


class HeatRequestBinarySensor(ClimatePilotZoneEntity, BinarySensorEntity):
    """Whether the zone actually asks for heat (design doc 6.1).

    ``heat`` mode alone is **not** a request; only a reported ``heating``
    action is.  An unreadable thermostat reports ``unknown``, never "off".
    """

    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "heat_request")
        self._attr_translation_key = "heat_request"

    @property
    def is_on(self) -> bool | None:
        zone = self.zone
        if zone.feedback_quality == FEEDBACK_UNAVAILABLE:
            return None
        if zone.hvac_mode in _UNREADABLE:
            return None
        if zone.hvac_action is None:
            # No trustworthy action feedback: unknown, not "no demand".
            return None
        return zone.hvac_action == HVAC_ACTION_HEATING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self.zone
        return {
            "hvac_mode": zone.hvac_mode,
            "hvac_action": zone.hvac_action,
            "supports_action": zone.supports_action,
            "feedback_quality": zone.feedback_quality,
        }


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the ClimatePilot binary sensor entities."""
    coordinator: ClimatePilotCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[BinarySensorEntity] = [
        ShortCycleAlertBinarySensor(coordinator),
        DegradedBinarySensor(coordinator),
    ]

    for zone_id in coordinator.zone_ids:
        entities.extend(
            (
                ManualOverrideBinarySensor(coordinator, zone_id),
                HeatRequestBinarySensor(coordinator, zone_id),
            )
        )

    async_add_entities(entities)
