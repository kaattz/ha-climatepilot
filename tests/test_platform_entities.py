"""Tests for the ClimatePilot entity platforms and the integration entry point.

The platform layer is a thin projection of the coordinator's public surface
(``docs/INTERFACES.md`` section 2.8). These tests therefore drive it with a
duck-typed coordinator double -- no Home Assistant instance is booted and no
integration is loaded -- and assert the three things that would silently break
a real install:

1. every entity carries a ``translation_key`` that exists in ``strings.json``
   and never hardcodes a ``name``;
2. ``unique_id`` is stable and equal to ``{entry_id}_{zone}_{key}``;
3. the values the entities publish are honest: unknown stays ``unknown``
   instead of being reported as ``0`` or ``off``.

The reference installation is the real one this integration targets: three KNX
floor-heating zones (living room, master bedroom, guest bedroom) and a Rinnai
boiler with a burner-state sensor.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import slugify

from custom_components.climatepilot import (
    PLATFORMS,
    _async_handle_reassess,
    _async_handle_resume_auto,
    _async_handle_set_supply_temperature,
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.climatepilot.binary_sensor import (
    DegradedBinarySensor,
    HeatRequestBinarySensor,
    ManualOverrideBinarySensor,
    OverheatingBinarySensor,
    ProbeFaultBinarySensor,
    ShortCycleAlertBinarySensor,
    UnderheatingBinarySensor,
)
from custom_components.climatepilot.button import ReassessButton, ResumeAutoButton
from custom_components.climatepilot.const import (
    ADVICE_LOWER_SUPPLY,
    ADVICE_NONE,
    ADVICE_RAISE_SUPPLY,
    CALIBRATION_INSUFFICIENT_DATA,
    CALIBRATION_RAISE_CURVE,
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    COMFORT_UNKNOWN,
    CONDENSING_LIKELY,
    CONDENSING_UNKNOWN,
    CONF_ZONE_ID,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    DOMAIN,
    FEEDBACK_UNAVAILABLE,
    FEEDBACK_VALID,
    FSM_DEGRADED,
    FSM_HEATING,
    FSM_IDLE,
    HVAC_ACTION_HEATING,
    HVAC_ACTION_IDLE,
    HYDRAULIC_OK,
    HYDRAULIC_UNKNOWN,
    PROBE_IMPLAUSIBLE,
    PROBE_QUALITIES,
    PROBE_STALE,
    PROBE_UNAVAILABLE,
    PROBE_VALID,
    QUALITY_FAIR,
    QUALITY_GOOD,
    QUALITY_POOR,
    QUALITY_UNKNOWN,
    SERVICE_REASSESS,
    SERVICE_RESUME_AUTO,
    SERVICE_SET_SUPPLY_TEMPERATURE,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
    ZONE_MODE_AUTO,
    ZONE_MODE_ECO,
)
from custom_components.climatepilot.models import (
    ManualOverride,
    SetpointDecision,
    ZoneState,
)
from custom_components.climatepilot.number import SupplyTemperatureOverrideNumber
from custom_components.climatepilot.select import ControlModeSelect, ZoneModeSelect
from custom_components.climatepilot.sensor import (
    ActiveZonesSensor,
    BurnerStarts24hSensor,
    Combinations24hSensor,
    ComfortAdviceSensor,
    ComfortVerdictSensor,
    CondensingRegimeSensor,
    CurveCalibrationAdviceSensor,
    CyclingQualitySensor,
    DeltaTQualitySensor,
    DeltaTSensor,
    DominantZoneCombinationSensor,
    EffectiveSetpointSensor,
    HeatingStateSensor,
    HydraulicVerdictSensor,
    LastBurnDurationSensor,
    OutdoorTemperatureSensor,
    OverrideRemainingSensor,
    ReturnTemperatureSensor,
    SetpointSourceSensor,
    SuggestedSupplyTempSensor,
    SupplyTemperatureSensor,
    UnexplainedBurns24hSensor,
    ZoneCombinationCountSensor,
    ZoneTemperatureSensor,
)
from custom_components.climatepilot.switch import AuxiliaryLoadSwitch

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "climatepilot"
DASHBOARD_FILE = Path(__file__).resolve().parents[1] / "dashboards" / "climatepilot.yaml"

ENTRY_ID = "entry_abc123"
T0 = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)

#: Distinguishes "caller did not pass this" from "caller passed ``None``".
#: Several V0.2 surfaces treat ``None`` as a real state ("no report yet"), so a
#: default of ``None`` would make the meaningful case unrepresentable.
UNSET: object = object()

ZONES = {
    "living_room": ("climate.livingroom_floorheating", "客厅"),
    "master_bedroom": ("climate.master_bedroom_floorheating", "主卧"),
    "guest_bedroom": ("climate.guest_bedroom_floorheating", "次卧"),
}


def _schema_invalid_class() -> type[BaseException] | tuple[type[BaseException], ...]:
    """Return every schema-validation error class this process may raise.

    Home Assistant 2026.9 swaps the voluptuous implementation for ``probatio``
    at import time (``install_as_voluptuous``). That swap is order-sensitive and
    can leave *two* unrelated ``MultipleInvalid`` classes reachable in one
    process:

    * ``probatio.error.MultipleInvalid`` - the swap took effect;
    * ``voluptuous.error.MultipleInvalid`` - some module imported voluptuous
      first, so the swap did not.

    Whether they are the same object depends on which test module pytest
    imported first, so asserting either one alone makes the test pass or fail by
    file ordering. Every reachable class from both libraries is collected, and
    ``Exception`` is deliberately *not* in the list - the assertion must still
    be about schema rejection, not about "something went wrong".
    """
    import voluptuous.error as voluptuous_error

    candidates: list[type[BaseException]] = [
        voluptuous_error.Invalid,
        voluptuous_error.MultipleInvalid,
    ]

    try:  # pragma: no cover - only present on newer Home Assistant
        import probatio.error as probatio_error

        candidates.append(probatio_error.Invalid)
        candidates.append(probatio_error.MultipleInvalid)
    except ImportError:
        pass

    unique: list[type[BaseException]] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)

    if not unique:  # pragma: no cover - defensive
        raise AssertionError("no schema validation error class found")
    return unique[0] if len(unique) == 1 else tuple(unique)


async def _assert_schema_rejects(hass, service: str, data: dict) -> None:
    """Assert the registered schema rejects ``data``.

    Checks the *behaviour* - a validation error is raised - while tolerating
    both schema libraries Home Assistant may be using. The exception is matched
    by walking its MRO for a class named ``Invalid``/``MultipleInvalid`` from
    either library, which survives the import-order problem described above
    without weakening the assertion into "any exception at all": a
    ``ServiceNotFound`` or a ``TypeError`` must still fail the test.
    """
    try:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    except BaseException as err:
        names = {cls.__name__ for cls in type(err).__mro__}
        modules = {cls.__module__ for cls in type(err).__mro__}
        is_validation_error = bool(
            names & {"Invalid", "MultipleInvalid", "RequiredFieldInvalid"}
        ) and any(mod.startswith(("voluptuous", "probatio")) for mod in modules)
        assert is_validation_error, (
            f"expected a schema validation error, got {type(err).__module__}."
            f"{type(err).__name__}: {err}"
        )
        return
    raise AssertionError(f"schema accepted invalid data for {service}: {data!r}")


# ---------------------------------------------------------------------------
# Coordinator double
# ---------------------------------------------------------------------------


class FakeCompensation:
    """Stand-in for ``CompensationResult``."""

    def __init__(
        self,
        *,
        suggested: float | None = 42.5,
        outdoor: float | None = 6.0,
        quality: str = FEEDBACK_VALID,
    ) -> None:
        self.suggested = suggested
        self.outdoor = outdoor
        self.quality = quality
        self.reason_codes = ("schedule_transition",)
        self.should_apply = False


def make_burner_snapshot(**overrides: object) -> dict[str, object]:
    """Return a BurnerStats.snapshot()-shaped mapping."""
    snapshot: dict[str, object] = {
        "state": "on",
        "state_known": True,
        "quality": FEEDBACK_VALID,
        "burning": True,
        "kind": "heating",
        "includes_dhw": False,
        "last_duration": 615.0,
        "current_duration": 120.0,
        "runtime_24h": 3600.0,
        "starts_1h": 1,
        "starts_24h": 9,
        "starts_total": 40,
        "heating_starts_24h": 9,
        "dhw_starts_24h": 0,
        "burns_24h": 9,
        "short_cycles_24h": 2,
        "short_cycle_alert": True,
        "short_cycle_seconds": 300.0,
        "histogram": {"0-5m": 2},
        "histogram_window_seconds": 86400.0,
        "last_started_at": None,
        "last_ended_at": None,
        "demand_active": True,
        # V0.2 combustion grading (INTERFACES_V0.2.md 3.3).
        "cycling_quality": QUALITY_GOOD,
        "cycling_quality_reason": "average 720s over 9 heating burns",
        "unexplained_burns_24h": 1,
        "burns_without_demand_24h": 1,
        "grade_heating_24h": {"good": 7, "fair": 2},
        "grade_dhw_24h": {"poor": 1},
    }
    snapshot.update(overrides)
    return snapshot


class FakeCombinationReport:
    """Stand-in for ``CombinationReport`` (INTERFACES_V0.2.md 3.1).

    ``dominant_threshold``, ``has_dominant_mode`` and ``total_groups`` belong to
    the engine's ``as_dict()`` export rather than to the report's attribute
    surface, so the double carries them exactly that way - a sensor that starts
    reading them as attributes fails here instead of in production.
    """

    def __init__(
        self,
        *,
        dominant_key: str = "living_room+master_bedroom",
        dominant_share: float = 0.62,
        distinct_count: int = 2,
        total_demand_seconds: float = 5400.0,
        stats: tuple[tuple[str, float], ...] = (
            ("living_room+master_bedroom", 3600.0),
            ("living_room", 1800.0),
            ("none", 0.0),
        ),
        current_key: str = "living_room+master_bedroom",
        current_seconds: float = 900.0,
        dominant_threshold: float = 0.3,
    ) -> None:
        self.dominant_key = dominant_key
        self.dominant_share = dominant_share
        self.distinct_count = distinct_count
        self.total_demand_seconds = total_demand_seconds
        self.stats = tuple(
            SimpleNamespace(key=key, total_seconds=seconds) for key, seconds in stats
        )
        self.current_key = current_key
        self.current_seconds = current_seconds
        self.dominant_threshold = dominant_threshold

    @property
    def has_dominant_mode(self) -> bool:
        return self.dominant_share >= self.dominant_threshold

    @property
    def total_groups(self) -> int:
        return len(self.stats)

    def as_dict(self) -> dict[str, object]:
        return {
            "dominant_key": self.dominant_key,
            "dominant_share": round(self.dominant_share, 4),
            "dominant_threshold": round(self.dominant_threshold, 4),
            "has_dominant_mode": self.has_dominant_mode,
            "distinct_count": self.distinct_count,
            "total_groups": self.total_groups,
            "total_demand_seconds": round(self.total_demand_seconds, 1),
            "current_key": self.current_key,
            "current_seconds": round(self.current_seconds, 1),
            "stats": [
                {"key": stat.key, "total_seconds": round(stat.total_seconds, 1)}
                for stat in self.stats
            ],
        }


class FakeComfortVerdict:
    """Stand-in for ``ComfortVerdict`` (INTERFACES_V0.2.md 3.2)."""

    def __init__(
        self,
        zone_id: str,
        *,
        verdict: str = COMFORT_OK,
        advice: str = ADVICE_NONE,
        average_deviation: float = 0.2,
        max_over: float = 0.6,
        max_under: float = 0.4,
        overheated_seconds: float = 0.0,
        underheated_seconds: float = 0.0,
        samples: int = 12,
        reason_codes: tuple[str, ...] = ("comfort_window_ok",),
    ) -> None:
        self.zone_id = zone_id
        self.verdict = verdict
        self.advice = advice
        self.average_deviation = average_deviation
        self.max_over = max_over
        self.max_under = max_under
        self.overheated_seconds = overheated_seconds
        self.underheated_seconds = underheated_seconds
        self.samples = samples
        self.reason_codes = reason_codes

    def as_dict(self) -> dict[str, object]:
        return {
            "zone_id": self.zone_id,
            "verdict": self.verdict,
            "advice": self.advice,
            "average_deviation": round(self.average_deviation, 2),
            "max_over": self.max_over,
            "max_under": self.max_under,
            "overheated_seconds": self.overheated_seconds,
            "underheated_seconds": self.underheated_seconds,
            "samples": self.samples,
            "reason_codes": list(self.reason_codes),
        }


class FakeProbeAssessment:
    """Stand-in for ``ProbeAssessment`` (INTERFACES_V0.3.md 4.1)."""

    def __init__(
        self,
        entity_id: str = "sensor.probe",
        *,
        value: float | None = 45.0,
        quality: str = PROBE_VALID,
        reason_codes: tuple[str, ...] = (),
        age_seconds: float | None = 12.0,
        frozen_seconds: float | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.value = value
        self.quality = quality
        self.reason_codes = reason_codes
        self.age_seconds = age_seconds
        self.frozen_seconds = frozen_seconds

    @property
    def usable(self) -> bool:
        return self.quality == PROBE_VALID

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "value": self.value,
            "quality": self.quality,
            "reason_codes": list(self.reason_codes),
            "age_seconds": self.age_seconds,
            "frozen_seconds": self.frozen_seconds,
        }


class FakeProbePair:
    """Stand-in for ``ProbePairAssessment``.

    The divergence is computed here rather than passed in, mirroring the engine's
    rule that a pair without two usable probes has **no** divergence instead of a
    one-sided guess.
    """

    def __init__(
        self,
        *,
        supply: FakeProbeAssessment | None = None,
        return_: FakeProbeAssessment | None = None,
        reason_codes: tuple[str, ...] = (),
    ) -> None:
        self.supply = supply if supply is not None else FakeProbeAssessment()
        self.return_ = (
            return_
            if return_ is not None
            else FakeProbeAssessment(entity_id="sensor.return_probe", value=37.0)
        )
        self.reason_codes = reason_codes

    @property
    def both_usable(self) -> bool:
        return self.supply.usable and self.return_.usable

    @property
    def divergence(self) -> float | None:
        if not self.both_usable:
            return None
        assert self.supply.value is not None and self.return_.value is not None
        return abs(self.supply.value - self.return_.value)

    def as_dict(self) -> dict[str, object]:
        return {
            "supply": self.supply.as_dict(),
            "return": self.return_.as_dict(),
            "divergence": self.divergence,
            "both_usable": self.both_usable,
            "reason_codes": list(self.reason_codes),
        }


def make_missing_probe_pair() -> FakeProbePair:
    """The "no probe installed" state: both sides unavailable, values ``None``."""
    return FakeProbePair(
        supply=FakeProbeAssessment(entity_id="", value=None, quality=PROBE_UNAVAILABLE),
        return_=FakeProbeAssessment(entity_id="", value=None, quality=PROBE_UNAVAILABLE),
    )


class FakeDeltaTReport:
    """Stand-in for ``DeltaTReport`` (INTERFACES_V0.3.md 4.2).

    ``is_power_measurement`` is hardcoded ``False`` exactly as the real engine
    does, so a sensor that stops publishing the marker fails here.
    """

    def __init__(
        self,
        *,
        delta_t: float | None = 8.0,
        average: float | None = 7.8,
        minimum: float | None = 7.1,
        maximum: float | None = 8.6,
        trend_per_hour: float | None = 0.3,
        stable_seconds: float = 1800.0,
        samples: int = 30,
        quality: str = PROBE_VALID,
        condensing: str = CONDENSING_LIKELY,
        hydraulic: str = HYDRAULIC_OK,
        calibration: str = CALIBRATION_RAISE_CURVE,
        reason_codes: tuple[str, ...] = ("temperature_difference_is_not_heat_power",),
    ) -> None:
        self.delta_t = delta_t
        self.average = average
        self.minimum = minimum
        self.maximum = maximum
        self.trend_per_hour = trend_per_hour
        self.stable_seconds = stable_seconds
        self.samples = samples
        self.quality = quality
        self.condensing = condensing
        self.hydraulic = hydraulic
        self.calibration = calibration
        self.reason_codes = reason_codes

    def as_dict(self) -> dict[str, object]:
        return {
            "delta_t": self.delta_t,
            "average": self.average,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "trend_per_hour": self.trend_per_hour,
            "stable_seconds": self.stable_seconds,
            "samples": self.samples,
            "quality": self.quality,
            "condensing": self.condensing,
            "hydraulic": self.hydraulic,
            "calibration": self.calibration,
            "reason_codes": list(self.reason_codes),
            "is_power_measurement": False,
        }


def make_empty_delta_t_report() -> FakeDeltaTReport:
    """What the real engine returns with no probes: everything unknown."""
    return FakeDeltaTReport(
        delta_t=None,
        average=None,
        minimum=None,
        maximum=None,
        trend_per_hour=None,
        stable_seconds=0.0,
        samples=0,
        quality=PROBE_UNAVAILABLE,
        condensing=CONDENSING_UNKNOWN,
        hydraulic=HYDRAULIC_UNKNOWN,
        calibration=CALIBRATION_INSUFFICIENT_DATA,
        reason_codes=("temperature_difference_is_not_heat_power", "probe_not_configured"),
    )


class FakeWeather:
    """Stand-in for ``WeatherCompensator``."""

    def __init__(self, last_applied: float | None = 42.5) -> None:
        self.last_applied = last_applied


class FakeStateMachine:
    """Stand-in for ``HeatSourceStateMachine``."""

    def __init__(self, capabilities: object | None = None) -> None:
        self.capabilities = capabilities
        self.state = FSM_HEATING


class FakeCoordinator:
    """Duck-typed ``ClimatePilotCoordinator`` exposing only public attributes."""

    def __init__(
        self,
        *,
        zone_ids: tuple[str, ...] = tuple(ZONES),
        compensation: object | None = None,
        burner: dict[str, object] | None = None,
        control_mode: str = CONTROL_MODE_COORDINATE,
        capabilities: object | None = None,
        combinations: object = UNSET,
        comfort: dict[str, object] | None = None,
        probes: object = UNSET,
        delta_t: object = UNSET,
        probe_fault: bool = False,
        probes_configured: bool = True,
    ) -> None:
        self.system_id = ENTRY_ID
        self.name = "智暖领航"
        self.control_mode = control_mode
        # The real coordinator always carries its config entry, and the zone
        # device lookup needs its entry_id to scope the parent resolution.
        self.entry = SimpleNamespace(entry_id=ENTRY_ID)
        self.zones: dict[str, ZoneState] = {}
        for zone_id, (entity_id, name) in ZONES.items():
            if zone_id not in zone_ids:
                continue
            self.zones[zone_id] = ZoneState(
                zone_id=zone_id,
                entity_id=entity_id,
                name=name,
                mode=ZONE_MODE_AUTO,
                effective_setpoint=21.5,
                actual_setpoint=21.5,
                current_temperature=20.5,
                min_temp=16.0,
                max_temp=26.0,
                target_step=0.5,
                tolerance=0.2,
                hvac_mode="heat",
                hvac_action=HVAC_ACTION_HEATING,
                feedback_quality=FEEDBACK_VALID,
            )

        self.zone_decisions: dict[str, SetpointDecision] = {
            zone_id: SetpointDecision(
                zone_id=zone_id,
                effective=21.5,
                base=21.0,
                source=SOURCE_SCHEDULE,
                reason_codes=("schedule_transition",),
                adjustment_delta=0.5,
            )
            for zone_id in self.zones
        }
        self.heat_state = FSM_HEATING
        self.heat_decision = SimpleNamespace(
            state=FSM_HEATING,
            decision="request_heat",
            target_mode="heat",
            target_supply=42.5,
            reason_codes=("demand_debounce",),
        )
        self.compensation = compensation
        self.diagnostics_snapshot: dict[str, object] = {
            "burner": burner if burner is not None else make_burner_snapshot(),
            "heat_state": FSM_HEATING,
            "heat_decision": "request_heat",
            "active_zones": 2,
            "unknown_zones": 0,
            "control_mode": control_mode,
            "zones": {},
        }
        self.state_machine = FakeStateMachine(capabilities)
        self.weather = FakeWeather()
        self.last_update_success = True

        # --- V0.2 surfaces (INTERFACES_V0.2.md 3.4) -----------------------
        # ``combinations=None`` is a meaningful state (no report yet), so the
        # sentinel rather than None decides whether the caller overrode it.
        self.combinations = FakeCombinationReport() if combinations is UNSET else combinations
        self.comfort: dict[str, object] = (
            comfort
            if comfort is not None
            else {zone_id: FakeComfortVerdict(zone_id) for zone_id in self.zones}
        )
        # The analyser is what tells a comfort flag how long a fault must last;
        # a double without it proves the flag degrades instead of guessing.
        self.comfort_analyser = SimpleNamespace(overheat_dwell=1800.0, underheat_dwell=3600.0)

        # --- V0.3 surfaces (INTERFACES_V0.3.md 4.3) -----------------------
        # ``probes=None`` / ``delta_t=None`` are meaningful states ("not
        # evaluated yet"), so the sentinel decides whether the caller overrode.
        self.probes = FakeProbePair() if probes is UNSET else probes
        self.delta_t = FakeDeltaTReport() if delta_t is UNSET else delta_t
        self.probe_fault = probe_fault
        self.probes_configured = probes_configured

        # Commands the platform layer is allowed to call.
        self.async_set_control_mode = AsyncMock()
        self.async_resume_auto = AsyncMock()
        self.async_set_zone_mode = AsyncMock()
        self.async_set_supply_temperature = AsyncMock()
        self.async_reassess = AsyncMock()
        self.async_set_zone_aux_load = AsyncMock()

    # --- public helpers from INTERFACES.md 2.8 -------------------------
    @property
    def zone_ids(self) -> list[str]:
        return list(self.zones)

    @property
    def cycling_quality(self) -> str:
        burner = self.diagnostics_snapshot.get("burner") or {}
        return str(burner.get("cycling_quality", QUALITY_UNKNOWN))

    @property
    def cycling_quality_reason(self) -> str:
        burner = self.diagnostics_snapshot.get("burner") or {}
        return str(burner.get("cycling_quality_reason", ""))

    @property
    def unexplained_burns(self) -> int:
        burner = self.diagnostics_snapshot.get("burner") or {}
        return int(burner.get("unexplained_burns_24h", 0))

    def comfort_for(self, zone_id: str) -> object | None:
        return self.comfort.get(zone_id)

    @property
    def short_cycle_alert(self) -> bool:
        burner = self.diagnostics_snapshot.get("burner") or {}
        return bool(burner.get("short_cycle_alert"))

    @property
    def degraded(self) -> bool:
        return self.heat_state == FSM_DEGRADED or bool(
            self.diagnostics_snapshot.get("unknown_zones")
        )

    def setpoint_source(self, zone_id: str) -> str | None:
        decision = self.zone_decisions.get(zone_id)
        return decision.source if decision else None

    def reason_codes_for(self, zone_id: str) -> list[str]:
        decision = self.zone_decisions.get(zone_id)
        return list(decision.reason_codes) if decision else []

    def supply_temperature_reading(self) -> float | None:
        return 41.0


def make_hass(coordinator: object | None = None) -> SimpleNamespace:
    """Return a minimal ``hass`` double holding the coordinator in ``data``."""
    data: dict[str, object] = {}
    if coordinator is not None:
        data[ENTRY_ID] = coordinator
    return SimpleNamespace(data={DOMAIN: data}, services=SimpleNamespace())


# ---------------------------------------------------------------------------
# strings.json contract
# ---------------------------------------------------------------------------


def load_entity_strings() -> dict[str, dict[str, object]]:
    with (COMPONENT_DIR / "strings.json").open(encoding="utf-8") as handle:
        return json.load(handle)["entity"]


ENTITY_STRINGS = load_entity_strings()


def assert_translation_key(platform: str, key: str) -> None:
    """The key must exist in strings.json, otherwise HA renders nothing."""
    assert key in ENTITY_STRINGS[platform], (
        f"{platform}.{key} is missing from strings.json entity section"
    )


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


#: (system-level, per-zone) entity inventory as declared by ``docs/INTERFACES.md``
#: 2.9 and ``docs/INTERFACES_V0.2.md`` 2 / 3.5, and consumed from the ``entity``
#: section of ``strings.json``. Kept at module level so the build helper and the
#: count test cannot disagree with each other.
#:
#: ``climate`` is absent on purpose: this module builds the platform entities the
#: way each platform's ``async_setup_entry`` does, and the whole-home thermostat
#: is created by ``climate.py`` through a different path (``test_integration_setup``
#: counts it).
EXPECTED_PER_PLATFORM: dict[str, tuple[int, int]] = {
    "sensor": (18, 6),
    "binary_sensor": (3, 4),
    "select": (1, 1),
    "button": (1, 1),
    "number": (1, 0),
    "switch": (0, 1),
}

#: Entities one three-zone system must expose in total. Derived, never pinned:
#: V0.1 asserted a bare ``39`` *and* restated the same sum in a trailing comment,
#: so the comment and the number could drift apart with nothing to catch it. The
#: two axes (system-level, per-zone) now live in ``EXPECTED_PER_PLATFORM`` and
#: every total below is read out of them.
EXPECTED_TOTAL = sum(
    system + len(ZONES) * per_zone for system, per_zone in EXPECTED_PER_PLATFORM.values()
)


def build_platform_entities(coordinator: FakeCoordinator) -> dict[str, list]:
    """Build every entity exactly the way each platform's setup does."""
    entities: dict[str, list] = {
        "sensor": [
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
        ],
        "binary_sensor": [
            ShortCycleAlertBinarySensor(coordinator),
            DegradedBinarySensor(coordinator),
            ProbeFaultBinarySensor(coordinator),
        ],
        "select": [ControlModeSelect(coordinator)],
        "button": [ReassessButton(coordinator)],
        "number": [SupplyTemperatureOverrideNumber(coordinator)],
        "switch": [],
    }
    for zone_id in coordinator.zone_ids:
        entities["sensor"].extend(
            [
                EffectiveSetpointSensor(coordinator, zone_id),
                SetpointSourceSensor(coordinator, zone_id),
                ZoneTemperatureSensor(coordinator, zone_id),
                OverrideRemainingSensor(coordinator, zone_id),
                ComfortVerdictSensor(coordinator, zone_id),
                ComfortAdviceSensor(coordinator, zone_id),
            ]
        )
        entities["binary_sensor"].extend(
            [
                ManualOverrideBinarySensor(coordinator, zone_id),
                HeatRequestBinarySensor(coordinator, zone_id),
                OverheatingBinarySensor(coordinator, zone_id),
                UnderheatingBinarySensor(coordinator, zone_id),
            ]
        )
        entities["select"].append(ZoneModeSelect(coordinator, zone_id))
        entities["button"].append(ResumeAutoButton(coordinator, zone_id))
        entities["switch"].append(AuxiliaryLoadSwitch(coordinator, zone_id))
    return entities


