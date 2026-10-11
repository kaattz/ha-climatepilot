"""Binary sensor platform for ClimatePilot.

These entities answer "is this condition true right now?" for the dashboard and
for automations:

* ``manual_override`` / ``heat_request`` -- per room (design doc 4, 6.1);
* ``short_cycle_alert`` / ``degraded`` -- system diagnostics (design doc 8, 11);
* ``overheating`` / ``underheating`` -- per room comfort faults (V0.2, section 3.2);
* ``probe_fault`` -- a *configured* external pipe probe became unusable (V0.3).
  Not a diagnostic entity: it is ``off`` when no probe is installed, because
  "not fitted" is not "broken" and a permanently-on flag teaches users to
  ignore it.

Unknown is never reported as "off": when the coordinator cannot tell whether a
zone requests heat, when the burner reading is not trustworthy, or when the
comfort analyser has no samples for a room yet, the entity reports ``None``
(``unknown``) or becomes unavailable instead of claiming the safe-looking false
value.

The two comfort flags read the *engine verdict* rather than re-deriving the
comparison from the current temperature.  A single warm reading is not
"overheating" -- a leaky valve and a sunlit afternoon are indistinguishable at
that instant; only the dwell time the engine accumulates separates them, so the
flag must come from the engine to stay consistent with ``sensor.comfort_verdict``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .base import (
    ClimatePilotDiagnosticEntity,
    ClimatePilotEntity,
    ClimatePilotZoneEntity,
)
from .const import (
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    COMFORT_UNKNOWN,
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


class ProbeFaultBinarySensor(ClimatePilotEntity, BinarySensorEntity):
    """Whether a *configured* external pipe probe has become unusable.

    ``PROBLEM`` device class, but deliberately **not** a diagnostic entity: a
    probe that fell off the pipe is something the occupant should see, not
    something to file behind the diagnostic filter.

    The semantics matter more than the plumbing, and they are:

    * **no probe configured -> ``off``.** Most installations have no external
      probes at all. Reporting a permanent fault would train the user to ignore
      the flag, so an absent probe is not a fault.
    * a *configured* probe that is missing, stale, frozen, implausible (the
      DS18B20 85 °C sentinel) or out of range -> ``on``.

    The coordinator owns that distinction in ``probe_fault``; this entity only
    reports it, so the policy lives in one place.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "probe_fault")
        self._attr_translation_key = "probe_fault"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.probe_fault)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name which side failed and why, and repeat the "not power" caveat.

        A failed probe degrades diagnostics only - it never changes a write - so
        the entity states that outright rather than leaving the user to wonder
        whether the heating is affected.
        """
        pair = self.coordinator.probes
        attributes: dict[str, Any] = {
            "configured": bool(getattr(self.coordinator, "probes_configured", False)),
            "affects_heating": False,
            "is_power_measurement": False,
        }
        if pair is None:
            attributes["evaluated"] = False
            return attributes
        attributes.update(
            {
                "evaluated": True,
                "supply_quality": pair.supply.quality,
                "return_quality": pair.return_.quality,
                "both_usable": pair.both_usable,
                "supply_entity": pair.supply.entity_id or None,
                "return_entity": pair.return_.entity_id or None,
                "reason_codes": list(pair.reason_codes),
            }
        )
        return attributes


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


class _ComfortBinarySensor(ClimatePilotZoneEntity, BinarySensorEntity):
    """Shared plumbing for the over/underheating flags.

    Both flags answer one question -- "did this room hold a comfort fault long
    enough for the engine to call it one?" -- and differ only in which verdict
    they watch, so the tolerance rules live here once.
    """

    #: The ``COMFORT_*`` verdict that turns this flag on.
    _verdict: str = COMFORT_OK
    #: Which ``ComfortVerdict`` fields to publish as the explanation.
    _attribute_keys: tuple[str, ...] = ()

    def _comfort_verdict(self) -> Any:
        """The analyser's verdict for this room, or ``None`` if unsampled."""
        return self.coordinator.comfort.get(self._zone_id)

    @property
    def available(self) -> bool:
        """Unknown is not a fault state.

        The engine reports ``COMFORT_UNKNOWN`` both before the first sample and
        afterwards whenever the room's temperature or target goes missing, so a
        flag that stayed available would read as "checked, and fine" while
        nothing had been checked at all.

        ``is_on`` still publishes ``None`` rather than raising while unavailable:
        Home Assistant writes the state once more as it tears an entity down, and
        a property that only works while available would turn that write into a
        logged error.
        """
        if not super().available:
            return False
        verdict = self._comfort_verdict()
        return verdict is not None and verdict.verdict != COMFORT_UNKNOWN

    @property
    def is_on(self) -> bool | None:
        """``None`` whenever there is no verdict that could be trusted."""
        verdict = self._comfort_verdict()
        if verdict is None or verdict.verdict == COMFORT_UNKNOWN:
            return None
        return verdict.verdict == self._verdict

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show the measurement the verdict rests on, not just the verdict.

        ``dwell_seconds`` is what separates this flag from a bare thermometer
        comparison, and it depends on the analyser's configuration.  It is
        reported as ``None`` rather than guessed when the engine does not
        publish it -- an invented threshold would be worse than a missing one.
        """
        verdict = self._comfort_verdict()
        attributes: dict[str, Any] = {
            "verdict": None if verdict is None else verdict.verdict,
            "dwell_seconds": _dwell_seconds(self.coordinator, self._verdict),
        }
        if verdict is None:
            attributes["evaluated"] = False
            return attributes
        attributes["evaluated"] = True
        detail = verdict.as_dict()
        attributes.update({key: detail.get(key) for key in self._attribute_keys})
        return attributes


def _dwell_seconds(coordinator: ClimatePilotCoordinator, verdict: str) -> float | None:
    """How long the fault must persist before the engine raises it.

    Read from the analyser when it exposes its settings, otherwise reported as
    unknown.  The flag never falls back to a module default, because a user who
    retuned the dwell time would then be shown a threshold that is not in force.
    """
    analyser = getattr(coordinator, "comfort_analyser", None)
    if analyser is None:
        return None
    if verdict == COMFORT_OVERHEATING:
        value = getattr(analyser, "overheat_dwell", None)
    else:
        value = getattr(analyser, "underheat_dwell", None)
    return None if value is None else float(value)


class OverheatingBinarySensor(_ComfortBinarySensor):
    """The room has held a temperature above its target for long enough to matter.

    Not diagnostic: a permanently warm room is something the occupant wants to
    see, not something to file away behind a diagnostic filter.
    """

    _verdict = COMFORT_OVERHEATING
    _attribute_keys = (
        "max_over",
        "overheated_seconds",
        "average_deviation",
        "samples",
        "reason_codes",
    )

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "overheating")
        self._attr_translation_key = "overheating"


class UnderheatingBinarySensor(_ComfortBinarySensor):
    """The room is below its target *and* genuinely asking for heat.

    ``heat_request`` alone only says a valve is open; this flag says the demand
    is not being met, which is the symptom worth acting on (a capacity or
    hydraulics problem rather than a setpoint mistake).
    """

    _verdict = COMFORT_UNDERHEATING
    _attribute_keys = (
        "max_under",
        "underheated_seconds",
        "average_deviation",
        "samples",
        "reason_codes",
    )

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "underheating")
        self._attr_translation_key = "underheating"


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
        ProbeFaultBinarySensor(coordinator),
    ]

    for zone_id in coordinator.zone_ids:
        entities.extend(
            (
                ManualOverrideBinarySensor(coordinator, zone_id),
                HeatRequestBinarySensor(coordinator, zone_id),
                OverheatingBinarySensor(coordinator, zone_id),
                UnderheatingBinarySensor(coordinator, zone_id),
            )
        )

    async_add_entities(entities)
