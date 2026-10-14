"""Sensor platform for ClimatePilot.

Every entity reads the coordinator's public surface only (``docs/INTERFACES.md``
section 2.8) and carries a ``translation_key`` that exists in ``strings.json``
under ``entity.sensor``.  Names are never hardcoded so the frontend stays
localisable.

Several sensors publish raw engine constants which ``strings.json`` translates:
``heating_state`` (``FSM_*``), ``setpoint_source`` (``SOURCE_*``) and the V0.2
trio ``comfort_verdict`` (``COMFORT_*``), ``comfort_advice`` (``ADVICE_*``) and
``cycling_quality`` (``QUALITY_*``).  All of them fall back to ``unknown``
instead of inventing a state when an engine has not produced a decision yet, and
every value that could be misread as "zero" is reported as ``None`` rather than
a fabricated number (design doc 6.1, 11).

The V0.2 diagnostic sensors carry the engine output that explains *why* the
system behaves as it does: the zone-combination report on
``combinations_24h`` and the comfort verdict on ``comfort_verdict`` expose their
engine's ``as_dict()`` so the answer travels with the value (design doc 10.3).

V0.3 adds six system-level sensors for the optional external pipe probes: the
return temperature and ΔT readings, plus four diagnostic verdicts
(``delta_t_quality``, ``condensing_regime``, ``hydraulic_verdict``,
``curve_calibration_advice``). They are strictly observation-only - a failed
probe never influences a write - and they publish ``None`` rather than a
substituted value whenever a probe is absent or untrustworthy.
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
    ADVICE_CHECK_HYDRAULICS,
    ADVICE_CHECK_SCHEDULE,
    ADVICE_LOWER_SUPPLY,
    ADVICE_NONE,
    ADVICE_RAISE_SUPPLY,
    CALIBRATION_INSUFFICIENT_DATA,
    CALIBRATION_LOWER_CURVE,
    CALIBRATION_NONE,
    CALIBRATION_RAISE_CURVE,
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    COMFORT_UNKNOWN,
    CONDENSING_LIKELY,
    CONDENSING_UNKNOWN,
    CONDENSING_UNLIKELY,
    DEFAULT_COMBINATION_WINDOW,
    DOMAIN,
    ENERGY_UNIT_DEGREE_DAYS,
    ENERGY_UNIT_GAS,
    ENERGY_UNIT_GAS_PER_DEGREE_DAY,
    FEEDBACK_VALID,
    FSM_STATES,
    HYDRAULIC_HIGH_FLOW,
    HYDRAULIC_LOW_FLOW,
    HYDRAULIC_OK,
    HYDRAULIC_UNBALANCED,
    HYDRAULIC_UNKNOWN,
    PROBE_QUALITIES,
    QUALITY_FAIR,
    QUALITY_GOOD,
    QUALITY_POOR,
    QUALITY_UNKNOWN,
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

#: Order mirrors the ``state`` translations in ``strings.json`` for the V0.2
#: enum sensors.  Publishing a value outside its list would make Home Assistant
#: raise instead of render, so every ``native_value`` below checks membership.
COMFORT_VERDICTS: list[str] = [
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    COMFORT_UNKNOWN,
]

COMFORT_ADVICE: list[str] = [
    ADVICE_LOWER_SUPPLY,
    ADVICE_RAISE_SUPPLY,
    ADVICE_CHECK_HYDRAULICS,
    ADVICE_CHECK_SCHEDULE,
    ADVICE_NONE,
]

CYCLING_QUALITIES: list[str] = [
    QUALITY_GOOD,
    QUALITY_FAIR,
    QUALITY_POOR,
    QUALITY_UNKNOWN,
]

#: V0.3 probe/delta-T verdict vocabularies (docs/INTERFACES_V0.3.md section 3).
#: ``PROBE_QUALITIES`` is imported rather than restated: the engine owns the
#: severity order and duplicating it here would let the two drift apart.
CONDENSING_REGIMES: list[str] = [
    CONDENSING_LIKELY,
    CONDENSING_UNLIKELY,
    CONDENSING_UNKNOWN,
]

HYDRAULIC_VERDICTS: list[str] = [
    HYDRAULIC_OK,
    HYDRAULIC_LOW_FLOW,
    HYDRAULIC_HIGH_FLOW,
    HYDRAULIC_UNBALANCED,
    HYDRAULIC_UNKNOWN,
]

CALIBRATION_ADVICE: list[str] = [
    CALIBRATION_NONE,
    CALIBRATION_RAISE_CURVE,
    CALIBRATION_LOWER_CURVE,
    CALIBRATION_INSUFFICIENT_DATA,
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
# System level - V0.2 demand combinations and combustion quality
# ---------------------------------------------------------------------------


class DominantZoneCombinationSensor(ClimatePilotEntity, SensorEntity):
    """Which rooms demanded heat together for most of the window.

    Deliberately **not** a diagnostic entity: "the living room and the master
    bedroom are the pair that runs this house" is an everyday insight, and a
    diagnostic category hides it behind the entity filter on the dashboard.
    """

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "dominant_zone_combination")
        self._attr_translation_key = "dominant_zone_combination"

    @property
    def native_value(self) -> str | None:
        """The combination key, e.g. ``living_room+master_bedroom``.

        ``none`` is a real answer here (``const.COMBINATION_NONE``): it means the
        window held no heating demand at all.  ``None`` is reserved for "there is
        no report yet", which is a different claim.
        """
        report = self.coordinator.combinations
        return None if report is None else report.dominant_key

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain the ranking behind the key (design doc 10.3).

        ``dominant_threshold`` / ``has_dominant_mode`` are read out of the
        engine's own export rather than off the report object: they are *why*
        the key should be trusted, so they belong to the explanation, and
        ``as_dict()`` is the frozen surface for explanations.
        """
        report = self.coordinator.combinations
        if report is None:
            return {"evaluated": False}
        detail = report.as_dict()
        return {
            "evaluated": True,
            "dominant_share": round(report.dominant_share, 4),
            "dominant_threshold": detail.get("dominant_threshold"),
            # False means the demand time is spread over several patterns; the
            # key is still the leader, but it is not "the" combination.
            "has_dominant_mode": detail.get("has_dominant_mode"),
            "current_key": report.current_key,
            "current_seconds": round(report.current_seconds, 1),
            "distinct_count": report.distinct_count,
            "total_demand_seconds": round(report.total_demand_seconds, 1),
        }