# ---------------------------------------------------------------------------
# Entity counts and translation keys
# ---------------------------------------------------------------------------


def test_entity_counts() -> None:
    """3 zones must yield the documented number of entities per platform.

    The invariant is the *formula* ``system_level + zones * per_zone``, and the
    total must equal the number of ``translation_key``s in ``strings.json``
    (1 system + 1 per zone for select, and so on). Pinning a bare total would
    silently drift the moment a platform gains an entity.
    """
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entities = build_platform_entities(coordinator)
    zone_count = len(coordinator.zone_ids)

    assert set(entities) == set(EXPECTED_PER_PLATFORM)

    for platform, (system_level, per_zone) in EXPECTED_PER_PLATFORM.items():
        assert len(entities[platform]) == system_level + zone_count * per_zone, (
            f"{platform}: expected {system_level} + {zone_count}*{per_zone}"
        )

    total = sum(len(group) for group in entities.values())
    assert total == EXPECTED_TOTAL, (
        f"expected {EXPECTED_TOTAL} entities (V0.1's 31 + 5 system-level + "
        f"4 per zone x {zone_count}), got {total}"
    )
    assert total == (
        sum(system for system, _ in EXPECTED_PER_PLATFORM.values())
        + zone_count * sum(per_zone for _, per_zone in EXPECTED_PER_PLATFORM.values())
    )


