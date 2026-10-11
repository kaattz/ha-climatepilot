"""Tests for the temperature arbiter (design doc section 4).

These pin the rules that matter most in practice:

* a manual change is never undone by the periodic cycle;
* automatic adjustments never apply on top of a manual override;
* the effective value always lands on the device's real step, inside the
  intersection of the configured and reported ranges;
* an override expires exactly as its policy says, and a restart cannot extend
  a fixed-duration override.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.const import (
    OVERRIDE_FIXED_DURATION,
    OVERRIDE_INDEFINITE,
    OVERRIDE_NEXT_SCHEDULE,
    REASON_MANUAL_OVERRIDE,
    REASON_RANGE_CLAMP,
    SOURCE_FALLBACK,
    SOURCE_FIXED_MODE,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
    ZONE_MODE_AWAY,
    ZONE_MODE_COMFORT,
    ZONE_MODE_ECO,
    ZONE_MODE_MANUAL,
)
from custom_components.climatepilot.models import (
    DEFAULT_SLOTS,
    Schedule,
    ScheduleSlot,
    ZoneState,
)
from custom_components.climatepilot.temperature_arbiter import (
    TemperatureArbiter,
    active_slot_key,
)


def make_zone(**kwargs) -> ZoneState:
    zone = ZoneState(zone_id="living_room", entity_id="climate.livingroom")
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.target_step = 0.5
    for key, value in kwargs.items():
        setattr(zone, key, value)
    # The effective P0 window is always derived, never assigned directly.
    zone.refresh_limits()
    return zone


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    """A fixed, timezone-aware instant (2026-10-07 is a Wednesday)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@pytest.fixture()
def arbiter() -> TemperatureArbiter:
    return TemperatureArbiter()


@pytest.fixture()
def schedule() -> Schedule:
    return Schedule(slots=DEFAULT_SLOTS)


# ---------------------------------------------------------------------------
# P3: schedule
# ---------------------------------------------------------------------------


def test_schedule_slot_wins_when_no_override(arbiter, schedule):
    decision = arbiter.arbitrate(make_zone(), at(17, 30), schedule=schedule)
    assert decision.effective == 22.0
    assert decision.source == SOURCE_SCHEDULE


def test_schedule_slot_snaps_to_device_step(arbiter):
    schedule = Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=22.3),))
    decision = arbiter.arbitrate(make_zone(), at(8), schedule=schedule)
    assert decision.effective == 22.5


def test_effective_setpoint_without_schedule_uses_stored_base(arbiter):
    zone = make_zone(base_setpoint=21.0)
    decision = arbiter.arbitrate(zone, at(8), schedule=None)
    assert decision.source == SOURCE_FALLBACK
    assert decision.effective == 21.0


def test_schedule_handles_timezones_across_days(arbiter):
    """A slot before the first slot uses the previous applicable day."""
    schedule = Schedule(
        slots=(
            ScheduleSlot(at=time(6, 0), setpoint=21.0, days=(0,)),
            ScheduleSlot(at=time(22, 0), setpoint=23.0, days=(0,)),
        )
    )
    # Tuesday 02:00 - Monday's 22:00 slot still governs.
    tuesday_early = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
    decision = arbiter.arbitrate(make_zone(), tuesday_early, schedule=schedule)
    assert decision.effective == 23.0


# ---------------------------------------------------------------------------
# P1: manual override outranks everything
# ---------------------------------------------------------------------------


def test_manual_override_beats_schedule(arbiter, schedule):
    zone = make_zone()
    arbiter.note_external_change(zone, 21.5, at(19), policy=OVERRIDE_INDEFINITE)
    decision = arbiter.arbitrate(
        zone, at(20), schedule=schedule, cold_adjustment=1.0, aux_delta=0.5
    )
    assert decision.effective == 21.5
    assert decision.source == SOURCE_MANUAL
    assert REASON_MANUAL_OVERRIDE in decision.reason_codes


def test_automatic_adjustments_never_stack_on_manual_override(arbiter, schedule):
    """Design doc 4.1: while an override is active, nothing else may move it."""
    zone = make_zone()
    arbiter.note_external_change(zone, 21.5, at(19), policy=OVERRIDE_INDEFINITE)
    decision = arbiter.arbitrate(
        zone, at(20), schedule=schedule, cold_adjustment=2.0, aux_delta=2.0
    )
    assert decision.adjustment_delta == 0.0
    assert decision.effective == 21.5


def test_manual_override_still_respects_device_range(arbiter):
    zone = make_zone(config_max_temp=22.0)
    arbiter.note_external_change(zone, 30.0, at(19), policy=OVERRIDE_INDEFINITE)
    decision = arbiter.arbitrate(zone, at(20))
    assert decision.effective == 22.0
    assert REASON_RANGE_CLAMP in decision.reason_codes