class ZoneCombinationCountSensor(ClimatePilotEntity, SensorEntity):
    """How many different demand combinations the window contained.

    Not diagnostic either: it answers "is my heating running one stable pattern
    or scattering across many?", which is exactly the kind of thing the
    zone-combination analysis exists to surface.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "zone_combination_count")
        self._attr_translation_key = "zone_combination_count"

    @property
    def native_value(self) -> int | None:
        report = self.coordinator.combinations
        return None if report is None else report.distinct_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.coordinator.combinations
        if report is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "dominant_key": report.dominant_key,
            # Groups in ``stats``, which - unlike ``distinct_count`` - includes
            # the empty (no-demand) set.
            "total_groups": report.as_dict().get("total_groups"),
            "combinations": [stat.key for stat in report.stats],
        }


class Combinations24hSensor(_SystemDiagnosticSensor):
    """Demand time accumulated inside the combination window, in seconds.

    The state is *demand time*, not the combination count: ``zone_combination_count``
    already publishes the count, and two entities repeating one number is noise
    on a dashboard.  Demand time is the quantity that graphs and can back a
    statistic, while the full breakdown lives in the attributes.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "combinations_24h")
        self._attr_translation_key = "combinations_24h"

    @property
    def native_value(self) -> float | None:
        report = self.coordinator.combinations
        return None if report is None else report.total_demand_seconds

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Carry the whole report, plus the same figure in minutes.

        The engine's ``as_dict()`` ships verbatim so the entity and the
        diagnostics export can never disagree about what was measured.  The
        minutes copy saves every user (and every template) the division.
        """
        report = self.coordinator.combinations
        if report is None:
            return {"evaluated": False}
        attributes = dict(report.as_dict())
        attributes["evaluated"] = True
        attributes["total_demand_minutes"] = round(report.total_demand_seconds / 60.0, 1)
        attributes["window_seconds"] = DEFAULT_COMBINATION_WINDOW
        return attributes


class CyclingQualitySensor(_SystemDiagnosticSensor):
    """Combustion quality grade over the diagnostics window (``QUALITY_*``).

    Diagnostic because it grades the *boiler's* behaviour from burner
    statistics - it is troubleshooting evidence, not a reading of the house.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CYCLING_QUALITIES

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "cycling_quality")
        self._attr_translation_key = "cycling_quality"

    @property
    def native_value(self) -> str | None:
        quality = self.coordinator.cycling_quality
        return quality if quality in CYCLING_QUALITIES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the reasoning and the per-grade counts it was based on."""
        burner = _burner_snapshot(self.coordinator)
        return {
            "reason": self.coordinator.cycling_quality_reason,
            "grade_heating_24h": burner.get("grade_heating_24h"),
            # DHW burns are graded too, but they are explicitly excluded from
            # ``cycling_quality`` - a shower must not make the heating look bad.
            "grade_dhw_24h": burner.get("grade_dhw_24h"),
            "heating_starts_24h": burner.get("heating_starts_24h"),
            "dhw_starts_24h": burner.get("dhw_starts_24h"),
            "includes_dhw": burner.get("includes_dhw"),
            "quality": burner.get("quality"),
        }


class UnexplainedBurns24hSensor(_SystemDiagnosticSensor):
    """Ignitions in the window with no zone demanding heat (design doc 8)."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "unexplained_burns_24h")
        self._attr_translation_key = "unexplained_burns_24h"

    @property
    def native_value(self) -> int | None:
        """The count, or ``None`` while the burner reading is untrustworthy.

        Zero unexplained burns and "we cannot see the burner" must not look the
        same on a dashboard, so a reading that is not ``valid`` reports unknown.
        """
        if not _burner_quality_is_valid(self.coordinator):
            return None
        return self.coordinator.unexplained_burns

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        burner = _burner_snapshot(self.coordinator)
        return {
            # V0.1 spelling of the same measurement, kept so an existing
            # automation reading this attribute does not break.
            "burns_without_demand_24h": burner.get("burns_without_demand_24h"),
            "demand_active": burner.get("demand_active"),
            "burns_24h": burner.get("burns_24h"),
            "heating_starts_24h": burner.get("heating_starts_24h"),
            "dhw_starts_24h": burner.get("dhw_starts_24h"),
            "cycling_quality": burner.get("cycling_quality"),
            "quality": burner.get("quality"),
        }