def test_expected_counts_agree_with_strings_json() -> None:
    """The declared inventory must match the translated keys exactly.

    This is the check V0.1 was missing. That suite pinned the platform split in
    one place and a bare total in another, with nothing tying either to the
    ``strings.json`` tree they both describe - so the two could drift, and a
    ``translation_key`` added without touching the counts would only have shown
    up as a blank entity name in the UI.

    ``climate`` is excluded from both sides for the reason given above
    ``EXPECTED_PER_PLATFORM``: it is not built by the platform setup path.

    The comparison is against *keys*, not entities: ``strings.json`` holds one
    entry per ``translation_key``, so a per-zone key contributes once no matter
    how many rooms exist. Both axes of ``EXPECTED_PER_PLATFORM`` therefore
    contribute once each.
    """
    declared = {
        platform: system_level + per_zone
        for platform, (system_level, per_zone) in EXPECTED_PER_PLATFORM.items()
    }
    documented = {
        platform: len(keys)
        for platform, keys in ENTITY_STRINGS.items()
        if platform in EXPECTED_PER_PLATFORM
    }
    assert declared == documented, f"counts drift from strings.json: {declared} vs {documented}"

    # ... and the entity total really is what the two axes multiply out to.
    total = sum(
        system_level + len(ZONES) * per_zone
        for system_level, per_zone in EXPECTED_PER_PLATFORM.values()
    )
    assert total == EXPECTED_TOTAL


def test_all_translation_keys_exist_in_strings() -> None:
    """Every translation_key must be renderable by strings.json."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    for platform, group in build_platform_entities(coordinator).items():
        for entity in group:
            key = entity.translation_key
            assert key, f"{type(entity).__name__} has no translation_key"
            assert_translation_key(platform, key)


def test_no_entity_hardcodes_a_name() -> None:
    """Names come from strings.json, never from the entity.

    HA resolves an entity's name from ``translation_key`` only when no
    ``_attr_name`` is set, so a stray ``_attr_name`` would permanently shadow
    the translations. Reading ``entity.name`` is not possible here because it
    needs a fully attached platform, hence the attribute-level check.
    """
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    for group in build_platform_entities(coordinator).values():
        for entity in group:
            # HA wraps ``_attr_name`` into a property backed by ``__attr_name``;
            # it raises AttributeError precisely when no name was hardcoded.
            with pytest.raises(AttributeError):
                _ = entity._attr_name
            assert entity.has_entity_name is True
            assert entity.translation_key


def test_expected_translation_key_set() -> None:
    """Pin the exact key inventory so a rename cannot slip through."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entities = build_platform_entities(coordinator)
    keys = {
        platform: {entity.translation_key for entity in group}
        for platform, group in entities.items()
    }
    assert keys["select"] == {"control_mode", "zone_mode"}
    assert keys["sensor"] == {
        "heating_state",
        "active_zones",
        "suggested_supply_temp",
        "outdoor_temperature",
        "supply_temperature",
        "burner_starts_24h",
        "last_burn_duration",
        "dominant_zone_combination",
        "zone_combination_count",
        "combinations_24h",
        "cycling_quality",
        "unexplained_burns_24h",
        "effective_setpoint",
        "setpoint_source",
        "zone_temperature",
        "override_remaining",
        "comfort_verdict",
        "comfort_advice",
        "return_temperature",
        "delta_t",
        "delta_t_quality",
        "condensing_regime",
        "hydraulic_verdict",
        "curve_calibration_advice",
    }
    assert keys["binary_sensor"] == {
        "manual_override",
        "short_cycle_alert",
        "degraded",
        "heat_request",
        "overheating",
        "underheating",
        "probe_fault",
    }
    assert keys["button"] == {"resume_auto", "reassess"}
    assert keys["number"] == {"supply_temperature_override"}
    assert keys["switch"] == {"auxiliary_load"}


def test_v02_translation_keys_are_implemented_verbatim() -> None:
    """The V0.2 keys are a frozen contract (INTERFACES_V0.2.md section 2).

    The dashboard YAML, the translations and this test all quote the same nine
    literals; a rename in one place is silent everywhere else, so they are
    pinned here by hand rather than derived from the entities under test.
    """
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entities = build_platform_entities(coordinator)

    system_level = {
        "dominant_zone_combination",
        "zone_combination_count",
        "combinations_24h",
        "cycling_quality",
        "unexplained_burns_24h",
    }
    per_zone_sensor = {"comfort_verdict", "comfort_advice"}
    per_zone_binary = {"overheating", "underheating"}

    sensor_keys = {entity.translation_key for entity in entities["sensor"]}
    binary_keys = {entity.translation_key for entity in entities["binary_sensor"]}

    assert system_level <= sensor_keys
    assert per_zone_sensor <= sensor_keys
    assert per_zone_binary <= binary_keys

    # Each per-zone key must exist once per configured room, not once overall.
    for key in per_zone_sensor:
        assert sum(1 for entity in entities["sensor"] if entity.translation_key == key) == len(
            coordinator.zone_ids
        )
    for key in per_zone_binary:
        assert sum(
            1 for entity in entities["binary_sensor"] if entity.translation_key == key
        ) == len(coordinator.zone_ids)


def test_v02_entity_categories_split_explanations_from_readings() -> None:
    """Diagnostic only where the entity is troubleshooting evidence.

    ``combinations_24h`` / ``cycling_quality`` / ``unexplained_burns_24h`` grade
    the *boiler*; the dominant combination, the combination count and both
    comfort entities describe the house and are meant to be on the dashboard,
    where an entity category would hide them behind the diagnostic filter.

    V0.3 follows the same split (INTERFACES_V0.3.md 4.4): the four *verdicts*
    (``delta_t_quality`` / ``condensing_regime`` / ``hydraulic_verdict`` /
    ``curve_calibration_advice``) are diagnostic, while the two readings
    (``return_temperature`` / ``delta_t``) and the ``probe_fault`` flag stay
    visible - a probe that fell off the pipe is something to see, not to file.
    """
    from homeassistant.helpers.entity import EntityCategory

    diagnostic = {
        "combinations_24h",
        "cycling_quality",
        "unexplained_burns_24h",
        "delta_t_quality",
        "condensing_regime",
        "hydraulic_verdict",
        "curve_calibration_advice",
    }
    user_facing = {
        "dominant_zone_combination",
        "zone_combination_count",
        "comfort_verdict",
        "comfort_advice",
        "return_temperature",
        "delta_t",
    }
    flags = {"overheating", "underheating", "probe_fault"}

    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entities = build_platform_entities(coordinator)

    seen: set[str] = set()
    for entity in [*entities["sensor"], *entities["binary_sensor"]]:
        key = entity.translation_key
        seen.add(key)
        if key in diagnostic:
            assert entity.entity_category is EntityCategory.DIAGNOSTIC, key
        elif key in user_facing | flags:
            assert entity.entity_category is None, key

    # No key may be silently uncategorised by this test: every V0.3 addition must
    # appear in exactly one of the three sets above.
    assert diagnostic | user_facing | flags <= seen


# ---------------------------------------------------------------------------
# V0.3 system-level probe and delta-T entities
# ---------------------------------------------------------------------------


def test_v03_translation_keys_are_implemented_verbatim() -> None:
    """The V0.3 keys are a frozen contract (INTERFACES_V0.3.md section 3).

    All seven are system-level, so each must appear exactly once regardless of
    how many rooms are configured - a per-zone duplicate would silently change
    the entity count the setup tests assert.
    """
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entities = build_platform_entities(coordinator)

    sensor_keys = {entity.translation_key for entity in entities["sensor"]}
    binary_keys = {entity.translation_key for entity in entities["binary_sensor"]}

    for key in (
        "return_temperature",
        "delta_t",
        "delta_t_quality",
        "condensing_regime",
        "hydraulic_verdict",
        "curve_calibration_advice",
    ):
        assert key in sensor_keys, key
        assert sum(1 for e in entities["sensor"] if e.translation_key == key) == 1, key
    assert "probe_fault" in binary_keys
    assert sum(1 for e in entities["binary_sensor"] if e.translation_key == "probe_fault") == 1


def test_return_temperature_maps_the_probe_value() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ReturnTemperatureSensor(coordinator)
    assert entity.native_value == 37.0
    assert entity.device_class is SensorDeviceClass.TEMPERATURE

    attributes = entity.extra_state_attributes
    assert attributes["evaluated"] is True
    assert attributes["divergence"] == 8.0
    assert attributes["both_usable"] is True


def test_return_temperature_is_unknown_without_a_probe_but_still_available() -> None:
    """``None`` state, ``available`` True: "not fitted" is not "broken"."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=make_missing_probe_pair(),
        delta_t=make_empty_delta_t_report(),
        probes_configured=False,
    )
    entity = ReturnTemperatureSensor(coordinator)
    assert entity.native_value is None
    assert entity.available is True, "an unconfigured probe must not look broken"


def test_return_temperature_hides_an_implausible_probe_value() -> None:
    """A rejected reading must not be published as a pipe temperature.

    The DS18B20 85 °C sentinel is the dangerous case: it reads like a very hot
    return pipe, so the engine reports ``value=None`` with quality
    ``implausible`` and the entity must show unknown rather than 85.
    """
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=FakeProbePair(
            supply=FakeProbeAssessment(),
            return_=FakeProbeAssessment(
                entity_id="sensor.return_probe",
                value=None,
                quality=PROBE_IMPLAUSIBLE,
                reason_codes=("probe_sentinel_value_85c",),
            ),
        ),
    )
    entity = ReturnTemperatureSensor(coordinator)
    assert entity.native_value is None
    assert entity.available is True
    assert entity.extra_state_attributes["both_usable"] is False


def test_delta_t_sensor_uses_the_delta_device_class() -> None:
    """ΔT must not be ``TEMPERATURE``: that class applies an offset on conversion."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = DeltaTSensor(coordinator)

    assert entity.native_value == 8.0
    assert entity.device_class is SensorDeviceClass.TEMPERATURE_DELTA
    assert entity.device_class is not SensorDeviceClass.TEMPERATURE


