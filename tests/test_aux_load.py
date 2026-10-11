"""Tests for the bounded auxiliary heat load (design doc 5.3, 13.8).

The blueprint this replaces raised the living-room target to ``current room
temperature + 2 °C`` whenever a bedroom called for heat. That target climbs as
the room warms, so the room can never satisfy it - the "追涨" failure the design
document calls out by name. These tests pin the replacement behaviour:

* the boost is relative to the room's own *schedule* target, never to the
  measured room temperature;
* a hard ceiling, a night block and a manual override all end it;
* when other demand ends it returns to the normal target;
* it cannot engage more often than the configured minimum interval.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.aux_load import (
    AuxLoadController,
)
from custom_components.climatepilot.const import (
    REASON_AUX_LOAD_EXIT,
    REASON_MANUAL_OVERRIDE,
    REASON_THROTTLED,
    SOURCE_SCHEDULE,
)
from custom_components.climatepilot.models import (
    AuxLoadConfig,
    AuxLoadState,
    Schedule,
    ScheduleSlot,
    ZoneState,
)
from custom_components.climatepilot.temperature_arbiter import (
    TemperatureArbiter,
)

T0 = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)


def make_zone(**kwargs) -> ZoneState:
    zone = ZoneState(zone_id="living_room", entity_id="climate.livingroom")
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.aux_load_enabled = True
    zone.target_step = 0.5
    for key, value in kwargs.items():
        setattr(zone, key, value)
    zone.refresh_limits()
    return zone


def enabled_config(**kwargs) -> AuxLoadConfig:
    base = {
        "enabled": True,
        "delta": 0.5,
        "max_temperature": 24.0,
        "hysteresis": 0.3,
        "min_switch_interval": 1800.0,
        "night_block": False,
    }
    base.update(kwargs)
    return AuxLoadConfig(**base)


@pytest.fixture()
def controller() -> AuxLoadController:
    return AuxLoadController(enabled_config())


@pytest.fixture()
def state() -> AuxLoadState:
    return AuxLoadState()


# ---------------------------------------------------------------------------
# The core anti-pattern: no chase of the measured room temperature
# ---------------------------------------------------------------------------


def test_boost_is_relative_to_schedule_target_not_room_temperature(controller, state):
    """The old ``current_temperature + 2`` chase must be gone for good."""
    zone = make_zone(current_temperature=23.5)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is True
    assert decision.target == 22.5  # 22.0 + delta, NOT 23.5 + 2
    assert decision.delta == 0.5


def test_boost_does_not_grow_as_the_room_warms(controller, state):
    """Repeated evaluation with a rising room temperature yields one target."""
    targets = []
    for temperature in (20.0, 21.0, 22.0, 23.0, 23.9):
        zone = make_zone(current_temperature=temperature)
        decision = controller.evaluate(
            state, zone, T0, other_demand=1, base_target=22.0, in_use=True
        )
        targets.append(decision.target)
    assert set(targets) == {22.5}, "the target must not follow the room upwards"


def test_boost_is_never_above_the_absolute_ceiling(controller, state):
    zone = make_zone(current_temperature=20.0)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=24.0, in_use=True)
    assert decision.target <= 24.0


def test_boost_is_bounded_by_the_zones_own_maximum():
    controller = AuxLoadController(enabled_config(max_temperature=30.0))
    zone = make_zone(current_temperature=20.0, config_max_temp=23.0)
    decision = controller.evaluate(
        AuxLoadState(), zone, T0, other_demand=1, base_target=22.0, in_use=True
    )
    assert decision.target <= 23.0


# ---------------------------------------------------------------------------
# Exit conditions
# ---------------------------------------------------------------------------


def test_no_boost_when_no_other_zone_demands_heat(controller, state):
    zone = make_zone(current_temperature=20.0)
    decision = controller.evaluate(state, zone, T0, other_demand=0, base_target=22.0, in_use=True)
    assert decision.active is False
    assert decision.delta == 0.0
    assert REASON_AUX_LOAD_EXIT in decision.reason_codes


def test_active_boost_ends_when_demand_ends(controller, state):
    zone = make_zone(current_temperature=20.0)
    first = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert first.active is True

    later = T0 + timedelta(minutes=40)
    ended = controller.evaluate(state, zone, later, other_demand=0, base_target=22.0, in_use=True)
    assert ended.active is False
    assert ended.delta == 0.0
    assert ended.target == 22.0, "the room must return to its own target"


def test_manual_override_blocks_the_boost(controller, state):
    zone = make_zone(current_temperature=20.0)
    arbiter = TemperatureArbiter()
    arbiter.note_external_change(zone, 21.0, T0, policy="indefinite")

    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is False
    assert REASON_MANUAL_OVERRIDE in decision.reason_codes


def test_night_block_prevents_engagement():
    controller = AuxLoadController(
        enabled_config(night_block=True, night_start=time(23, 0), night_end=time(6, 0))
    )
    zone = make_zone(current_temperature=20.0)
    night = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
    decision = controller.evaluate(
        AuxLoadState(), zone, night, other_demand=1, base_target=22.0, in_use=True
    )
    assert decision.active is False
    assert REASON_AUX_LOAD_EXIT in decision.reason_codes


def test_room_at_ceiling_does_not_engage(controller, state):
    zone = make_zone(current_temperature=24.0)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is False


def test_hysteresis_keeps_an_active_boost_until_the_ceiling(controller, state):
    zone = make_zone(current_temperature=23.5)
    controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)

    later = T0 + timedelta(minutes=40)
    still_on = controller.evaluate(
        state, zone, later, other_demand=1, base_target=22.0, in_use=True
    )
    assert still_on.active is True, "an active boost holds below the ceiling"

    at_ceiling = make_zone(current_temperature=24.0)
    later2 = later + timedelta(minutes=40)
    ended = controller.evaluate(
        state, at_ceiling, later2, other_demand=1, base_target=22.0, in_use=True
    )
    assert ended.active is False


def test_zone_not_marked_as_aux_load_never_engages(controller, state):
    zone = make_zone(current_temperature=20.0, aux_load_enabled=False)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is False


def test_disabled_aux_load_never_engages(state):
    controller = AuxLoadController(enabled_config(enabled=False))
    zone = make_zone(current_temperature=20.0)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is False


def test_only_one_zone_holds_the_auxiliary_slot(controller, state):
    zone = make_zone(current_temperature=20.0)
    not_holder = controller.evaluate(
        state, zone, T0, other_demand=1, base_target=22.0, in_use=False
    )
    assert not_holder.active is False


def test_no_headroom_means_no_boost(controller, state):
    """If the normal target already sits at the ceiling, boosting is churn."""
    zone = make_zone(current_temperature=20.0)
    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=24.0, in_use=True)
    assert decision.active is False


# ---------------------------------------------------------------------------
# Minimum interval
# ---------------------------------------------------------------------------


def test_switch_interval_prevents_rapid_toggling(controller, state):
    zone = make_zone(current_temperature=20.0)
    controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)

    # Demand ends immediately; the switch interval has not elapsed.
    soon = T0 + timedelta(seconds=60)
    held = controller.evaluate(state, zone, soon, other_demand=0, base_target=22.0, in_use=True)
    assert held.active is True
    assert REASON_THROTTLED in held.reason_codes

    # Once the interval elapses it is allowed to shed.
    later = T0 + timedelta(seconds=controller.config.min_switch_interval + 1)
    shed = controller.evaluate(state, zone, later, other_demand=0, base_target=22.0, in_use=True)
    assert shed.active is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_restore_drops_a_stale_overnight_activation(controller):
    """A previous day's activation must not silently resume after a restart."""
    stale = AuxLoadState(
        active=True,
        zone_switch_at={"living_room": T0 - timedelta(hours=12)},
    )
    state = AuxLoadState()
    controller.restore(state, stale.to_dict(), T0)
    assert state.active is False
    assert state.zone_switch_at == {}