# ---------------------------------------------------------------------------
# System level - V0.3 external pipe probes and delta-T diagnostics
#
# HARD CONSTRAINT (docs/INTERFACES_V0.3.md section 1): a temperature difference
# is **not** heat power. These entities describe flow and condensing *regimes*
# indirectly; none of them may be read as thermal output or efficiency. The
# engines stamp ``is_power_measurement = False`` and
# ``REASON_NOT_A_POWER_MEASUREMENT`` into their exports and the entities carry
# that straight through to the UI.
#
# The probe entities are monitoring/optimisation inputs only: a failed probe
# never changes a write decision, so none of them can make the heating worse.
#
# "No probe installed" is not "the entity is broken": both readings below
# publish ``None`` (state ``unknown``) while ``available`` stays True, so an
# installation without external probes shows a calm unknown rather than a fault.
# ---------------------------------------------------------------------------


def _probe_assessment(coordinator: ClimatePilotCoordinator, side: str) -> Any:
    """Return one side of the probe pair, or ``None`` before the first cycle."""
    pair = coordinator.probes
    if pair is None:
        return None
    return pair.supply if side == "supply" else pair.return_


class ReturnTemperatureSensor(ClimatePilotEntity, SensorEntity):
    """Return-pipe temperature from the optional external probe.

    ``device_class`` is plain ``TEMPERATURE`` (not ``TEMPERATURE_DELTA``): this
    is an absolute pipe temperature, so a unit conversion to °F must apply. It
    is also *not* the boiler's own return reading - it is the strap-on probe
    (design doc 12, V0.3).
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "return_temperature")
        self._attr_translation_key = "return_temperature"

    @property
    def native_value(self) -> float | None:
        """The reading, or ``None`` when no usable probe reported one.

        ``None`` covers both "no probe configured" and "probe not trustworthy"
        (missing, stale, frozen, or the DS18B20 85 °C sentinel). Rendering any of
        those as a number would invent a pipe temperature that was never
        measured - the 85 °C sentinel in particular reads like a very hot return
        pipe and would look like a real measurement.
        """
        assessment = _probe_assessment(self.coordinator, "return")
        return None if assessment is None else assessment.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Carry the probe pair's own export so the verdict is explainable."""
        pair = self.coordinator.probes
        if pair is None:
            return {"evaluated": False}
        attributes = dict(pair.as_dict())
        attributes["evaluated"] = True
        return attributes