def test_delta_t_sensor_publishes_the_not_a_power_measurement_marker() -> None:
    """Design constraint: ΔT is never heat output. The marker must reach the UI."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    attributes = DeltaTSensor(coordinator).extra_state_attributes

    assert attributes["is_power_measurement"] is False
    assert "temperature_difference_is_not_heat_power" in attributes["reason_codes"]
    assert attributes["average"] == 7.8
    assert attributes["samples"] == 30


def test_delta_t_sensor_is_unknown_without_probes() -> None:
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=make_missing_probe_pair(),
        delta_t=make_empty_delta_t_report(),
    )
    entity = DeltaTSensor(coordinator)
    assert entity.native_value is None
    assert entity.available is True


def test_delta_t_quality_sensor_maps_probe_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = DeltaTQualitySensor(coordinator)

    assert entity.native_value == PROBE_VALID
    assert entity.options == PROBE_QUALITIES

    coordinator.delta_t = FakeDeltaTReport(quality=PROBE_STALE)
    assert entity.native_value == PROBE_STALE

    # An untranslatable value must not be published.
    coordinator.delta_t = FakeDeltaTReport(quality="not_a_quality")
    assert entity.native_value is None


def test_delta_t_quality_publishes_every_probe_quality_verbatim() -> None:
    """All five ``PROBE_QUALITIES`` must be publishable as real states.

    ``PROBE_UNAVAILABLE`` used to be the literal ``"unavailable"``, which Home
    Assistant reserves for a broken entity: a healthy sensor on a probe-less
    installation then rendered exactly like a dead one. The value is now
    ``"probe_unavailable"`` (fixed in ``const.py``, guarded by
    ``tests/test_contracts.py``), so this entity publishes the code verbatim as
    the frozen contract in INTERFACES_V0.3.md section 3 requires.
    """
    from homeassistant.const import STATE_UNAVAILABLE

    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = DeltaTQualitySensor(coordinator)

    assert PROBE_UNAVAILABLE != STATE_UNAVAILABLE, (
        "PROBE_UNAVAILABLE must not collide with the reserved HA state"
    )
    for quality in PROBE_QUALITIES:
        coordinator.delta_t = FakeDeltaTReport(quality=quality)
        assert entity.native_value == quality

    # The probe-less installation is the common case: it must stay *available*
    # and publish a real, translatable state rather than looking broken.
    coordinator.probes = make_missing_probe_pair()
    coordinator.delta_t = FakeDeltaTReport(quality=PROBE_UNAVAILABLE)
    assert entity.available is True
    assert entity.native_value == PROBE_UNAVAILABLE


def test_delta_t_quality_reports_both_sides() -> None:
    """The worst-side verdict must be explainable by naming both sides."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=FakeProbePair(
            supply=FakeProbeAssessment(quality=PROBE_VALID),
            return_=FakeProbeAssessment(
                entity_id="sensor.return_probe", value=None, quality=PROBE_STALE
            ),
        ),
        delta_t=FakeDeltaTReport(quality=PROBE_STALE),
    )
    attributes = DeltaTQualitySensor(coordinator).extra_state_attributes
    assert attributes["supply_quality"] == PROBE_VALID
    assert attributes["return_quality"] == PROBE_STALE
    assert attributes["both_usable"] is False
    assert attributes["divergence"] is None


def test_condensing_regime_sensor_maps_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = CondensingRegimeSensor(coordinator)
    assert entity.native_value == CONDENSING_LIKELY

    coordinator.delta_t = FakeDeltaTReport(condensing=CONDENSING_UNKNOWN)
    assert entity.native_value == CONDENSING_UNKNOWN

    coordinator.delta_t = FakeDeltaTReport(condensing="bogus")
    assert entity.native_value is None


def test_hydraulic_verdict_sensor_maps_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = HydraulicVerdictSensor(coordinator)
    assert entity.native_value == HYDRAULIC_OK

    coordinator.delta_t = FakeDeltaTReport(hydraulic=HYDRAULIC_UNKNOWN)
    assert entity.native_value == HYDRAULIC_UNKNOWN
    # The explanation survives even when the verdict is unknown.
    assert entity.extra_state_attributes["stable_seconds"] == 1800.0


def test_curve_calibration_advice_is_advisory_only() -> None:
    """The curve belongs to the user; the entity must say it never applies it."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = CurveCalibrationAdviceSensor(coordinator)

    assert entity.native_value == CALIBRATION_RAISE_CURVE
    assert entity.extra_state_attributes["advisory_only"] is True

    coordinator.delta_t = FakeDeltaTReport(calibration=CALIBRATION_INSUFFICIENT_DATA)
    assert entity.native_value == CALIBRATION_INSUFFICIENT_DATA


def test_v03_verdict_sensors_are_unknown_not_zero_before_evaluation() -> None:
    """Before the first cycle the coordinator has no report; show unknown."""
    coordinator = FakeCoordinator(compensation=FakeCompensation(), probes=None, delta_t=None)
    for entity in (
        ReturnTemperatureSensor(coordinator),
        DeltaTSensor(coordinator),
        DeltaTQualitySensor(coordinator),
        CondensingRegimeSensor(coordinator),
        HydraulicVerdictSensor(coordinator),
        CurveCalibrationAdviceSensor(coordinator),
    ):
        assert entity.native_value is None, entity.translation_key
        assert entity.extra_state_attributes["evaluated"] is False


def test_probe_fault_is_off_when_no_probe_is_configured() -> None:
    """The most important semantic: no probe is not a fault.

    A permanently-on flag would train the user to ignore it, so an installation
    with no external probes must read ``off``.
    """
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=make_missing_probe_pair(),
        probe_fault=False,
        probes_configured=False,
    )
    entity = ProbeFaultBinarySensor(coordinator)

    assert entity.is_on is False
    assert entity.extra_state_attributes["configured"] is False
    assert entity.extra_state_attributes["affects_heating"] is False


def test_probe_fault_is_on_when_a_configured_probe_fails() -> None:
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        probes=FakeProbePair(
            supply=FakeProbeAssessment(quality=PROBE_VALID),
            return_=FakeProbeAssessment(
                entity_id="sensor.return_probe",
                value=None,
                quality=PROBE_UNAVAILABLE,
                reason_codes=("probe_not_configured",),
            ),
        ),
        probe_fault=True,
        probes_configured=True,
    )
    entity = ProbeFaultBinarySensor(coordinator)

    assert entity.is_on is True
    assert entity.extra_state_attributes["configured"] is True
    assert entity.extra_state_attributes["return_quality"] == PROBE_UNAVAILABLE
    # A failed probe degrades diagnostics only - never the heating.
    assert entity.extra_state_attributes["affects_heating"] is False


def test_probe_fault_is_not_a_diagnostic_entity() -> None:
    """A probe off the pipe is something to see, not to file away."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    assert ProbeFaultBinarySensor(coordinator).entity_category is None


def test_v03_entities_appear_once_per_system_not_per_zone() -> None:
    """Adding rooms must not multiply the seven V0.3 entities."""
    one = FakeCoordinator(zone_ids=("living_room",), compensation=FakeCompensation())
    three = FakeCoordinator(compensation=FakeCompensation())

    def v03_counts(coordinator: FakeCoordinator) -> dict[str, int]:
        entities = build_platform_entities(coordinator)
        keys = {
            "return_temperature",
            "delta_t",
            "delta_t_quality",
            "condensing_regime",
            "hydraulic_verdict",
            "curve_calibration_advice",
        }
        counts = dict.fromkeys(keys, 0)
        for entity in [*entities["sensor"], *entities["binary_sensor"]]:
            if entity.translation_key in counts:
                counts[entity.translation_key] += 1
        return counts

    assert v03_counts(one) == v03_counts(three)
    assert set(v03_counts(three).values()) == {1}


# ---------------------------------------------------------------------------
# Dashboard contract
# ---------------------------------------------------------------------------

#: Device names the shipped dashboard was written for. They are the reference
#: installation's: the system name from ``tests/test_integration_setup.py`` and
#: its two rooms. An entity id is ``<slug of device name>_<slug of ENGLISH
#: entity name>`` - see ``docs/DASHBOARD.md`` section 1.
DASHBOARD_SYSTEM_DEVICE = "我的家"
DASHBOARD_ZONE_DEVICES = {"living_room": "客厅地暖", "master_bedroom": "主卧地暖"}

#: The two KNX thermostats are the *user's* entities, created by their KNX
#: integration - not by ClimatePilot - so they are not derivable from
#: ``strings.json`` and are listed explicitly.
USER_OWNED_ENTITIES = {
    "climate.livingroom_floorheating",
    "climate.master_bedroom_floorheating",
}

#: translation_keys that belong to a zone rather than to the system device.
ZONE_SCOPED_KEYS = {
    ("sensor", "effective_setpoint"),
    ("sensor", "setpoint_source"),
    ("sensor", "zone_temperature"),
    ("sensor", "override_remaining"),
    ("sensor", "comfort_verdict"),
    ("sensor", "comfort_advice"),
    ("binary_sensor", "manual_override"),
    ("binary_sensor", "heat_request"),
    ("binary_sensor", "overheating"),
    ("binary_sensor", "underheating"),
    ("select", "zone_mode"),
    ("button", "resume_auto"),
    ("switch", "auxiliary_load"),
}


def load_english_entity_names() -> dict[str, dict[str, str]]:
    """English display names per platform/key, from ``translations/en.json``."""
    with (COMPONENT_DIR / "translations" / "en.json").open(encoding="utf-8") as handle:
        entity = json.load(handle)["entity"]
    return {
        platform: {key: meta["name"] for key, meta in keys.items()}
        for platform, keys in entity.items()
    }


def _yaml_strings(node: object):
    """Yield every string value in a parsed YAML document, keys excluded."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _yaml_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _yaml_strings(value)
    elif isinstance(node, str):
        yield node


#: Matches an entity id anywhere inside a card value (``entity: x``,
#: ``features[].entity``, a template string, ...).
_ENTITY_ID = re.compile(
    r"\b(?:sensor|binary_sensor|select|button|number|switch|climate)\.[a-z0-9_]+"
)


def dashboard_entity_ids() -> set[str]:
    """Every entity id the shipped dashboard actually references.

    Parsed as YAML rather than scanned as text: a commented-out card or an id
    quoted in prose would otherwise be counted as a live reference, and the
    completeness check below would then pass while the panel was missing a card.
    Comments never survive parsing, so this cannot be fooled by them.
    """
    document = yaml.safe_load(DASHBOARD_FILE.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for text in _yaml_strings(document):
        ids.update(_ENTITY_ID.findall(text))
    return ids


def expected_dashboard_ids() -> set[str]:
    """Derive the ids the dashboard *should* use, by HA's real slug rule.

    The subtlety that broke this once: Home Assistant builds an entity id from
    the localised name only when the UI language is one of the 41 in
    ``homeassistant.generated.languages.NATIVE_ENTITY_IDS``; **no Chinese variant
    is in that list**, so a zh-Hans install shows Chinese names but ASCII English
    ids. Deriving from ``en.json`` is therefore correct even for Chinese users,
    and deriving from ``zh-Hans.json`` produces plausible-looking ids that do not
    exist.

    This helper is a *derivation*, so it can only ever be as right as its rule -
    and it was wrong for exactly that reason: the first version of the dashboard
    used ``slugify(<Chinese device name> + <Chinese entity name>)``, and the
    check that "verified" it re-derived the same ids with the same wrong rule, so
    it passed while every id was broken. Two sides of a check sharing one
    assumption proves nothing about the assumption.

    The independent check is
    ``test_integration_setup.test_dashboard_entity_ids_exist_in_the_registry``,
    which reads ids out of a booted Home Assistant instead of computing them. If
    you change the rule here, that test is the one that must still agree.
    """
    names = load_english_entity_names()
    expected: set[str] = set()
    for platform, keys in names.items():
        for key, name in keys.items():
            slug = slugify(name)
            if (platform, key) in ZONE_SCOPED_KEYS:
                for device in DASHBOARD_ZONE_DEVICES.values():
                    expected.add(f"{platform}.{slugify(device)}_{slug}")
            else:
                expected.add(f"{platform}.{slugify(DASHBOARD_SYSTEM_DEVICE)}_{slug}")
    return expected


def test_dashboard_references_only_real_entity_ids() -> None:
    """Every id on the dashboard must be derivable from a translation_key.

    A dashboard full of plausible-looking but non-existent ids is worse than no
    dashboard: Home Assistant renders each one as an "entity not available" card
    and the user has no way to tell a typo from a missing integration.
    """
    referenced = dashboard_entity_ids()
    derivable = expected_dashboard_ids() | USER_OWNED_ENTITIES

    unknown = sorted(referenced - derivable)
    assert not unknown, (
        "dashboard references ids that no translation_key produces (and that are "
        f"not the user's own KNX entities): {unknown}"
    )


def test_dashboard_covers_every_translation_key() -> None:
    """... and the other direction: no entity is missing from the panel.

    Together with the test above this pins the dashboard exactly: one typo adds
    an unresolvable id, one deletion removes a translation_key from the panel.
    """
    referenced = dashboard_entity_ids()
    derivable = expected_dashboard_ids()

    missing = sorted(derivable - referenced)
    assert not missing, f"translation keys not shown on the dashboard: {missing}"


def test_dashboard_does_not_use_chinese_derived_slugs() -> None:
    """Guard the specific mistake this suite was fixed for.

    Chinese names slugify into something that *looks* like an entity id, so a
    wrong dashboard is self-consistent as long as the same assumption is used to
    check it. Two independent signals catch it: no Chinese-derived slug may
    appear, and the Chinese name must not equal the English one for these keys.
    """
    names_zh = {
        platform: {key: meta["name"] for key, meta in keys.items()}
        for platform, keys in json.loads(
            (COMPONENT_DIR / "translations" / "zh-Hans.json").read_text(encoding="utf-8")
        )["entity"].items()
    }
    names_en = load_english_entity_names()

    chinese_derived: set[str] = set()
    for platform, keys in names_zh.items():
        for key, name in keys.items():
            if name == names_en[platform][key]:
                continue  # same in both languages: cannot distinguish
            slug = slugify(name)
            if (platform, key) in ZONE_SCOPED_KEYS:
                for device in DASHBOARD_ZONE_DEVICES.values():
                    chinese_derived.add(f"{platform}.{slugify(device)}_{slug}")
            else:
                chinese_derived.add(f"{platform}.{slugify(DASHBOARD_SYSTEM_DEVICE)}_{slug}")

    offenders = sorted(dashboard_entity_ids() & chinese_derived)
    assert not offenders, (
        f"dashboard uses ids derived from Chinese names, which HA never creates: {offenders}"
    )
    # The guard is only meaningful if the two rules really do diverge.
    assert chinese_derived, "no Chinese-derived ids were computed; the check is vacuous"


# ---------------------------------------------------------------------------
# unique_id stability
# ---------------------------------------------------------------------------


def test_unique_ids_are_stable_and_structured() -> None:
    """unique_id == {entry_id}_{zone}_{key} and never changes across rebuilds."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    first = {
        entity.translation_key + ":" + getattr(entity, "_zone_id", ""): entity.unique_id
        for group in build_platform_entities(coordinator).values()
        for entity in group
    }
    second = {
        entity.translation_key + ":" + getattr(entity, "_zone_id", ""): entity.unique_id
        for group in build_platform_entities(coordinator).values()
        for entity in group
    }
    assert first == second

    # System-level entity: {entry_id}_{key}
    assert first["active_zones:"] == f"{ENTRY_ID}_active_zones"
    assert first["control_mode:"] == f"{ENTRY_ID}_control_mode"
    assert first["reassess:"] == f"{ENTRY_ID}_reassess"
    assert first["supply_temperature_override:"] == (f"{ENTRY_ID}_supply_temperature_override")

    # Zone-level entity: {entry_id}_{zone}_{key}
    assert first["effective_setpoint:living_room"] == (f"{ENTRY_ID}_living_room_effective_setpoint")
    assert first["zone_mode:master_bedroom"] == f"{ENTRY_ID}_master_bedroom_zone_mode"
    assert first["auxiliary_load:guest_bedroom"] == (f"{ENTRY_ID}_guest_bedroom_auxiliary_load")
    assert len(set(first.values())) == len(first), "unique_ids must not collide"