def test_manual_override_sets_zone_mode(arbiter):
    zone = make_zone()
    arbiter.note_external_change(zone, 21.0, at(19))
    assert zone.mode == ZONE_MODE_MANUAL
    assert zone.manual_override is not None


# ---------------------------------------------------------------------------
# Override policies
# ---------------------------------------------------------------------------


def test_fixed_duration_override_expires_and_reverts(arbiter, schedule):
    zone = make_zone()
    arbiter.note_external_change(
        zone,
        21.5,
        at(17),
        policy=OVERRIDE_FIXED_DURATION,
        duration=timedelta(hours=2),
    )
    active = arbiter.arbitrate(zone, at(18), schedule=schedule)
    assert active.effective == 21.5

    released = arbiter.expire_overrides([zone], at(19, 1))
    assert released == ["living_room"]
    after = arbiter.arbitrate(zone, at(19, 1), schedule=schedule)
    assert after.source == SOURCE_SCHEDULE
    assert after.effective == 22.0


def test_restart_does_not_extend_a_fixed_duration_override(arbiter):
    """45 minutes into a 2-hour override, a restart must keep the remainder."""
    created = at(18)
    override = arbiter.note_external_change(
        make_zone(),
        21.5,
        created,
        policy=OVERRIDE_FIXED_DURATION,
        duration=timedelta(hours=2),
    )
    assert override.until == created + timedelta(hours=2)

    restarted = created + timedelta(minutes=45)
    remaining = override.remaining(restarted)
    assert remaining == timedelta(hours=1, minutes=15)
    assert override.is_active(restarted)


def test_next_schedule_override_released_at_transition(arbiter, schedule):
    zone = make_zone()
    arbiter.note_external_change(zone, 21.5, at(19), policy=OVERRIDE_NEXT_SCHEDULE)
    # Still active before the next switch point.
    assert arbiter.arbitrate(zone, at(22), schedule=schedule).source == SOURCE_MANUAL
    arbiter.note_schedule_transition(zone, at(23))
    after = arbiter.arbitrate(zone, at(23), schedule=schedule)
    assert after.source == SOURCE_SCHEDULE
    assert after.effective == 20.0
    assert zone.manual_override is None


def test_next_schedule_transition_does_not_release_fixed_duration(arbiter):
    zone = make_zone()
    arbiter.note_external_change(
        zone,
        21.5,
        at(17),
        policy=OVERRIDE_FIXED_DURATION,
        duration=timedelta(hours=6),
    )
    assert arbiter.note_schedule_transition(zone, at(18)) is False
    assert zone.manual_override is not None


def test_indefinite_override_survives_transitions(arbiter):
    zone = make_zone()
    arbiter.note_external_change(zone, 21.5, at(17), policy=OVERRIDE_INDEFINITE)
    assert arbiter.note_schedule_transition(zone, at(23)) is False
    assert arbiter.expire_overrides([zone], at(23, 59)) == []
    assert zone.manual_override is not None


def test_resume_auto_clears_override_immediately(arbiter, schedule):
    zone = make_zone()
    arbiter.note_external_change(zone, 21.5, at(19), policy=OVERRIDE_INDEFINITE)
    arbiter.resume_auto(zone, at(19, 30))
    decision = arbiter.arbitrate(zone, at(19, 30), schedule=schedule)
    assert decision.source == SOURCE_SCHEDULE
    assert zone.mode == "auto"


# ---------------------------------------------------------------------------
# P2: fixed modes
# ---------------------------------------------------------------------------


def test_fixed_mode_outranks_schedule(arbiter, schedule):
    zone = make_zone(base_setpoint=18.0, mode=ZONE_MODE_ECO)
    decision = arbiter.arbitrate(zone, at(17, 30), schedule=schedule)
    assert decision.source == SOURCE_FIXED_MODE
    assert decision.effective == 18.0


def test_away_mode_uses_configured_base_and_is_clamped(arbiter, schedule):
    zone = make_zone(base_setpoint=12.0, mode=ZONE_MODE_AWAY)
    decision = arbiter.arbitrate(zone, at(17, 30), schedule=schedule)
    assert decision.effective == 16.0
    assert decision.clamped is True
    assert REASON_RANGE_CLAMP in decision.reason_codes


def test_comfort_mode_is_a_fixed_mode(arbiter, schedule):
    zone = make_zone(base_setpoint=0.0, mode=ZONE_MODE_COMFORT)
    decision = arbiter.arbitrate(zone, at(3), schedule=schedule)
    assert decision.source == SOURCE_FIXED_MODE
    assert decision.effective == 22.0