class DeltaTSensor(ClimatePilotEntity, SensorEntity):
    """Supply-minus-return temperature difference.

    ``TEMPERATURE_DELTA`` is the correct device class rather than
    ``TEMPERATURE``: a difference converts between °C and K one-to-one, whereas
    ``TEMPERATURE`` would apply an offset and turn 8 °C into 46.4 °F. Home
    Assistant validates the unit against the class, so ``°C`` is accepted here.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE_DELTA
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "delta_t")
        self._attr_translation_key = "delta_t"

    @property
    def native_value(self) -> float | None:
        report = self.coordinator.delta_t
        return None if report is None else report.delta_t

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Ship the engine's full report, including the "not power" marker.

        ``is_power_measurement`` is always ``False`` and is published verbatim so
        neither a human nor a dashboard template can mistake ΔT for heat output.
        """
        report = self.coordinator.delta_t
        if report is None:
            return {"evaluated": False}
        attributes = dict(report.as_dict())
        attributes["evaluated"] = True
        return attributes


class DeltaTQualitySensor(_SystemDiagnosticSensor):
    """Worst probe quality behind the current ΔT (``PROBE_*``).

    Diagnostic: it grades the *instrumentation*, which is troubleshooting
    evidence rather than a reading of the house.

    All five ``PROBE_QUALITIES`` values are published verbatim
    (docs/INTERFACES_V0.3.md section 3). One of them, ``PROBE_UNAVAILABLE``, was
    formerly the literal ``"unavailable"``, which Home Assistant reserves for a
    broken entity - publishing it made a healthy sensor on a probe-less
    installation indistinguishable from a dead one. That collision was fixed at
    the source in ``const.py`` (the value is now ``"probe_unavailable"``) and is
    guarded by ``tests/test_contracts.py``, so this entity no longer needs to
    work around it.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PROBE_QUALITIES

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "delta_t_quality")
        self._attr_translation_key = "delta_t_quality"

    @property
    def native_value(self) -> str | None:
        report = self.coordinator.delta_t
        quality = None if report is None else report.quality
        # Never publish a value the enum translation cannot render.
        if quality not in PROBE_QUALITIES:
            return None
        return quality

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.coordinator.delta_t
        pair = self.coordinator.probes
        if report is None:
            return {"evaluated": False}
        attributes: dict[str, Any] = {
            "evaluated": True,
            "samples": report.samples,
            "reason_codes": list(report.reason_codes),
        }
        if pair is not None:
            attributes["supply_quality"] = pair.supply.quality
            attributes["return_quality"] = pair.return_.quality
            attributes["both_usable"] = pair.both_usable
            attributes["divergence"] = pair.divergence
        return attributes


class CondensingRegimeSensor(_SystemDiagnosticSensor):
    """Whether the return temperature suggests a condensing regime.

    An **indirect** indicator from return temperature only; it is not an
    efficiency measurement (design doc 12, V0.3 delivery standard).
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONDENSING_REGIMES

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "condensing_regime")
        self._attr_translation_key = "condensing_regime"

    @property
    def native_value(self) -> str | None:
        report = self.coordinator.delta_t
        regime = None if report is None else report.condensing
        return regime if regime in CONDENSING_REGIMES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.coordinator.delta_t
        if report is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "is_power_measurement": False,
            "stable_seconds": report.stable_seconds,
            "samples": report.samples,
            "reason_codes": list(report.reason_codes),
        }