def test_unique_ids_isolated_per_entry() -> None:
    """Two systems must never share an entity registry row."""
    a = FakeCoordinator(compensation=FakeCompensation())
    b = FakeCoordinator(compensation=FakeCompensation())
    b.system_id = "entry_other"
    assert ActiveZonesSensor(a).unique_id != ActiveZonesSensor(b).unique_id
    assert (
        EffectiveSetpointSensor(a, "living_room").unique_id
        != EffectiveSetpointSensor(b, "living_room").unique_id
    )


def test_zone_entities_share_parent_device() -> None:
    """Zone entities are sub-devices of the system device (base.py contract).

    The parent link is expressed with whichever key this Home Assistant accepts,
    and the two are mutually exclusive: ``via_device`` (identifier tuple) was
    removed in favour of ``via_device_id`` (registry id). Asserting the wrong
    one either fails outright or - worse - passes while entity registration is
    broken, which is how the original bug reached a release.
    """
    from custom_components.climatepilot.base import _supports_via_device_id

    coordinator = FakeCoordinator(compensation=FakeCompensation())
    system_info = ActiveZonesSensor(coordinator).device_info
    zone_info = EffectiveSetpointSensor(coordinator, "living_room").device_info

    assert (DOMAIN, ENTRY_ID) in system_info["identifiers"]
    assert (DOMAIN, f"{ENTRY_ID}_living_room") in zone_info["identifiers"]
    assert zone_info["name"] == "客厅"

    if _supports_via_device_id():
        # Current HA: a registry id, resolved from the identifier tuple.
        assert "via_device" not in zone_info, "the removed parameter must not be used"
        # With no registry in this unit test the id cannot resolve, so the key is
        # absent rather than wrong - the integration test proves the linked case.
        assert "via_device_id" not in zone_info or isinstance(zone_info["via_device_id"], str)
    else:
        # Older HA: still the identifier tuple.
        assert zone_info["via_device"] == (DOMAIN, ENTRY_ID)


# ---------------------------------------------------------------------------
# Sensor values
# ---------------------------------------------------------------------------


def test_heating_state_sensor_maps_fsm_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = HeatingStateSensor(coordinator)

    coordinator.heat_state = FSM_HEATING
    assert entity.native_value == FSM_HEATING
    coordinator.heat_state = FSM_IDLE
    assert entity.native_value == FSM_IDLE

    # An unknown FSM string must not be published as a real state.
    coordinator.heat_state = "not_a_state"
    assert entity.native_value is None
    assert entity.options == list(ENTITY_STRINGS["sensor"]["heating_state"]["state"])


def test_active_zones_sensor_never_invents_zero() -> None:
    """Unknown demand is unknown, not 'no demand' (design doc 6.1)."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ActiveZonesSensor(coordinator)
    assert entity.native_value == 2

    coordinator.diagnostics_snapshot = {}
    assert entity.native_value is None


def test_suggested_supply_sensor_tolerates_missing_compensation() -> None:
    """``compensation`` is None before the first weather evaluation."""
    coordinator = FakeCoordinator(compensation=None)
    entity = SuggestedSupplyTempSensor(coordinator)
    assert entity.native_value is None
    assert entity.extra_state_attributes["evaluated"] is False

    coordinator.compensation = FakeCompensation(suggested=45.0)
    assert entity.native_value == 45.0
    assert entity.extra_state_attributes["should_apply"] is False


def test_outdoor_temperature_sensor_reports_stale_quality() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation(outdoor=None, quality="stale"))
    entity = OutdoorTemperatureSensor(coordinator)
    assert entity.native_value is None
    assert entity.extra_state_attributes["quality"] == "stale"


def test_supply_temperature_sensor_reads_boiler() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    assert SupplyTemperatureSensor(coordinator).native_value == 41.0


def test_burner_sensors_map_statistics() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    starts = BurnerStarts24hSensor(coordinator)
    duration = LastBurnDurationSensor(coordinator)

    assert starts.native_value == 9
    assert duration.native_value == 615.0
    # Design doc 8: DHW contamination must be visible on the entity.
    assert starts.extra_state_attributes["includes_dhw"] is False
    assert duration.extra_state_attributes["kind"] == "heating"


def test_burner_sensors_flag_dhw_contamination() -> None:
    """A generic flame sensor must be labelled as possibly including DHW."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        burner=make_burner_snapshot(includes_dhw=True, kind="unknown"),
    )
    starts = BurnerStarts24hSensor(coordinator)
    assert starts.extra_state_attributes["includes_dhw"] is True
    assert LastBurnDurationSensor(coordinator).extra_state_attributes["kind"] == "unknown"


def test_burner_sensors_are_unknown_before_first_cycle() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.diagnostics_snapshot = {}
    assert BurnerStarts24hSensor(coordinator).native_value is None
    assert LastBurnDurationSensor(coordinator).native_value is None


def test_effective_setpoint_sensor_explains_value() -> None:
    """Design doc 10.3: base, delta, source and reasons must be visible."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = EffectiveSetpointSensor(coordinator, "living_room")
    assert entity.native_value == 21.5

    attributes = entity.extra_state_attributes
    assert attributes["base"] == 21.0
    assert attributes["adjustment_delta"] == 0.5
    assert attributes["source"] == SOURCE_SCHEDULE
    assert attributes["reason_codes"] == ["schedule_transition"]


def test_effective_setpoint_falls_back_to_zone_state() -> None:
    """Before the first arbitration the zone's own value is the honest answer."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.zone_decisions = {}
    entity = EffectiveSetpointSensor(coordinator, "living_room")
    assert entity.native_value == 21.5


def test_setpoint_source_sensor_maps_source_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = SetpointSourceSensor(coordinator, "living_room")
    assert entity.native_value == SOURCE_SCHEDULE

    coordinator.zone_decisions["living_room"] = SetpointDecision(
        zone_id="living_room",
        effective=22.0,
        base=21.0,
        source=SOURCE_MANUAL,
        reason_codes=("manual_override",),
    )
    assert entity.native_value == SOURCE_MANUAL

    coordinator.zone_decisions["living_room"] = SetpointDecision(
        zone_id="living_room", effective=22.0, base=21.0, source="bogus"
    )
    assert entity.native_value is None


def test_setpoint_source_sensor_unknown_without_decision() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.zone_decisions = {}
    entity = SetpointSourceSensor(coordinator, "living_room")
    assert entity.native_value is None
    assert entity.extra_state_attributes["reason_codes"] == []


def test_zone_temperature_sensor_reports_measurement() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ZoneTemperatureSensor(coordinator, "master_bedroom")
    assert entity.native_value == 20.5
    assert entity.extra_state_attributes["hvac_action"] == HVAC_ACTION_HEATING


def test_override_remaining_sensor_counts_minutes() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    zone = coordinator.zones["living_room"]
    entity = OverrideRemainingSensor(coordinator, "living_room")

    # No override at all.
    assert entity.native_value is None
    assert entity.extra_state_attributes == {"active": False}

    # Fixed-duration override: minutes remaining.
    zone.manual_override = ManualOverride(
        value=22.0,
        created_at=T0,
        policy="fixed_duration",
        until=datetime.now(UTC) + timedelta(minutes=90),
    )
    assert entity.native_value == pytest.approx(90.0, abs=1.0)
    assert entity.extra_state_attributes["active"] is True
    assert entity.extra_state_attributes["value"] == 22.0


def test_override_remaining_sensor_is_unknown_for_open_ended_policies() -> None:
    """A next_schedule override has no deadline; do not fabricate a countdown."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.zones["living_room"].manual_override = ManualOverride(
        value=22.0, created_at=T0, policy="next_schedule"
    )
    entity = OverrideRemainingSensor(coordinator, "living_room")
    assert entity.native_value is None
    assert entity.extra_state_attributes["policy"] == "next_schedule"


# ---------------------------------------------------------------------------
# Binary sensors
# ---------------------------------------------------------------------------


def test_manual_override_binary_sensor() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ManualOverrideBinarySensor(coordinator, "living_room")
    assert entity.is_on is False

    coordinator.zones["living_room"].manual_override = ManualOverride(value=23.0, created_at=T0)
    assert entity.is_on is True
    assert entity.extra_state_attributes["value"] == 23.0


def test_heat_request_requires_a_real_action() -> None:
    """``heat`` mode alone is not demand (design doc 6.1)."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    zone = coordinator.zones["living_room"]
    entity = HeatRequestBinarySensor(coordinator, "living_room")

    zone.hvac_mode = "heat"
    zone.hvac_action = HVAC_ACTION_HEATING
    assert entity.is_on is True

    zone.hvac_action = HVAC_ACTION_IDLE
    assert entity.is_on is False


def test_heat_request_is_unknown_when_feedback_is_missing() -> None:
    """An unreadable thermostat is unknown, never 'off'."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    zone = coordinator.zones["living_room"]
    entity = HeatRequestBinarySensor(coordinator, "living_room")

    zone.feedback_quality = FEEDBACK_UNAVAILABLE
    assert entity.is_on is None

    zone.feedback_quality = FEEDBACK_VALID
    zone.hvac_mode = "unavailable"
    assert entity.is_on is None

    zone.hvac_mode = "heat"
    zone.hvac_action = None
    assert entity.is_on is None


def test_short_cycle_alert_reads_snapshot_and_hides_when_blind() -> None:
    """A lost burner reading must not be shown as 'no short cycling'."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ShortCycleAlertBinarySensor(coordinator)
    assert entity.is_on is True
    assert entity.extra_state_attributes["short_cycles_24h"] == 2

    coordinator.diagnostics_snapshot = {
        "burner": make_burner_snapshot(quality="uncertain", short_cycle_alert=None)
    }
    assert entity.available is False
    assert entity.is_on is None


def test_short_cycle_alert_false_without_snapshot() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.diagnostics_snapshot = {}
    entity = ShortCycleAlertBinarySensor(coordinator)
    assert entity.available is False


def test_degraded_binary_sensor_tracks_state_and_faults() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = DegradedBinarySensor(coordinator)
    assert entity.is_on is False

    coordinator.heat_state = FSM_DEGRADED
    assert entity.is_on is True
    assert "heat_source_degraded" in entity.extra_state_attributes["reasons"]

    coordinator.heat_state = FSM_IDLE
    coordinator.diagnostics_snapshot["unknown_zones"] = 1
    assert entity.is_on is True
    assert "zone_state_unknown" in entity.extra_state_attributes["reasons"]


def test_degraded_binary_sensor_flags_unusable_weather() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation(quality=FEEDBACK_UNAVAILABLE))
    entity = DegradedBinarySensor(coordinator)
    assert "weather_data_unusable" in entity.extra_state_attributes["reasons"]


# ---------------------------------------------------------------------------
# V0.2 system-level sensors: demand combinations
# ---------------------------------------------------------------------------


def test_dominant_zone_combination_publishes_the_engine_key() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = DominantZoneCombinationSensor(coordinator)
    assert entity.native_value == "living_room+master_bedroom"

    attributes = entity.extra_state_attributes
    assert attributes["evaluated"] is True
    assert attributes["dominant_share"] == 0.62
    assert attributes["current_key"] == "living_room+master_bedroom"
    assert attributes["distinct_count"] == 2