def test_restore_keeps_a_recent_activation(controller):
    recent = AuxLoadState(
        active=True,
        zone_switch_at={"living_room": T0 - timedelta(minutes=30)},
    )
    state = AuxLoadState()
    controller.restore(state, recent.to_dict(), T0)
    assert state.active is True
    assert "living_room" in state.zone_switch_at


def test_state_round_trips_through_dict():
    original = AuxLoadState(
        active=True,
        last_switch=T0,
        zone_switch_at={"living_room": T0},
    )
    restored = AuxLoadState.from_dict(original.to_dict())
    assert restored.active is True
    assert restored.zone_switch_at == original.zone_switch_at


def test_config_round_trips_through_dict():
    original = enabled_config(delta=0.8, max_temperature=23.5)
    restored = AuxLoadConfig.from_dict(original.to_dict())
    assert restored.delta == 0.8
    assert restored.max_temperature == 23.5
    assert restored.min_switch_interval == original.min_switch_interval


# ---------------------------------------------------------------------------
# Integration with the arbiter
# ---------------------------------------------------------------------------


def test_aux_delta_flows_through_arbitration(controller, state):
    """End to end: schedule 22.0 + bounded 0.5 -> 22.5 to the device.

    This is the whole point of the auxiliary load: the room's *scheduled* target
    plus a small bounded delta, produced by the same arbitration path every
    other setpoint goes through.
    """
    arbiter = TemperatureArbiter()
    zone = make_zone(current_temperature=20.0)
    schedule = Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=22.0),))

    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    assert decision.active is True

    result = arbiter.arbitrate(zone, T0, schedule=schedule, aux_delta=decision.delta)
    assert result.effective == 22.5
    assert result.adjustment_delta == 0.5
    assert result.source == SOURCE_SCHEDULE


def test_aux_delta_is_zero_for_a_manual_override_zone(controller, state):
    arbiter = TemperatureArbiter()
    zone = make_zone(current_temperature=20.0)
    schedule = Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=22.0),))
    arbiter.note_external_change(zone, 21.0, T0, policy="indefinite")

    decision = controller.evaluate(state, zone, T0, other_demand=1, base_target=22.0, in_use=True)
    result = arbiter.arbitrate(zone, T0, schedule=schedule, aux_delta=decision.delta)
    assert result.effective == 21.0
    assert result.adjustment_delta == 0.0
