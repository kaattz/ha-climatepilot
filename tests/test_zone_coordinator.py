"""Tests for the zone write coordinator (design doc 4.4, 4.7).

The scenarios here are lifted straight from the design document's mandatory
test list (section 13):

* 13.2 - our own write echoes back and must not be read as a manual change;
* 13.3 - a late echo of an old command must not undo a newer user change;
* 13.4 - a burst of 22 / 23 / 22.5 °C arrives in order;
* 13.5 - a 10-minute outage must not produce a command storm on recovery;
* 13.9 - an unknown zone state is not treated as "no demand".
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.climate_adapter import ClimateSnapshot
from custom_components.climatepilot.const import (
    FEEDBACK_STALE,
    FEEDBACK_UNAVAILABLE,
    REASON_ENTITY_UNAVAILABLE,
    REASON_THROTTLED,
    REASON_TOLERANCE,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
)
from custom_components.climatepilot.models import SetpointDecision, ZoneState
from custom_components.climatepilot.temperature_arbiter import (
    TemperatureArbiter,
)
from custom_components.climatepilot.zone_coordinator import (
    SYNC_ECHO,
    SYNC_EXTERNAL,
    SYNC_UNCERTAIN,
    SYNC_UNCHANGED,
    ZoneCoordinator,
)

T0 = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)


def make_zone(**kwargs) -> ZoneState:
    zone = ZoneState(zone_id="living_room", entity_id="climate.livingroom")
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.target_step = 0.5
    zone.tolerance = 0.2
    for key, value in kwargs.items():
        setattr(zone, key, value)
    zone.refresh_limits()
    return zone


def snapshot(
    temperature: float | None,
    *,
    state: str = "heat",
    hvac_action: str | None = "heating",
    current: float | None = 21.0,
    exists: bool = True,
) -> ClimateSnapshot:
    return ClimateSnapshot(
        entity_id="climate.livingroom",
        exists=exists,
        state=state,
        hvac_action=hvac_action,
        temperature=temperature,
        current_temperature=current,
        min_temp=16.0,
        max_temp=26.0,
        target_step=0.5,
        hvac_modes=("heat", "off"),
        last_updated=T0,
        last_changed=T0,
        supports_action=hvac_action is not None,
    )


def decision(zone: ZoneState, value: float, source: str = SOURCE_SCHEDULE):
    return SetpointDecision(
        zone_id=zone.zone_id,
        effective=value,
        base=value,
        source=source,
    )


@pytest.fixture()
def coordinator() -> ZoneCoordinator:
    return ZoneCoordinator(TemperatureArbiter())


# ---------------------------------------------------------------------------
# 13.2 - our own echo
# ---------------------------------------------------------------------------


def test_own_write_echo_is_not_a_manual_change(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 23.0), T0, enabled=True)
    assert plan.should_write is True
    coordinator.note_write_issued(zone, plan, T0, "req-1")

    outcome = coordinator.sync_from_device(zone, snapshot(23.0), T0 + timedelta(seconds=5))
    assert outcome.kind == SYNC_ECHO
    assert zone.manual_override is None
    assert zone.last_confirmed_write is not None
    assert zone.last_confirmed_write.target == 23.0


def test_echo_clears_pending_and_failure_state(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_failed(zone, T0)
    assert coordinator.failure_count("living_room") == 1

    coordinator.sync_from_device(zone, snapshot(22.0), T0 + timedelta(seconds=10))
    assert coordinator.failure_count("living_room") == 0


def test_identical_value_without_pending_is_unchanged(coordinator):
    """A repeated readback that matches the last value is not a new event."""
    zone = make_zone(actual_setpoint=22.0)
    outcome = coordinator.sync_from_device(zone, snapshot(22.0), T0)
    assert outcome.kind == SYNC_UNCHANGED
    assert zone.manual_override is None


def test_matching_value_after_window_expires_is_external(coordinator):
    """4.4.2 / 4.4.6: a matching value cannot be claimed as our echo forever.

    Past the confirmation window we cannot distinguish our own echo from a
    person entering the same number, so the design doc's rule applies: protect
    the possible manual operation.
    """
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 23.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_succeeded(zone)

    late = T0 + timedelta(seconds=coordinator.pending_timeout + 30)
    outcome = coordinator.sync_from_device(zone, snapshot(23.0), late)
    assert outcome.kind == SYNC_EXTERNAL
    assert zone.manual_override is not None
    assert zone.manual_override.value == 23.0


def test_failed_write_echo_still_counts_as_ours(coordinator):
    """A failed service call may still have reached the device.

    Recognising its echo is what stops the integration from freezing a room on
    a value it picked itself.
    """
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.5), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_failed(zone, T0)

    # Late - well past the confirmation window, but inside the failure grace.
    late = T0 + timedelta(seconds=coordinator.pending_timeout + 60)
    outcome = coordinator.sync_from_device(zone, snapshot(22.5), late)
    assert outcome.kind == SYNC_ECHO
    assert zone.manual_override is None


def test_failed_write_echo_stops_being_ours_eventually(coordinator):
    """The failure grace is bounded, so an unrelated later entry stays manual."""
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.5), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_failed(zone, T0)

    way_later = T0 + timedelta(seconds=coordinator.pending_timeout + coordinator.echo_grace + 60)
    outcome = coordinator.sync_from_device(zone, snapshot(22.5), way_later)
    assert outcome.kind == SYNC_EXTERNAL
    assert zone.manual_override is not None


def test_failed_write_does_not_block_retry(coordinator):
    """A failed command must be retried, not left blocking the zone."""
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_failed(zone, T0)

    later = T0 + timedelta(seconds=coordinator.min_write_interval + 1)
    retry = coordinator.plan_write(zone, decision(zone, 22.0), later, enabled=True)
    assert retry.should_write is True


def test_late_echo_of_superseded_command_is_flagged(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 23.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    coordinator.note_write_succeeded(zone)

    user_time = T0 + timedelta(seconds=20)
    coordinator.sync_from_device(zone, snapshot(19.5), user_time)

    from custom_components.climatepilot.const import REASON_LATE_ECHO

    late = coordinator.sync_from_device(zone, snapshot(23.0), user_time + timedelta(seconds=3))
    assert late.kind == SYNC_UNCHANGED
    assert REASON_LATE_ECHO in late.reason_codes


# ---------------------------------------------------------------------------
# 13.3 - late echo must not undo a newer user change
# ---------------------------------------------------------------------------


def test_user_change_after_our_write_wins(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 23.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")

    # The user walks to the panel and sets 19.5 before the echo lands.
    user_time = T0 + timedelta(seconds=20)
    outcome = coordinator.sync_from_device(zone, snapshot(19.5), user_time)
    assert outcome.kind == SYNC_EXTERNAL
    assert zone.manual_override is not None
    assert zone.manual_override.value == 19.5

    # Our stale 23.0 echo arrives afterwards and must be discarded.
    late = coordinator.sync_from_device(zone, snapshot(23.0), user_time + timedelta(seconds=3))
    assert late.kind == SYNC_UNCHANGED
    assert zone.manual_override.value == 19.5

    # And the user's 19.5 must still be what arbitration sends.
    result = coordinator.arbiter.arbitrate(zone, user_time + timedelta(seconds=10))
    assert result.source == SOURCE_MANUAL
    assert result.effective == 19.5


# ---------------------------------------------------------------------------
# 13.4 - burst of changes
# ---------------------------------------------------------------------------


def test_burst_of_external_changes_is_processed_in_order(coordinator):
    zone = make_zone(actual_setpoint=21.0)
    for offset, value in enumerate((22.0, 23.0, 22.5)):
        moment = T0 + timedelta(seconds=offset * 2)
        outcome = coordinator.sync_from_device(zone, snapshot(value), moment)
        assert outcome.kind == SYNC_EXTERNAL
        assert zone.manual_override.value == value
    assert zone.actual_setpoint == 22.5


def test_arbitration_honours_the_latest_external_value(coordinator):
    zone = make_zone(actual_setpoint=21.0)
    coordinator.sync_from_device(zone, snapshot(23.0), T0)
    latest = T0 + timedelta(seconds=30)
    coordinator.sync_from_device(zone, snapshot(22.5), latest)

    result = coordinator.arbiter.arbitrate(zone, latest)
    assert result.source == SOURCE_MANUAL
    assert result.effective == 22.5


# ---------------------------------------------------------------------------
# 13.5 - outage recovery
# ---------------------------------------------------------------------------


def test_unavailable_entity_is_uncertain_not_zero_demand(coordinator):
    zone = make_zone(actual_setpoint=22.0)
    outcome = coordinator.sync_from_device(zone, snapshot(None, state="unavailable"), T0)
    assert outcome.kind == SYNC_UNCERTAIN
    assert zone.feedback_quality == FEEDBACK_UNAVAILABLE
    assert REASON_ENTITY_UNAVAILABLE in outcome.reason_codes


def test_unknown_state_is_treated_as_unavailable(coordinator):
    zone = make_zone()
    outcome = coordinator.sync_from_device(zone, snapshot(None, state="unknown"), T0)
    assert outcome.kind == SYNC_UNCERTAIN
    assert zone.feedback_quality == FEEDBACK_UNAVAILABLE


def test_missing_entity_never_writes(coordinator):
    zone = make_zone()
    coordinator.sync_from_device(zone, snapshot(None, exists=False), T0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    assert plan.should_write is False
    assert REASON_ENTITY_UNAVAILABLE in plan.reason_codes


def test_recording_failure_does_not_raise(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")
    retry_at = coordinator.note_write_failed(zone, T0)
    assert retry_at is not None
    assert zone.pending_command is None


def test_retry_backoff_is_bounded(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    delays = []
    for attempt in range(coordinator.retry_max + 2):
        moment = T0 + timedelta(seconds=attempt * 100)
        retry_at = coordinator.note_write_failed(zone, moment)
        delays.append(retry_at)

    assert delays[-1] is None, "retry budget must eventually be exhausted"
    assert all(d is None or d > T0 for d in delays)
    # Backoff grows: each delay is larger than the previous one.
    concrete = [d for d in delays if d is not None]
    assert len(concrete) == coordinator.retry_max
    assert concrete == sorted(concrete)


def test_backoff_blocks_writes_until_it_elapses(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    coordinator.note_write_failed(zone, T0)
    blocked = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    assert blocked.should_write is False
    assert blocked.retry_at is not None

    later = blocked.retry_at + timedelta(seconds=1)
    allowed = coordinator.plan_write(zone, decision(zone, 22.0), later, enabled=True)
    assert allowed.should_write is True


def test_no_command_storm_during_outage(coordinator):
    """10 minutes of unavailability must not queue any retries."""
    zone = make_zone(actual_setpoint=20.0)
    target = decision(zone, 23.0)
    writes = 0
    for step in range(0, 600, 30):
        moment = T0 + timedelta(seconds=step)
        coordinator.sync_from_device(zone, snapshot(None, state="unavailable"), moment)
        plan = coordinator.plan_write(zone, target, moment, enabled=True)
        if plan.should_write:
            writes += 1
            coordinator.note_write_issued(zone, plan, moment, f"req-{step}")
    assert writes == 0


def test_outage_marks_pending_as_stale(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 23.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")

    expired = T0 + timedelta(seconds=coordinator.pending_timeout + 1)
    stale = coordinator.expire_pending([zone], expired)
    assert stale == ["living_room"]
    assert zone.feedback_quality == FEEDBACK_STALE
    assert zone.pending_command is None


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def test_write_is_throttled_within_minimum_interval(coordinator):
    zone = make_zone(actual_setpoint=20.0, last_write_at=T0)
    plan = coordinator.plan_write(
        zone, decision(zone, 22.0), T0 + timedelta(seconds=5), enabled=True
    )
    assert plan.should_write is False
    assert REASON_THROTTLED in plan.reason_codes


def test_write_allowed_after_minimum_interval(coordinator):
    zone = make_zone(actual_setpoint=20.0, last_write_at=T0)
    later = T0 + timedelta(seconds=coordinator.min_write_interval + 1)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), later, enabled=True)
    assert plan.should_write is True


def test_within_tolerance_is_not_rewritten(coordinator):
    zone = make_zone(actual_setpoint=22.1)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    assert plan.should_write is False
    assert REASON_TOLERANCE in plan.reason_codes


def test_pending_write_suppresses_duplicate_commands(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=True)
    coordinator.note_write_issued(zone, plan, T0, "req-1")

    again = coordinator.plan_write(
        zone, decision(zone, 22.0), T0 + timedelta(seconds=5), enabled=True
    )
    assert again.should_write is False


def test_disabled_system_never_writes(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    plan = coordinator.plan_write(zone, decision(zone, 22.0), T0, enabled=False)
    assert plan.should_write is False


def test_null_setpoint_from_device_is_not_a_change(coordinator):
    """The boiler reports ``temperature: null``; that is not a user action."""
    zone = make_zone(actual_setpoint=22.0)
    outcome = coordinator.sync_from_device(zone, snapshot(None), T0)
    assert outcome.kind == SYNC_UNCHANGED
    assert zone.manual_override is None


def test_state_round_trips_through_snapshot(coordinator):
    zone = make_zone(actual_setpoint=20.0)
    coordinator.note_write_failed(zone, T0)
    saved = coordinator.snapshot()

    restored = ZoneCoordinator(TemperatureArbiter())
    restored.restore(saved)
    assert restored.snapshot() == saved