def test_dominant_zone_combination_reports_none_demand_honestly() -> None:
    """``none`` is a measurement ("nobody asked for heat"), not a missing value.

    Conflating the two would make a summer afternoon look like a broken sensor.
    """
    report = FakeCombinationReport(
        dominant_key="none",
        dominant_share=0.0,
        distinct_count=0,
        total_demand_seconds=0.0,
        stats=(("none", 7200.0),),
        current_key="none",
        current_seconds=7200.0,
    )
    coordinator = FakeCoordinator(compensation=FakeCompensation(), combinations=report)
    assert DominantZoneCombinationSensor(coordinator).native_value == "none"


def test_dominant_zone_combination_flags_a_diffuse_pattern() -> None:
    """A leader below the threshold must not be presented as *the* pattern."""
    report = FakeCombinationReport(dominant_share=0.2)
    coordinator = FakeCoordinator(compensation=FakeCompensation(), combinations=report)
    attributes = DominantZoneCombinationSensor(coordinator).extra_state_attributes
    assert attributes["has_dominant_mode"] is False
    assert attributes["dominant_threshold"] == 0.3


def test_v02_system_sensors_are_unknown_before_the_first_cycle() -> None:
    """No report yet is unknown, never zero (design doc 6.1)."""
    coordinator = FakeCoordinator(compensation=FakeCompensation(), combinations=None)
    for entity in (
        DominantZoneCombinationSensor(coordinator),
        ZoneCombinationCountSensor(coordinator),
        Combinations24hSensor(coordinator),
    ):
        assert entity.native_value is None, type(entity).__name__
        assert entity.extra_state_attributes == {"evaluated": False}


def test_zone_combination_count_reads_distinct_count() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ZoneCombinationCountSensor(coordinator)
    assert entity.native_value == 2

    attributes = entity.extra_state_attributes
    assert attributes["dominant_key"] == "living_room+master_bedroom"
    # Groups include the empty set; the count deliberately does not.
    assert attributes["total_groups"] == 3
    assert attributes["combinations"] == ["living_room+master_bedroom", "living_room", "none"]


def test_combinations_24h_publishes_demand_seconds_not_the_count() -> None:
    """The state is demand time; ``zone_combination_count`` already has the count.

    Two entities publishing one number would be dashboard noise, so the second
    one carries the quantity that graphs and can back a statistic instead.
    """
    from homeassistant.const import UnitOfTime

    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = Combinations24hSensor(coordinator)
    assert entity.native_value == 5400.0
    assert entity.native_unit_of_measurement == UnitOfTime.SECONDS

    attributes = entity.extra_state_attributes
    # The engine's own export travels verbatim, so entity and diagnostics agree.
    assert attributes["total_demand_seconds"] == 5400.0
    assert attributes["dominant_key"] == "living_room+master_bedroom"
    # ... and the minutes copy saves the user the division.
    assert attributes["total_demand_minutes"] == 90.0


# ---------------------------------------------------------------------------
# V0.2 system-level sensors: combustion quality
# ---------------------------------------------------------------------------


def test_cycling_quality_sensor_maps_quality_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = CyclingQualitySensor(coordinator)
    assert entity.native_value == QUALITY_GOOD
    assert entity.options == [QUALITY_GOOD, QUALITY_FAIR, QUALITY_POOR, QUALITY_UNKNOWN]

    attributes = entity.extra_state_attributes
    assert attributes["reason"] == "average 720s over 9 heating burns"
    assert attributes["grade_heating_24h"] == {"good": 7, "fair": 2}
    # DHW is graded separately precisely so it cannot drag the heating grade.
    assert attributes["grade_dhw_24h"] == {"poor": 1}


def test_cycling_quality_sensor_refuses_an_untranslatable_value() -> None:
    """A state the enum translation cannot render must not be published."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        burner=make_burner_snapshot(cycling_quality="excellent"),
    )
    assert CyclingQualitySensor(coordinator).native_value is None


def test_cycling_quality_unknown_without_a_burner_reading() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.diagnostics_snapshot = {}
    entity = CyclingQualitySensor(coordinator)
    assert entity.native_value == QUALITY_UNKNOWN


def test_unexplained_burns_sensor_reads_the_diagnostic_count() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = UnexplainedBurns24hSensor(coordinator)
    assert entity.native_value == 1

    attributes = entity.extra_state_attributes
    assert attributes["demand_active"] is True
    # The V0.1 spelling of the same measurement stays available.
    assert attributes["burns_without_demand_24h"] == 1
    assert attributes["cycling_quality"] == QUALITY_GOOD


def test_unexplained_burns_are_unknown_when_the_burner_is_blind() -> None:
    """Zero unexplained burns and 'we cannot see the burner' must differ."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        burner=make_burner_snapshot(quality="uncertain"),
    )
    entity = UnexplainedBurns24hSensor(coordinator)
    assert entity.native_value is None
    assert entity.extra_state_attributes["quality"] == "uncertain"


# ---------------------------------------------------------------------------
# V0.2 zone-level comfort sensors
# ---------------------------------------------------------------------------


def test_comfort_verdict_sensor_maps_verdict_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ComfortVerdictSensor(coordinator, "living_room")
    assert entity.native_value == COMFORT_OK
    assert entity.options == [
        COMFORT_OK,
        COMFORT_OVERHEATING,
        COMFORT_UNDERHEATING,
        COMFORT_UNKNOWN,
    ]

    coordinator.comfort["living_room"] = FakeComfortVerdict(
        "living_room",
        verdict=COMFORT_OVERHEATING,
        advice=ADVICE_LOWER_SUPPLY,
        average_deviation=1.4,
        overheated_seconds=2400.0,
        reason_codes=("overheat_dwell_exceeded",),
    )
    assert entity.native_value == COMFORT_OVERHEATING
    attributes = entity.extra_state_attributes
    assert attributes["average_deviation"] == 1.4
    assert attributes["overheated_seconds"] == 2400.0
    assert attributes["reason_codes"] == ["overheat_dwell_exceeded"]


def test_comfort_verdict_is_unknown_without_samples() -> None:
    """An unsampled room is unmeasured, not 'ok' and not unavailable.

    The entity is available and reports ``unknown`` because the integration is
    working fine -- it simply has nothing to say about this room yet.
    """
    coordinator = FakeCoordinator(compensation=FakeCompensation(), comfort={})
    entity = ComfortVerdictSensor(coordinator, "living_room")
    assert entity.native_value == COMFORT_UNKNOWN
    assert entity.extra_state_attributes["evaluated"] is False


def test_comfort_advice_sensor_maps_advice_constants() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ComfortAdviceSensor(coordinator, "master_bedroom")
    assert entity.native_value == ADVICE_NONE

    coordinator.comfort["master_bedroom"] = FakeComfortVerdict(
        "master_bedroom",
        verdict=COMFORT_UNDERHEATING,
        advice=ADVICE_RAISE_SUPPLY,
        average_deviation=-1.8,
        reason_codes=("underheat_dwell_exceeded",),
    )
    assert entity.native_value == ADVICE_RAISE_SUPPLY
    assert entity.extra_state_attributes["verdict"] == COMFORT_UNDERHEATING
    assert entity.extra_state_attributes["average_deviation"] == -1.8


def test_comfort_advice_is_none_without_samples() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation(), comfort={})
    entity = ComfortAdviceSensor(coordinator, "living_room")
    assert entity.native_value == ADVICE_NONE
    assert entity.extra_state_attributes["evaluated"] is False


def test_comfort_sensors_tolerate_an_unknown_verdict_string() -> None:
    """The engine is the source of truth; a stray code must not crash the UI."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        comfort={
            "living_room": FakeComfortVerdict(
                "living_room", verdict="boiling", advice="turn_it_off"
            )
        },
    )
    assert ComfortVerdictSensor(coordinator, "living_room").native_value == COMFORT_UNKNOWN
    assert ComfortAdviceSensor(coordinator, "living_room").native_value == ADVICE_NONE


# ---------------------------------------------------------------------------
# V0.2 zone-level comfort flags
# ---------------------------------------------------------------------------


def test_overheating_flag_reads_the_engine_verdict() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = OverheatingBinarySensor(coordinator, "living_room")
    assert entity.is_on is False
    assert entity.available is True
    assert entity.extra_state_attributes["dwell_seconds"] == 1800.0

    coordinator.comfort["living_room"] = FakeComfortVerdict(
        "living_room", verdict=COMFORT_OVERHEATING, advice=ADVICE_LOWER_SUPPLY
    )
    assert entity.is_on is True
    assert entity.extra_state_attributes["verdict"] == COMFORT_OVERHEATING


def test_underheating_flag_reads_the_engine_verdict() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = UnderheatingBinarySensor(coordinator, "living_room")
    assert entity.is_on is False
    assert entity.extra_state_attributes["dwell_seconds"] == 3600.0

    coordinator.comfort["living_room"] = FakeComfortVerdict(
        "living_room", verdict=COMFORT_UNDERHEATING, advice=ADVICE_RAISE_SUPPLY
    )
    assert entity.is_on is True


def test_comfort_flags_are_unavailable_when_the_verdict_is_unknown() -> None:
    """Otherwise the flag would read as 'checked, and fine' while nothing was checked."""
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(),
        comfort={
            "living_room": FakeComfortVerdict(
                "living_room", verdict=COMFORT_UNKNOWN, advice=ADVICE_NONE
            )
        },
    )
    entity = OverheatingBinarySensor(coordinator, "living_room")
    assert entity.available is False
    assert entity.is_on is None
    assert UnderheatingBinarySensor(coordinator, "living_room").is_on is None


def test_comfort_flags_are_unavailable_without_a_sample() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation(), comfort={})
    entity = OverheatingBinarySensor(coordinator, "living_room")
    assert entity.available is False
    assert entity.is_on is None
    assert entity.extra_state_attributes["evaluated"] is False


def test_comfort_flags_are_mutually_exclusive() -> None:
    """One verdict drives both flags, so both can never be on at once."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    over = OverheatingBinarySensor(coordinator, "living_room")
    under = UnderheatingBinarySensor(coordinator, "living_room")

    for verdict in (COMFORT_OK, COMFORT_OVERHEATING, COMFORT_UNDERHEATING, COMFORT_UNKNOWN):
        coordinator.comfort["living_room"] = FakeComfortVerdict("living_room", verdict=verdict)
        assert not (over.is_on and under.is_on), verdict


def test_comfort_flags_degrade_when_the_analyser_is_absent() -> None:
    """A missing dwell setting is reported as unknown, never as a made-up number."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    del coordinator.comfort_analyser
    entity = OverheatingBinarySensor(coordinator, "living_room")
    assert entity.extra_state_attributes["dwell_seconds"] is None


def test_comfort_flags_are_available_without_ha() -> None:
    """Availability must not depend on an entity being attached to a platform."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.last_update_success = False
    entity = OverheatingBinarySensor(coordinator, "living_room")
    assert entity.available is False
    # ``is_on`` must stay answerable even then: HA writes the state once more as
    # it tears the entity down, and a raise there becomes a logged error.
    assert entity.is_on is False


# ---------------------------------------------------------------------------
# Selects
# ---------------------------------------------------------------------------


def test_control_mode_select_reads_and_writes() -> None:
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(), control_mode=CONTROL_MODE_OBSERVE
    )
    entity = ControlModeSelect(coordinator)
    assert entity.current_option == CONTROL_MODE_OBSERVE
    assert entity.options == ["observe", "coordinate", "paused"]


@pytest.mark.asyncio
async def test_control_mode_select_delegates_to_coordinator() -> None:
    coordinator = FakeCoordinator(
        compensation=FakeCompensation(), control_mode=CONTROL_MODE_OBSERVE
    )
    entity = ControlModeSelect(coordinator)
    await entity.async_select_option(CONTROL_MODE_COORDINATE)
    coordinator.async_set_control_mode.assert_awaited_once_with(CONTROL_MODE_COORDINATE)


def test_control_mode_select_unknown_option() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.control_mode = "bogus"
    assert ControlModeSelect(coordinator).current_option is None


def test_zone_mode_select_reads_and_writes() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ZoneModeSelect(coordinator, "guest_bedroom")
    assert entity.current_option == ZONE_MODE_AUTO
    assert entity.options == ["auto", "comfort", "eco", "away", "manual"]

    coordinator.zones["guest_bedroom"].mode = ZONE_MODE_ECO
    assert entity.current_option == ZONE_MODE_ECO


@pytest.mark.asyncio
async def test_zone_mode_select_delegates_to_coordinator() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = ZoneModeSelect(coordinator, "guest_bedroom")
    await entity.async_select_option(ZONE_MODE_ECO)
    coordinator.async_set_zone_mode.assert_awaited_once_with("guest_bedroom", ZONE_MODE_ECO)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_auto_button_calls_coordinator_with_its_zone() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    button = ResumeAutoButton(coordinator, "master_bedroom")
    await button.async_press()
    coordinator.async_resume_auto.assert_awaited_once_with("master_bedroom")


@pytest.mark.asyncio
async def test_reassess_button_calls_coordinator() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    button = ReassessButton(coordinator)
    await button.async_press()
    coordinator.async_reassess.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# Number
# ---------------------------------------------------------------------------


def test_supply_number_uses_default_limits_without_capabilities() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = SupplyTemperatureOverrideNumber(coordinator)
    assert entity.native_min_value == 35.0
    assert entity.native_max_value == 55.0