class HydraulicVerdictSensor(_SystemDiagnosticSensor):
    """Indirect hydraulic reading from the settled ΔT (``HYDRAULIC_*``).

    A low ΔT on a fixed-speed pump usually means **excessive flow or a bypass
    short-circuit**, and a high ΔT means **insufficient flow**. Both are
    hydraulic signals, not "good/bad efficiency", which is why the verdict is
    worded that way and never as a performance score.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = HYDRAULIC_VERDICTS

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "hydraulic_verdict")
        self._attr_translation_key = "hydraulic_verdict"

    @property
    def native_value(self) -> str | None:
        report = self.coordinator.delta_t
        verdict = None if report is None else report.hydraulic
        return verdict if verdict in HYDRAULIC_VERDICTS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.coordinator.delta_t
        if report is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "delta_t": report.delta_t,
            "average": report.average,
            "minimum": report.minimum,
            "maximum": report.maximum,
            "trend_per_hour": report.trend_per_hour,
            "stable_seconds": report.stable_seconds,
            "samples": report.samples,
            "is_power_measurement": False,
            "reason_codes": list(report.reason_codes),
        }


class CurveCalibrationAdviceSensor(_SystemDiagnosticSensor):
    """Advisory only: what to consider doing to the weather curve.

    The curve belongs to the user's configuration and is **never** changed
    automatically - this entity is a suggestion they may act on (design doc 7,
    V0.3: calibrate the weather-compensation parameters from real data, advice
    only, never applied automatically).
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALIBRATION_ADVICE

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "curve_calibration_advice")
        self._attr_translation_key = "curve_calibration_advice"

    @property
    def native_value(self) -> str | None:
        report = self.coordinator.delta_t
        advice = None if report is None else report.calibration
        return advice if advice in CALIBRATION_ADVICE else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self.coordinator.delta_t
        if report is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "advisory_only": True,
            "stable_seconds": report.stable_seconds,
            "samples": report.samples,
            "quality": report.quality,
            "reason_codes": list(report.reason_codes),
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


class ComfortVerdictSensor(ClimatePilotZoneEntity, SensorEntity):
    """Whether the room is actually comfortable (``COMFORT_*``).

    Not diagnostic: "is this room too warm or too cold" is the single most
    user-facing judgement the integration makes.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = COMFORT_VERDICTS

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "comfort_verdict")
        self._attr_translation_key = "comfort_verdict"

    @property
    def native_value(self) -> str:
        """A sampled verdict, or ``unknown`` when the room has none yet.

        A room the analyser has not seen is *not* uncomfortable and its entity is
        *not* broken - it is simply unmeasured, which is exactly what the
        ``unknown`` state (and its translation) exists to say.
        """
        verdict = self.coordinator.comfort.get(self._zone_id)
        if verdict is None or verdict.verdict not in COMFORT_VERDICTS:
            return COMFORT_UNKNOWN
        return verdict.verdict

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Ship the engine's full verdict so the number panel is explainable."""
        verdict = self.coordinator.comfort.get(self._zone_id)
        if verdict is None:
            return {"evaluated": False}
        attributes = dict(verdict.as_dict())
        attributes["evaluated"] = True
        return attributes


