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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

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
    ShortCycleAlertBinarySensor,
)
from custom_components.climatepilot.button import ReassessButton, ResumeAutoButton
from custom_components.climatepilot.const import (
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
    EffectiveSetpointSensor,
    HeatingStateSensor,
    LastBurnDurationSensor,
    OutdoorTemperatureSensor,
    OverrideRemainingSensor,
    SetpointSourceSensor,
    SuggestedSupplyTempSensor,
    SupplyTemperatureSensor,
    ZoneTemperatureSensor,
)
from custom_components.climatepilot.switch import AuxiliaryLoadSwitch

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "climatepilot"

ENTRY_ID = "entry_abc123"
T0 = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)

ZONES = {
    "living_room": ("climate.livingroom_floorheating", "客厅"),
    "master_bedroom": ("climate.master_bedroom_floorheating", "主卧"),
    "guest_bedroom": ("climate.guest_bedroom_floorheating", "次卧"),
}


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
    }
    snapshot.update(overrides)
    return snapshot


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
    ) -> None:
        self.system_id = ENTRY_ID
        self.name = "智暖领航"
        self.control_mode = control_mode
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
        ],
        "binary_sensor": [
            ShortCycleAlertBinarySensor(coordinator),
            DegradedBinarySensor(coordinator),
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
            ]
        )
        entities["binary_sensor"].extend(
            [
                ManualOverrideBinarySensor(coordinator, zone_id),
                HeatRequestBinarySensor(coordinator, zone_id),
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

    # (system-level, per-zone) as declared by documents/INTERFACES.md 2.9 and
    # consumed from strings.json entity section.
    expected_per_platform = {
        "sensor": (7, 4),
        "binary_sensor": (2, 2),
        "select": (1, 1),
        "button": (1, 1),
        "number": (1, 0),
        "switch": (0, 1),
    }
    assert set(entities) == set(expected_per_platform)

    for platform, (system_level, per_zone) in expected_per_platform.items():
        assert len(entities[platform]) == system_level + zone_count * per_zone, (
            f"{platform}: expected {system_level} + {zone_count}*{per_zone}"
        )

    total = sum(len(group) for group in entities.values())
    assert total == 39  # 12 system-level + 3 zones * 9 per-zone entities
    assert total == (
        sum(system for system, _ in expected_per_platform.values())
        + zone_count * sum(per_zone for _, per_zone in expected_per_platform.values())
    )


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
        "effective_setpoint",
        "setpoint_source",
        "zone_temperature",
        "override_remaining",
    }
    assert keys["binary_sensor"] == {
        "manual_override",
        "short_cycle_alert",
        "degraded",
        "heat_request",
    }
    assert keys["button"] == {"resume_auto", "reassess"}
    assert keys["number"] == {"supply_temperature_override"}
    assert keys["switch"] == {"auxiliary_load"}


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
    """Zone entities are sub-devices of the system device (base.py contract)."""
    coordinator = FakeCoordinator(compensation=FakeCompensation())
    system_info = ActiveZonesSensor(coordinator).device_info
    zone_info = EffectiveSetpointSensor(coordinator, "living_room").device_info

    assert (DOMAIN, ENTRY_ID) in system_info["identifiers"]
    assert (DOMAIN, f"{ENTRY_ID}_living_room") in zone_info["identifiers"]
    assert zone_info["via_device"] == (DOMAIN, ENTRY_ID)
    assert zone_info["name"] == "客厅"


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
        (sensor_platform, 7 + 4 * 3),
        (bs_platform, 2 + 2 * 3),
        (select_platform, 1 + 3),
        (button_platform, 1 + 3),
        (number_platform, 1),
        (switch_platform, 3),
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
    assert len(added) == 7


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
        import voluptuous as vol

        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)
        await _async_register_services(hass)

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, SERVICE_SET_SUPPLY_TEMPERATURE, {}, blocking=True
            )
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_SUPPLY_TEMPERATURE,
                {"temperature": 45.0, "bogus": 1},
                blocking=True,
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
        import voluptuous as vol

        from custom_components.climatepilot import _async_register_services

        coordinator = FakeCoordinator(compensation=FakeCompensation())
        hass = self._hass(coordinator)
        await _async_register_services(hass)

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, SERVICE_RESUME_AUTO, {"nope": "x"}, blocking=True
            )

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