# ---------------------------------------------------------------------------
# P0: range and step
# ---------------------------------------------------------------------------


def test_device_range_narrows_the_configured_range():
    zone = make_zone()
    zone.config_min_temp = 10.0
    zone.config_max_temp = 30.0
    zone.device_min_temp = 16.0
    zone.device_max_temp = 26.0
    zone.refresh_limits()
    assert (zone.min_temp, zone.max_temp) == (16.0, 26.0)


def test_partially_overlapping_range_intersects():
    zone = make_zone()
    zone.config_min_temp = 12.0
    zone.config_max_temp = 22.0
    zone.device_min_temp = 16.0
    zone.device_max_temp = 26.0
    zone.refresh_limits()
    assert (zone.min_temp, zone.max_temp) == (16.0, 22.0)


def test_contradictory_config_prefers_configured_window():
    """Config and device ranges disjoint: the user's allowance is the bound.

    Adopting the device window here would be dangerous - a room configured
    16-26 °C on an entity reporting 40-82 °C must not start accepting 40 °C.
    """
    zone = make_zone()
    zone.config_min_temp = 25.0
    zone.config_max_temp = 26.0
    zone.device_min_temp = 40.0
    zone.device_max_temp = 82.0
    zone.refresh_limits()
    assert zone.min_temp == 25.0
    assert zone.max_temp == 26.0


def test_cold_adjustment_is_clamped_by_ceiling(arbiter, schedule):
    zone = make_zone()
    decision = arbiter.arbitrate(zone, at(17, 30), schedule=schedule, cold_adjustment=6.0)
    assert decision.effective == 26.0
    assert REASON_RANGE_CLAMP in decision.reason_codes


def test_one_degree_step_device_gets_integer_targets(arbiter):
    zone = make_zone(target_step=1)
    schedule = Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=22.4),))
    decision = arbiter.arbitrate(zone, at(8), schedule=schedule)
    assert decision.effective == 22.0


def test_reason_codes_are_deduplicated(arbiter):
    zone = make_zone(config_max_temp=17.0)
    schedule = Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=25.0),))
    decision = arbiter.arbitrate(zone, at(8), schedule=schedule)
    assert len(decision.reason_codes) == len(set(decision.reason_codes))


# ---------------------------------------------------------------------------
# Auxiliary load interaction
# ---------------------------------------------------------------------------


def test_aux_load_delta_is_blocked_by_manual_override(arbiter):
    zone = make_zone(aux_load_enabled=True, current_temperature=20.0)
    zone.auto_adjustments["aux_load"] = 0.5
    arbiter.note_external_change(zone, 21.0, at(19), policy=OVERRIDE_INDEFINITE)
    assert arbiter.aux_load_delta(zone, now=at(19, 30), allowed=True) == 0.0


def test_aux_load_delta_respects_zone_cap(arbiter):
    zone = make_zone(aux_load_enabled=True, current_temperature=24.5, aux_load_cap=24.0)
    zone.auto_adjustments["aux_load"] = 0.5
    assert arbiter.aux_load_delta(zone, now=at(19), allowed=True) == 0.0


def test_aux_load_delta_is_zero_when_not_enabled(arbiter):
    zone = make_zone(aux_load_enabled=False, current_temperature=20.0)
    zone.auto_adjustments["aux_load"] = 0.5
    assert arbiter.aux_load_delta(zone, now=at(19), allowed=True) == 0.0


def test_aux_delta_applies_on_top_of_schedule(arbiter, schedule):
    zone = make_zone(aux_load_enabled=True)
    decision = arbiter.arbitrate(zone, at(17, 30), schedule=schedule, aux_delta=0.5)
    assert decision.effective == 22.5
    assert decision.adjustment_delta == 0.5


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------


def test_active_slot_key_changes_at_transition(schedule):
    before = active_slot_key(schedule, at(22, 59))
    after = active_slot_key(schedule, at(23, 0))
    assert before != after


def test_next_transition_is_always_in_the_future(schedule):
    now = at(22, 59)
    nxt = schedule.next_transition(now)
    assert nxt is not None and nxt > now


def test_schedule_round_trips_through_dict():
    original = Schedule(
        slots=(
            ScheduleSlot(at=time(6, 30), setpoint=21.5, days=(0, 1, 2)),
            ScheduleSlot(at=time(22, 0), setpoint=19.0, days=(4, 5)),
        )
    )
    restored = Schedule.from_dict(original.to_dict())
    assert restored.sorted_slots() == original.sorted_slots()


def test_empty_schedule_returns_none_slot(schedule):
    assert Schedule(slots=()).active_slot(at(12)) is None
    assert Schedule(slots=()).next_transition(at(12)) is None