class ComfortAdviceSensor(ClimatePilotZoneEntity, SensorEntity):
    """What to do about the verdict (``ADVICE_*``).

    Kept as its own entity rather than an attribute of ``comfort_verdict`` so it
    can be shown - and translated - on its own, and so an automation can trigger
    on a specific advice code without parsing attributes.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = COMFORT_ADVICE

    def __init__(self, coordinator: ClimatePilotCoordinator, zone_id: str) -> None:
        super().__init__(coordinator, zone_id, "comfort_advice")
        self._attr_translation_key = "comfort_advice"

    @property
    def native_value(self) -> str:
        """The advice code, or ``none`` when there is nothing to advise."""
        verdict = self.coordinator.comfort.get(self._zone_id)
        if verdict is None or verdict.advice not in COMFORT_ADVICE:
            return ADVICE_NONE
        return verdict.advice

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        verdict = self.coordinator.comfort.get(self._zone_id)
        if verdict is None:
            return {"evaluated": False}
        return {
            "evaluated": True,
            "verdict": verdict.verdict,
            "average_deviation": round(verdict.average_deviation, 2),
            "reason_codes": list(verdict.reason_codes),
        }


# ---------------------------------------------------------------------------
# V0.5 gas and degree-days
#
# An instrument, not a verdict. These entities report what the meter and the
# outdoor sensor actually did; none of them claims the integration saved anything,
# and none of them can influence the boiler - there is no write path from here.
# ---------------------------------------------------------------------------


class _EnergySensor(_SystemDiagnosticSensor):
    """Shared behaviour for the gas/degree-day readings.

    Two things are handled once, because both are load-bearing:

    ``native_value`` returns whatever the engine produced, including ``None``.
    ``None`` reaches Home Assistant as "unknown", which is what a missing reading
    means. Substituting 0 would claim a measurement that was never made.

    ``_attr_state_class`` is set on each subclass. Without it the recorder purges
    the history after its retention window - 30 days on the reference installation -
    so there would be nothing left to compare month over month, which is the entire
    purpose of the feature.
    """

    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: ClimatePilotCoordinator, key: str) -> None:
        super().__init__(coordinator, key)

    def _reading(self, name: str) -> Any:
        return self.coordinator.energy_snapshot().get(name)

    @property
    def _scope_attributes(self) -> dict[str, Any]:
        """Declare that these numbers are not heating-only.

        The gas meter on the reference installation serves the boiler, domestic
        hot water and the hob. Without this stated on the entity itself, a figure
        like ``m3 per degree-day`` reads as boiler efficiency when it also contains
        cooking. Mirrors the V0.3 convention that put an explicit
        ``is_power_measurement = False`` on the probe entities.
        """
        return {"scope": "whole_house", "includes_dhw_and_cooking": True}


class GasTodaySensor(_EnergySensor):
    """Gas used since midnight, or ``None`` if no reading was available."""

    _attr_translation_key = "gas_today"
    _attr_native_unit_of_measurement = ENERGY_UNIT_GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "gas_today")

    @property
    def native_value(self) -> float | None:
        return self._reading("gas_today")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._scope_attributes, "meter_entity": self.coordinator.gas_meter_entity}


class GasYesterdaySensor(_EnergySensor):
    """Gas used over the last complete day.

    ``measurement`` rather than ``total_increasing``: this is a finished daily
    total that is replaced each midnight, so a running-sum interpretation would
    integrate the daily figures and report a value that grows without bound.
    """

    _attr_translation_key = "gas_yesterday"
    _attr_native_unit_of_measurement = ENERGY_UNIT_GAS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "gas_yesterday")

    @property
    def native_value(self) -> float | None:
        return self._reading("gas_yesterday")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._scope_attributes


class GasThisMonthSensor(_EnergySensor):
    """Gas used since the first of the month."""

    _attr_translation_key = "gas_this_month"
    _attr_native_unit_of_measurement = ENERGY_UNIT_GAS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "gas_this_month")

    @property
    def native_value(self) -> float | None:
        return self._reading("gas_this_month")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._scope_attributes, "previous_month": self._reading("gas_previous_month")}


class DegreeDaysTodaySensor(_EnergySensor):
    """Heating degree-days accumulated since midnight.

    This is a temperature difference integrated over time, not a temperature, so no
    device class is set: ``temperature`` would trigger Home Assistant's unit
    conversion and turn it into a nonsense figure.
    """

    _attr_translation_key = "degree_days_today"
    _attr_native_unit_of_measurement = ENERGY_UNIT_DEGREE_DAYS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "degree_days_today")

    @property
    def native_value(self) -> float | None:
        return self._reading("degree_days_today")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Publish the base temperature and how much of the day was observed.

        The base is a convention - 15.5 here, 18.3 in US practice - so the reading
        cannot be interpreted without it. Coverage qualifies the number: a day with
        a sensor outage is a real measurement of a shorter period, and saying so is
        better than hiding it or silently trusting it.
        """
        return {
            "base_c": self._reading("base_c"),
            "coverage": self._reading("coverage_today"),
            "trusted": self._reading("degree_days_trusted"),
        }