def test_supply_number_prefers_device_limits() -> None:
    """Device-reported limits win over the built-in defaults (design doc 6.3)."""
    capabilities = SimpleNamespace(
        entity_id="climate.rinnai_heating_control",
        min_temp=30.0,
        max_temp=60.0,
        semantics_verified=True,
    )
    coordinator = FakeCoordinator(compensation=FakeCompensation(), capabilities=capabilities)
    entity = SupplyTemperatureOverrideNumber(coordinator)
    assert entity.native_min_value == 30.0
    assert entity.native_max_value == 60.0
    assert entity.extra_state_attributes["semantics_verified"] is True


def test_supply_number_ignores_inverted_device_limits() -> None:
    capabilities = SimpleNamespace(
        entity_id="climate.rinnai_heating_control",
        min_temp=60.0,
        max_temp=30.0,
        semantics_verified=False,
    )
    coordinator = FakeCoordinator(compensation=FakeCompensation(), capabilities=capabilities)
    entity = SupplyTemperatureOverrideNumber(coordinator)
    assert entity.native_min_value < entity.native_max_value


def test_supply_number_value_is_none_before_evaluation() -> None:
    coordinator = FakeCoordinator(compensation=None)
    coordinator.weather = FakeWeather(last_applied=None)
    entity = SupplyTemperatureOverrideNumber(coordinator)
    assert entity.native_value is None


@pytest.mark.asyncio
async def test_supply_number_delegates_to_coordinator() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = SupplyTemperatureOverrideNumber(coordinator)
    await entity.async_set_native_value(44.0)
    coordinator.async_set_supply_temperature.assert_awaited_once_with(44.0)


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


def test_auxiliary_load_switch_reflects_permission() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = AuxiliaryLoadSwitch(coordinator, "living_room")
    assert entity.is_on is False
    assert entity.extra_state_attributes["applied"] is False

    coordinator.zones["living_room"].aux_load_enabled = True
    coordinator.zones["living_room"].auto_adjustments["aux_load"] = 0.5
    assert entity.is_on is True
    assert entity.extra_state_attributes["applied"] is True
    assert entity.extra_state_attributes["applied_delta"] == 0.5


@pytest.mark.asyncio
async def test_auxiliary_load_switch_delegates_to_coordinator() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entity = AuxiliaryLoadSwitch(coordinator, "living_room")

    await entity.async_turn_on()
    coordinator.async_set_zone_aux_load.assert_awaited_once_with("living_room", True)

    coordinator.async_set_zone_aux_load.reset_mock()
    await entity.async_turn_off()
    coordinator.async_set_zone_aux_load.assert_awaited_once_with("living_room", False)


# ---------------------------------------------------------------------------
# Platform setup wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_setup_adds_expected_entities_per_platform() -> None:
    """Each platform's async_setup_entry must add its entities exactly once."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    hass = make_hass(coordinator)

    from custom_components.climatepilot import binary_sensor as bs_platform
    from custom_components.climatepilot import button as button_platform
    from custom_components.climatepilot import number as number_platform
    from custom_components.climatepilot import select as select_platform
    from custom_components.climatepilot import sensor as sensor_platform
    from custom_components.climatepilot import switch as switch_platform

    expectations = [
        (
            sensor_platform,
            EXPECTED_PER_PLATFORM["sensor"][0]
            + EXPECTED_PER_PLATFORM["sensor"][1] * len(coordinator.zone_ids),
        ),
        (
            bs_platform,
            EXPECTED_PER_PLATFORM["binary_sensor"][0]
            + EXPECTED_PER_PLATFORM["binary_sensor"][1] * len(coordinator.zone_ids),
        ),
        (select_platform, EXPECTED_PER_PLATFORM["select"][0] + len(coordinator.zone_ids)),
        (button_platform, EXPECTED_PER_PLATFORM["button"][0] + len(coordinator.zone_ids)),
        (number_platform, EXPECTED_PER_PLATFORM["number"][0]),
        (switch_platform, len(coordinator.zone_ids)),
    ]
    for module, expected in expectations:
        added: list = []
        await module.async_setup_entry(hass, entry, added.extend)
        assert len(added) == expected, f"{module.__name__} added {len(added)}"
        # Entities are built but not yet attached to hass; unique_id works.
        assert all(entity.unique_id for entity in added)
        assert all(entity.translation_key for entity in added)


@pytest.mark.asyncio
async def test_platform_setup_with_no_zones() -> None:
    """A system with no zones must still expose its system-level entities."""
    coordinator = FakeCoordinator(zone_ids=(), compensation=FakeCompensation())
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    hass = make_hass(coordinator)

    from custom_components.climatepilot import sensor as sensor_platform

    added: list = []
    await sensor_platform.async_setup_entry(hass, entry, added.extend)
    assert len(added) == EXPECTED_PER_PLATFORM["sensor"][0]


# ---------------------------------------------------------------------------
# Integration entry point
# ---------------------------------------------------------------------------


def test_platforms_include_climate_when_module_present() -> None:
    from homeassistant.const import Platform

    assert Platform.SELECT in PLATFORMS
    assert Platform.SENSOR in PLATFORMS
    assert Platform.BINARY_SENSOR in PLATFORMS
    assert Platform.BUTTON in PLATFORMS
    assert Platform.NUMBER in PLATFORMS
    assert Platform.SWITCH in PLATFORMS
    # climate.py ships with this integration, so it must be forwarded.
    assert Platform.CLIMATE in PLATFORMS


def test_platforms_are_unique() -> None:
    assert len(PLATFORMS) == len(set(PLATFORMS))


@pytest.mark.asyncio
async def test_async_setup_entry_registers_coordinator_services_and_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        data={},
        options={},
        add_update_listener=Mock(return_value=Mock()),
        async_on_unload=Mock(),
    )
    hass = SimpleNamespace(
        data={},
        services=SimpleNamespace(
            has_service=Mock(return_value=False),
            async_register=Mock(),
            async_remove=Mock(),
        ),
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    coordinator_cls = Mock(return_value=coordinator)
    coordinator.async_config_entry_first_refresh = AsyncMock()
    coordinator.async_setup_listeners = Mock()
    coordinator.async_shutdown_listeners = Mock()

    monkeypatch.setattr("custom_components.climatepilot.ClimatePilotCoordinator", coordinator_cls)
    registered: dict[str, object] = {}

    def fake_admin_register(hass_, domain, service, func, schema, **kwargs):
        registered[service] = func

    monkeypatch.setattr(
        "custom_components.climatepilot.async_register_admin_service",
        fake_admin_register,
    )

    assert await async_setup_entry(hass, entry) is True

    coordinator_cls.assert_called_once_with(hass, entry)
    coordinator.async_config_entry_first_refresh.assert_awaited_once_with()
    coordinator.async_setup_listeners.assert_called_once_with()
    assert hass.data[DOMAIN][ENTRY_ID] is coordinator

    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    forwarded = hass.config_entries.async_forward_entry_setups.await_args.args[1]
    assert forwarded == PLATFORMS

    assert set(registered) == {
        SERVICE_RESUME_AUTO,
        SERVICE_SET_SUPPLY_TEMPERATURE,
        SERVICE_REASSESS,
    }


@pytest.mark.asyncio
async def test_async_unload_entry_removes_coordinator_and_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.async_shutdown_listeners = Mock()
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    hass = SimpleNamespace(
        data={DOMAIN: {ENTRY_ID: coordinator}},
        services=SimpleNamespace(async_remove=Mock()),
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
    )

    assert await async_unload_entry(hass, entry) is True

    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)
    assert ENTRY_ID not in hass.data[DOMAIN]
    coordinator.async_shutdown_listeners.assert_called_once_with()
    removed = {call.args[1] for call in hass.services.async_remove.call_args_list}
    assert removed == {
        SERVICE_RESUME_AUTO,
        SERVICE_SET_SUPPLY_TEMPERATURE,
        SERVICE_REASSESS,
    }


@pytest.mark.asyncio
async def test_async_unload_entry_keeps_data_when_platforms_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.async_shutdown_listeners = Mock()
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    hass = SimpleNamespace(
        data={DOMAIN: {ENTRY_ID: coordinator}},
        services=SimpleNamespace(async_remove=Mock()),
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=False)),
    )

    assert await async_unload_entry(hass, entry) is False
    assert hass.data[DOMAIN][ENTRY_ID] is coordinator
    coordinator.async_shutdown_listeners.assert_not_called()
    hass.services.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_services_not_removed_while_another_entry_is_loaded() -> None:
    """Services belong to the domain, not to one entry."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    coordinator.async_shutdown_listeners = Mock()
    other = FakeCoordinator(compensation=FakeCompensation())
    other.system_id = "entry_other"
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    hass = SimpleNamespace(
        data={DOMAIN: {ENTRY_ID: coordinator, "entry_other": other}},
        services=SimpleNamespace(async_remove=Mock()),
        config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
    )

    assert await async_unload_entry(hass, entry) is True
    hass.services.async_remove.assert_not_called()


# ---------------------------------------------------------------------------
# Service handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_auto_service_with_zone() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={CONF_ZONE_ID: "living_room"})
    await _async_handle_resume_auto(call)
    coordinator.async_resume_auto.assert_awaited_once_with("living_room")