class DegreeDaysThisMonthSensor(_EnergySensor):
    """Heating degree-days accumulated since the first of the month."""

    _attr_translation_key = "degree_days_this_month"
    _attr_native_unit_of_measurement = ENERGY_UNIT_DEGREE_DAYS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "degree_days_this_month")

    @property
    def native_value(self) -> float | None:
        return self._reading("degree_days_this_month")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"base_c": self._reading("base_c")}


class GasPerDegreeDaySensor(_EnergySensor):
    """Gas used per degree-day this month: a comparable figure, not an efficiency.

    This is the number a user can watch across months to see whether the house is
    using more or less gas for the same weather. It is deliberately not called an
    efficiency: the meter is whole-house, so the value also contains hot water and
    cooking, and those do not scale with outdoor temperature at all.

    ``None`` when there are no degree-days - dividing by zero would report either
    perfect efficiency (0) or an unusable infinity.
    """

    _attr_translation_key = "gas_per_degree_day"
    _attr_native_unit_of_measurement = ENERGY_UNIT_GAS_PER_DEGREE_DAY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "gas_per_degree_day")

    @property
    def native_value(self) -> float | None:
        return self._reading("gas_per_degree_day")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._scope_attributes,
            # Mirrors `is_power_measurement = False` on the V0.3 probe entities: an
            # explicit marker so no consumer can mistake this for boiler performance.
            "is_efficiency_measurement": False,
            "base_c": self._reading("base_c"),
            "gas_this_month": self._reading("gas_this_month"),
            "degree_days_this_month": self._reading("degree_days_this_month"),
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
        DominantZoneCombinationSensor(coordinator),
        ZoneCombinationCountSensor(coordinator),
        Combinations24hSensor(coordinator),
        CyclingQualitySensor(coordinator),
        UnexplainedBurns24hSensor(coordinator),
        ReturnTemperatureSensor(coordinator),
        DeltaTSensor(coordinator),
        DeltaTQualitySensor(coordinator),
        CondensingRegimeSensor(coordinator),
        HydraulicVerdictSensor(coordinator),
        CurveCalibrationAdviceSensor(coordinator),
    ]

    for zone_id in coordinator.zone_ids:
        entities.extend(
            (
                EffectiveSetpointSensor(coordinator, zone_id),
                SetpointSourceSensor(coordinator, zone_id),
                ZoneTemperatureSensor(coordinator, zone_id),
                OverrideRemainingSensor(coordinator, zone_id),
                ComfortVerdictSensor(coordinator, zone_id),
                ComfortAdviceSensor(coordinator, zone_id),
            )
        )

    # V0.5 gas metering is opt-in: with no meter configured the six entities are
    # not created at all, so a system without one sees no permanently-unknown
    # sensors and pays nothing for the feature's existence.
    if coordinator.energy_enabled:
        entities.extend(
            (
                GasTodaySensor(coordinator),
                GasYesterdaySensor(coordinator),
                GasThisMonthSensor(coordinator),
                DegreeDaysTodaySensor(coordinator),
                DegreeDaysThisMonthSensor(coordinator),
                GasPerDegreeDaySensor(coordinator),
            )
        )

    async_add_entities(entities)