@pytest.mark.asyncio
async def test_resume_auto_service_without_zone_resumes_all() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={})
    await _async_handle_resume_auto(call)
    coordinator.async_resume_auto.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_resume_auto_service_accepts_zone_entity_id() -> None:
    """The field is documented as a zone id, but an entity id also resolves."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={CONF_ZONE_ID: "climate.livingroom_floorheating"})
    await _async_handle_resume_auto(call)
    coordinator.async_resume_auto.assert_awaited_once_with("living_room")


@pytest.mark.asyncio
async def test_resume_auto_service_rejects_foreign_zone() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={CONF_ZONE_ID: "climate.kitchen"})
    await _async_handle_resume_auto(call)
    coordinator.async_resume_auto.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_supply_temperature_service_handler() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={"temperature": 45.5})
    await _async_handle_set_supply_temperature(call)
    coordinator.async_set_supply_temperature.assert_awaited_once_with(45.5)


@pytest.mark.asyncio
async def test_reassess_service_handler() -> None:
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    hass = make_hass(coordinator)
    call = SimpleNamespace(hass=hass, data={})
    await _async_handle_reassess(call)
    coordinator.async_reassess.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_service_handlers_tolerate_no_coordinator() -> None:
    hass = make_hass()
    await _async_handle_reassess(SimpleNamespace(hass=hass, data={}))
    await _async_handle_resume_auto(SimpleNamespace(hass=hass, data={}))


# ---------------------------------------------------------------------------
# Config entry migration
# ---------------------------------------------------------------------------


def make_entry(**overrides: object) -> SimpleNamespace:
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        version=0,
        minor_version=1,
        data={},
        options={},
    )
    for key, value in overrides.items():
        setattr(entry, key, value)
    return entry


@pytest.mark.asyncio
async def test_migration_v0_dict_zones_to_list() -> None:
    """v0 stored zones as a mapping; v1 requires a list of dicts."""
    entry = make_entry(
        data={"system_name": "我家采暖"},
        options={
            "zones": {
                "living_room": {"entity_id": "climate.livingroom_floorheating"},
                "master_bedroom": {"entity_id": "climate.master_bedroom_floorheating"},
            }
        },
    )
    updates: dict[str, object] = {}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=lambda ent, **kw: updates.update(kw))
        )
    )

    assert await async_migrate_entry(hass, entry) is True

    zones = updates["options"]["zones"]
    assert isinstance(zones, list)
    assert [zone["zone_id"] for zone in zones] == ["living_room", "master_bedroom"]
    assert zones[0]["entity_id"] == "climate.livingroom_floorheating"
    # A missing control mode must default to observe, never to coordinate.
    assert updates["options"]["control_mode"] == CONTROL_MODE_OBSERVE
    assert updates["options"]["system_name"] == "我家采暖"
    assert updates["version"] == 1


@pytest.mark.asyncio
async def test_migration_drops_zoneless_entries() -> None:
    entry = make_entry(options={"zones": {"broken": {"name": "no entity"}}})
    updates: dict[str, object] = {}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=lambda ent, **kw: updates.update(kw))
        )
    )

    assert await async_migrate_entry(hass, entry) is True
    assert updates["options"]["zones"] == []


@pytest.mark.asyncio
async def test_migration_preserves_valid_control_mode() -> None:
    entry = make_entry(options={"control_mode": CONTROL_MODE_COORDINATE})
    updates: dict[str, object] = {}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=lambda ent, **kw: updates.update(kw))
        )
    )

    assert await async_migrate_entry(hass, entry) is True
    assert updates["options"]["control_mode"] == CONTROL_MODE_COORDINATE


@pytest.mark.asyncio
async def test_migration_is_noop_for_current_version() -> None:
    entry = make_entry(version=1)
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))
    assert await async_migrate_entry(hass, entry) is True
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_migration_refuses_future_version() -> None:
    """A newer entry must not be silently rewritten."""
    entry = make_entry(version=99)
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=Mock()))
    assert await async_migrate_entry(hass, entry) is False
    hass.config_entries.async_update_entry.assert_not_called()


# ---------------------------------------------------------------------------
# Contract test: the double must match the real coordinator
# ---------------------------------------------------------------------------


class TestCoordinatorDoubleMatchesReality:
    """Guard against duck-type drift.

    Every other test in this module drives ``FakeCoordinator``. That is only
    meaningful while the double still resembles the real thing, so these tests
    compare the two directly: a renamed attribute or a changed burner snapshot
    key must fail here rather than in production.
    """

    #: Attributes the platform layer reads off the coordinator.
    REQUIRED_ATTRS = (
        "system_id",
        "name",
        "control_mode",
        "zones",
        "zone_ids",
        "zone_decisions",
        "heat_state",
        "heat_decision",
        "compensation",
        "diagnostics_snapshot",
        "state_machine",
        "weather",
        "short_cycle_alert",
        "degraded",
        # V0.2 (docs/INTERFACES_V0.2.md 3.4). Adding them here is the point:
        # renaming one on the coordinator must fail in this suite, not in a
        # silently-unknown entity state on a real installation.
        "combinations",
        "comfort",
        "comfort_analyser",
        "cycling_quality",
        "cycling_quality_reason",
        "unexplained_burns",
        # V0.3 (docs/INTERFACES_V0.3.md 4.3). Same reasoning as above: a rename
        # here must fail in this suite, not become a silent `unknown` on a real
        # installation.
        "probes",
        "delta_t",
        "probe_fault",
        "probes_configured",
    )

    #: Methods the platform layer calls on the coordinator.
    REQUIRED_METHODS = (
        "async_set_control_mode",
        "async_resume_auto",
        "async_set_zone_mode",
        "async_set_supply_temperature",
        "async_reassess",
        "async_set_zone_aux_load",
        "setpoint_source",
        "reason_codes_for",
        "supply_temperature_reading",
        "comfort_for",
    )

    def test_real_coordinator_provides_the_whole_surface(self) -> None:
        """The real class must expose everything the entities touch.

        Some of these are class-level (properties/methods); the rest are set on
        the instance in ``__init__`` and during the evaluation cycle, so the
        check combines class members with the attributes the module assigns to
        ``self``.
        """
        import inspect
        import re

        from custom_components.climatepilot import coordinator as coordinator_mod
        from custom_components.climatepilot.coordinator import (
            ClimatePilotCoordinator,
        )

        class_members = set(dir(ClimatePilotCoordinator))
        # Match both ``self.x = ...`` and annotated ``self.x: T = ...``.
        assigned = set(
            re.findall(
                r"self\.([A-Za-z_]\w*)\s*(?::[^=\n]+)?=",
                inspect.getsource(coordinator_mod),
            )
        )
        available = class_members | assigned

        missing = [
            attr for attr in (*self.REQUIRED_ATTRS, *self.REQUIRED_METHODS) if attr not in available
        ]
        assert not missing, f"coordinator is missing: {sorted(missing)}"

    def test_double_provides_the_whole_surface(self) -> None:
        """The double must expose the same surface, or the tests prove nothing."""
        coordinator = FakeCoordinator(compensation=FakeCompensation())
        missing = [
            attr
            for attr in (*self.REQUIRED_ATTRS, *self.REQUIRED_METHODS)
            if not hasattr(coordinator, attr)
        ]
        assert not missing, f"FakeCoordinator is missing: {sorted(missing)}"

    def test_double_satisfies_the_init_api_used_by_services(self) -> None:
        """``_is_coordinator`` gates service dispatch; the double must pass."""
        from custom_components.climatepilot import _COORDINATOR_API, _is_coordinator

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        assert _is_coordinator(coordinator) is True
        assert all(hasattr(coordinator, attr) for attr in _COORDINATOR_API)

    def test_burner_snapshot_keys_match_diagnostics_engine(self) -> None:
        """Pin the exact snapshot keys the sensor entities consume.

        ``engines`` owns ``BurnerStats.snapshot()``; if a key is renamed the
        sensors would silently fall back to ``None``.
        """
        from custom_components.climatepilot.diagnostics_engine import BurnerStats

        now = datetime.now(UTC)
        engine = BurnerStats()
        engine.note_state("on", now)
        engine.note_state("off", now + timedelta(seconds=120))
        real = engine.snapshot(now + timedelta(seconds=130))

        consumed = (
            "starts_24h",
            "last_duration",
            "short_cycle_alert",
            "short_cycles_24h",
            "short_cycle_seconds",
            "quality",
            "includes_dhw",
            "kind",
            "burning",
            "current_duration",
            "runtime_24h",
            "starts_1h",
            "starts_total",
            "heating_starts_24h",
            "dhw_starts_24h",
            "burns_24h",
        )
        missing = [key for key in consumed if key not in real]
        assert not missing, f"BurnerStats.snapshot lost keys: {sorted(missing)}"

        # The double must carry the same keys, or the sensor tests are fiction.
        double = make_burner_snapshot()
        assert set(consumed) <= set(double)

    def test_snapshot_values_feed_the_sensors(self) -> None:
        """Real BurnerStats output must flow through the entities unchanged."""
        from custom_components.climatepilot.diagnostics_engine import BurnerStats

        now = datetime.now(UTC)
        engine = BurnerStats()
        engine.note_state("on", now)
        engine.note_state("off", now + timedelta(seconds=200))
        snapshot = engine.snapshot(now + timedelta(seconds=210))

        coordinator = FakeCoordinator(compensation=FakeCompensation(), burner=snapshot)
        assert BurnerStarts24hSensor(coordinator).native_value == 1
        assert LastBurnDurationSensor(coordinator).native_value == pytest.approx(200.0, abs=1.0)

    def test_delta_t_report_keys_match_the_real_engine(self) -> None:
        """Pin the ``DeltaTReport`` fields the sensors consume.

        Same reasoning as the burner-snapshot check: ``v3-probe`` owns
        ``DeltaTReport``, and a renamed field would leave the sensors silently
        reporting ``None`` instead of failing.
        """
        from custom_components.climatepilot.delta_t_diagnostics import (
            DeltaTDiagnostics,
        )

        real = DeltaTDiagnostics().report(datetime.now(UTC))
        consumed = (
            "delta_t",
            "average",
            "minimum",
            "maximum",
            "trend_per_hour",
            "stable_seconds",
            "samples",
            "quality",
            "condensing",
            "hydraulic",
            "calibration",
            "reason_codes",
        )
        missing = [name for name in consumed if not hasattr(real, name)]
        assert not missing, f"DeltaTReport lost fields: {sorted(missing)}"

        # The double must carry the same surface, plus the export the entities use.
        double = FakeDeltaTReport()
        assert not [name for name in consumed if not hasattr(double, name)]
        assert set(consumed) <= set(double.as_dict())

        # The "never heat power" marker is a hard contract, not a convention.
        assert real.as_dict()["is_power_measurement"] is False

    def test_real_engine_with_no_probes_is_the_unknown_state(self) -> None:
        """The real engine's no-probe output must match the double's empty state.

        This is the state almost every installation will actually be in, so the
        double may not be more optimistic than reality about it.
        """
        from custom_components.climatepilot.delta_t_diagnostics import (
            DeltaTDiagnostics,
        )

        real = DeltaTDiagnostics().report(datetime.now(UTC))
        double = make_empty_delta_t_report()

        assert real.delta_t is None and double.delta_t is None
        assert real.quality == double.quality == PROBE_UNAVAILABLE
        assert real.condensing == double.condensing == CONDENSING_UNKNOWN
        assert real.hydraulic == double.hydraulic == HYDRAULIC_UNKNOWN
        assert real.calibration == double.calibration == CALIBRATION_INSUFFICIENT_DATA
        assert real.samples == double.samples == 0

        # ... and the entities built on that double show unknown, not 0.
        coordinator = FakeCoordinator(
            compensation=FakeCompensation(),
            probes=make_missing_probe_pair(),
            delta_t=double,
        )
        assert DeltaTSensor(coordinator).native_value is None
        # The probe-less state is a real, translatable quality - not the reserved
        # HA literal, so the entity stays available and readable.
        assert DeltaTQualitySensor(coordinator).native_value == PROBE_UNAVAILABLE

    def test_probe_quality_vocabulary_matches_the_real_constants(self) -> None:
        """The enum options must be exactly the engine's ``PROBE_QUALITIES``."""
        coordinator = FakeCoordinator(compensation=FakeCompensation())
        options = DeltaTQualitySensor(coordinator).options
        assert list(options) == list(PROBE_QUALITIES)
        # Severity order is owned by the engine; the entity must not re-sort it.
        assert options.index(PROBE_VALID) < options.index(PROBE_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Services against the real Home Assistant service registry
# ---------------------------------------------------------------------------


class TestServicesWithRealRegistry:
    """Exercise registration and dispatch through HA's real ServiceRegistry.

    Mocking ``async_register_admin_service`` proves only that we called it. These
    tests use an actual ``HomeAssistant`` object so the schema, the service
    handlers and the removal path are all the real implementations.
    """

    @staticmethod
    def _hass(coordinator: object | None = None):
        import tempfile

        from homeassistant.core import HomeAssistant

        hass = HomeAssistant(tempfile.mkdtemp())
        hass.data[DOMAIN] = {} if coordinator is None else {ENTRY_ID: coordinator}
        return hass

    @pytest.mark.asyncio
    async def test_services_register_dispatch_and_remove(self) -> None:
        from custom_components.climatepilot import (
            _async_register_services,
            _async_remove_services,
        )

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)

        await _async_register_services(hass)
        for service in (
            SERVICE_RESUME_AUTO,
            SERVICE_SET_SUPPLY_TEMPERATURE,
            SERVICE_REASSESS,
        ):
            assert hass.services.has_service(DOMAIN, service)

        # Dispatch through the real registry.
        await hass.services.async_call(DOMAIN, SERVICE_REASSESS, {})
        coordinator.async_reassess.assert_awaited_once_with()

        await hass.services.async_call(
            DOMAIN, SERVICE_SET_SUPPLY_TEMPERATURE, {"temperature": 46.0}
        )
        coordinator.async_set_supply_temperature.assert_awaited_once_with(46.0)

        await hass.services.async_call(DOMAIN, SERVICE_RESUME_AUTO, {CONF_ZONE_ID: "living_room"})
        coordinator.async_resume_auto.assert_awaited_once_with("living_room")

        _async_remove_services(hass)
        for service in (
            SERVICE_RESUME_AUTO,
            SERVICE_SET_SUPPLY_TEMPERATURE,
            SERVICE_REASSESS,
        ):
            assert not hass.services.has_service(DOMAIN, service)

    @pytest.mark.asyncio
    async def test_register_is_idempotent(self) -> None:
        """A second entry must not re-register (HA raises on duplicates)."""
        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)

        await _async_register_services(hass)
        await _async_register_services(hass)  # must not raise
        assert hass.services.has_service(DOMAIN, SERVICE_REASSESS)

    @pytest.mark.asyncio
    async def test_set_supply_temperature_requires_temperature(self) -> None:
        """The documented required field must be enforced by the schema."""
        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)
        await _async_register_services(hass)

        # Missing required field, and an unknown field (extra=FORBID).
        await _assert_schema_rejects(hass, SERVICE_SET_SUPPLY_TEMPERATURE, {})
        await _assert_schema_rejects(
            hass,
            SERVICE_SET_SUPPLY_TEMPERATURE,
            {"temperature": 45.0, "bogus": 1},
        )

    @pytest.mark.asyncio
    async def test_resume_auto_accepts_entity_id_alias(self) -> None:
        """``entity_id`` is accepted as an alias for ``zone_id``."""
        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)
        await _async_register_services(hass)

        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESUME_AUTO,
            {"entity_id": "climate.livingroom_floorheating"},
            blocking=True,
        )
        coordinator.async_resume_auto.assert_awaited_once_with("living_room")

    @pytest.mark.asyncio
    async def test_resume_auto_rejects_unknown_field(self) -> None:
        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)
        await _async_register_services(hass)

        # Asserted through the helper so the check survives Home Assistant's
        # schema-library swap (see ``_assert_schema_rejects``).
        await _assert_schema_rejects(hass, SERVICE_RESUME_AUTO, {"nope": "x"})

    @pytest.mark.asyncio
    async def test_setup_entry_services_work_end_to_end(self) -> None:
        """Full path: setup_entry registers services that dispatch correctly."""
        from custom_components.climatepilot import async_setup_entry

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        coordinator.async_config_entry_first_refresh = AsyncMock()
        coordinator.async_setup_listeners = Mock()
        coordinator.async_shutdown_listeners = Mock()

        hass = self._hass()
        hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        entry = SimpleNamespace(
            entry_id=ENTRY_ID,
            data={},
            options={},
            add_update_listener=Mock(return_value=Mock()),
            async_on_unload=Mock(),
        )

        # Register with the real coordinator class replaced by the double.
        import custom_components.climatepilot as package

        original = package.ClimatePilotCoordinator
        package.ClimatePilotCoordinator = lambda hass_, entry_: coordinator
        try:
            assert await async_setup_entry(hass, entry) is True
            await hass.services.async_call(DOMAIN, SERVICE_REASSESS, {}, blocking=True)
            coordinator.async_reassess.assert_awaited_once_with()
        finally:
            package.ClimatePilotCoordinator = original
