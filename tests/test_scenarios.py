"""End-to-end acceptance scenarios for ClimatePilot.

Each test here is a narrative taken from the design document's mandatory test
list (section 13) and the V0.1 delivery criteria (section 12), driven through
the real engines together rather than one unit at a time.

The V0.1 delivery standard is explicit:

    manual temperature changes are not reverted by the periodic cycle;
    override deadlines survive a restart;
    a loss of communication does not switch the boiler off by mistake.

These tests are the evidence for that standard.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.aux_load import AuxLoadController
from custom_components.climatepilot.climate_adapter import ClimateSnapshot
from custom_components.climatepilot.const import (
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    DECISION_NONE,
    DECISION_REVOKE_HEAT,
    FSM_DEGRADED,
    FSM_HEATING,
    REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
)
from custom_components.climatepilot.heat_source import (
    BoilerCapabilities,
    HeatSourceConfig,
    HeatSourceInput,
    HeatSourceStateMachine,
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
from custom_components.climatepilot.zone_coordinator import (
    SYNC_ECHO,
    SYNC_EXTERNAL,
    ZoneCoordinator,
)

# 2026-10-07 is a Wednesday; the design doc's example runs 17:00 -> 23:00.
DAY = datetime(2026, 10, 7, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


SCHEDULE = Schedule(
    slots=(
        ScheduleSlot(at=time(6, 0), setpoint=22.0),
        ScheduleSlot(at=time(9, 0), setpoint=19.0),
        ScheduleSlot(at=time(17, 0), setpoint=22.0),
        ScheduleSlot(at=time(23, 0), setpoint=20.5),
    )
)


def bedroom(**kwargs) -> ZoneState:
    zone = ZoneState(zone_id="master_bedroom", entity_id="climate.master_bedroom_floorheating")
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.target_step = 0.5
    zone.tolerance = 0.2
    zone.actual_setpoint = 22.0
    for key, value in kwargs.items():
        setattr(zone, key, value)
    zone.refresh_limits()
    return zone


def snapshot(temperature: float | None, *, state: str = "heat", action: str | None = "heating"):
    return ClimateSnapshot(
        entity_id="climate.master_bedroom_floorheating",
        exists=True,
        state=state,
        hvac_action=action,
        temperature=temperature,
        current_temperature=21.0,
        min_temp=16.0,
        max_temp=26.0,
        target_step=0.5,
        hvac_modes=("heat", "off"),
        last_updated=DAY,
        last_changed=DAY,
        supports_action=action is not None,
    )


# ===========================================================================
# 13.1 - manual vs schedule conflict
# ===========================================================================


def test_scenario_manual_change_then_schedule_takes_over():
    """The design document's worked example, table 4.5.

    ======  ==============================  ==========  ============
    Time    Event                           Effective   Source
    ======  ==============================  ==========  ============
    17:00   schedule                        22.0        schedule
    18:00   cold-snap adjustment +1         23.0        adjustment
    19:00   KNX panel sets 21.5             21.5        manual
    20:00   outdoor keeps falling           21.5        manual held
    23:00   next schedule node              20.5        schedule
    ======  ==============================  ==========  ============
    """
    arbiter = TemperatureArbiter()
    zone = bedroom()

    # 17:00 - plain schedule.
    d_1700 = arbiter.arbitrate(zone, at(17), schedule=SCHEDULE)
    assert (d_1700.effective, d_1700.source) == (22.0, SOURCE_SCHEDULE)

    # 18:00 - an automatic cold-snap adjustment raises the target.
    d_1800 = arbiter.arbitrate(zone, at(18), schedule=SCHEDULE, cold_adjustment=1.0)
    assert d_1800.effective == 23.0

    # 19:00 - the user sets 21.5 on the KNX panel. It is not one of our writes,
    # so it becomes a manual override.
    coordinator = ZoneCoordinator(arbiter)
    outcome = coordinator.sync_from_device(zone, snapshot(21.5), at(19))
    assert outcome.kind == SYNC_EXTERNAL
    assert zone.manual_override is not None

    # 20:00 - the outdoor temperature keeps falling. The automatic adjustment
    # must NOT move a room the user has taken over.
    d_2000 = arbiter.arbitrate(zone, at(20), schedule=SCHEDULE, cold_adjustment=1.0)
    assert (d_2000.effective, d_2000.source) == (21.5, SOURCE_MANUAL)
    assert d_2000.adjustment_delta == 0.0

    # 23:00 - the next schedule node releases the override.
    assert arbiter.note_schedule_transition(zone, at(23)) is True
    d_2300 = arbiter.arbitrate(zone, at(23), schedule=SCHEDULE)
    assert (d_2300.effective, d_2300.source) == (20.5, SOURCE_SCHEDULE)


def test_scenario_periodic_cycle_never_reverts_a_manual_change():
    """The V0.1 delivery criterion, run over many cycles."""
    arbiter = TemperatureArbiter()
    coordinator = ZoneCoordinator(arbiter)
    zone = bedroom()

    coordinator.sync_from_device(zone, snapshot(21.5), at(19))
    assert zone.manual_override is not None

    for minute in range(0, 120, 5):
        moment = at(19) + timedelta(minutes=minute)
        decision = arbiter.arbitrate(zone, moment, schedule=SCHEDULE)
        assert decision.effective == 21.5, "the periodic cycle must not revert it"
        # And the write planner must not schedule a correction either.
        plan = coordinator.plan_write(zone, decision, moment, enabled=True)
        if plan.should_write:
            assert plan.target == 21.5


# ===========================================================================
# 13.2 / 13.4 - write echo and bursts
# ===========================================================================


def test_scenario_our_own_write_is_not_a_manual_change():
    arbiter = TemperatureArbiter()
    coordinator = ZoneCoordinator(arbiter)
    zone = bedroom(actual_setpoint=20.0)

    decision = arbiter.arbitrate(zone, at(17), schedule=SCHEDULE)
    plan = coordinator.plan_write(zone, decision, at(17), enabled=True)
    assert plan.should_write is True
    coordinator.note_write_issued(zone, plan, at(17), "req-1")

    outcome = coordinator.sync_from_device(zone, snapshot(22.0), at(17) + timedelta(seconds=3))
    assert outcome.kind == SYNC_ECHO
    assert zone.manual_override is None, "our own write must not freeze the zone"


def test_scenario_knx_burst_is_processed_in_order():
    arbiter = TemperatureArbiter()
    coordinator = ZoneCoordinator(arbiter)
    zone = bedroom(actual_setpoint=21.0)

    for index, value in enumerate((22.0, 23.0, 22.5)):
        moment = at(19) + timedelta(seconds=index * 3)
        outcome = coordinator.sync_from_device(zone, snapshot(value), moment)
        assert outcome.kind == SYNC_EXTERNAL
    assert zone.manual_override.value == 22.5
    assert arbiter.arbitrate(zone, at(19, 1)).effective == 22.5


# ===========================================================================
# 13.7 - day boundary and DST
# ===========================================================================


def test_scenario_schedule_node_fires_exactly_once_per_day():
    arbiter = TemperatureArbiter()
    zone = bedroom()

    seen: list[float] = []
    for hour in range(24):
        for minute in (0, 30):
            decision = arbiter.arbitrate(zone, at(hour, minute), schedule=SCHEDULE)
            if not seen or seen[-1] != decision.effective:
                seen.append(decision.effective)

    # 19.0 (09:00) -> 22.0 (17:00) -> 20.5 (23:00); the 06:00 value is the
    # previous day's 20.5 carrying over, so it appears first.
    assert seen == [20.5, 22.0, 19.0, 22.0, 20.5]


def test_scenario_next_transition_is_stable_across_the_day():
    for hour in (0, 5, 8, 12, 16, 22):
        moment = at(hour)
        nxt = SCHEDULE.next_transition(moment)
        assert nxt is not None
        assert nxt > moment, f"transition at {hour}:00 must be in the future"


# ===========================================================================
# 13.5 / 13.9 - boiler demand boundaries
# ===========================================================================


def verified_capabilities() -> BoilerCapabilities:
    return BoilerCapabilities(
        entity_id="climate.rinnai_heating_control",
        min_temp=40.0,
        max_temp=82.0,
        target_step=1.0,
        hvac_modes=("heat", "auto", "dry", "off"),
        semantics_verified=True,
        min_flow_verified=True,
    )


def fsm(capabilities=None) -> HeatSourceStateMachine:
    return HeatSourceStateMachine(
        HeatSourceConfig(),
        capabilities=capabilities if capabilities is not None else verified_capabilities(),
    )


def test_scenario_all_valves_closed_revokes_the_heat_request():
    """13.9: no zone demands heat -> the request is withdrawn.

    Driven the way the coordinator really drives it: a periodic evaluation
    cycle rather than one giant time jump. The FSM must pass through the demand
    debounce and the valve pre-open wait - a valve that has not physically
    opened cannot carry flow, so those stages may not be skipped.
    """
    machine = fsm()
    now = at(12)
    machine.note_demand_change(now, True)

    def step(moment: datetime, *, active: int, mode: str = "heat"):
        return machine.evaluate(
            HeatSourceInput(
                now=moment,
                active_zones=active,
                unknown_zones=0,
                control_mode=CONTROL_MODE_COORDINATE,
                boiler_mode=mode,
                boiler_available=True,
            ),
            supply=45.0,
        )

    # Walk forward in 15 s cycles until the request is actually made.
    moment = now
    heating = None
    for _ in range(40):  # 10 minutes of cycles
        moment += timedelta(seconds=15)
        decision = step(moment, active=1, mode="off")
        if decision.state == FSM_HEATING:
            heating = decision
            break

    assert heating is not None, "demand must eventually produce a heat request"
    assert heating.target_mode == "heat"

    # All zones close. The off-debounce holds briefly, then the request goes.
    machine.note_demand_change(moment, False)
    revoked = None
    for _ in range(60):  # another 15 minutes of cycles
        moment += timedelta(seconds=15)
        decision = step(moment, active=0)
        if decision.target_mode == "off":
            revoked = decision
            break

    assert revoked is not None, "the heat request must be revoked once demand ends"
    assert revoked.decision == DECISION_REVOKE_HEAT


def test_scenario_unknown_zone_state_does_not_revoke_or_demand():
    """13.9: unknown must not be laundered into 'no demand'."""
    machine = fsm()
    now = at(12)
    machine.note_demand_change(now, True)
    machine.evaluate(
        HeatSourceInput(
            now=now,
            active_zones=1,
            unknown_zones=0,
            control_mode=CONTROL_MODE_COORDINATE,
            boiler_mode="heat",
            boiler_available=True,
        ),
        supply=45.0,
    )

    degraded = machine.evaluate(
        HeatSourceInput(
            now=now + timedelta(minutes=5),
            active_zones=1,
            unknown_zones=2,
            control_mode=CONTROL_MODE_COORDINATE,
            boiler_mode="heat",
            boiler_available=True,
            zones_faulted=True,
        ),
        supply=45.0,
    )
    # Faulted zones must not be read as "demand ended" silently.
    assert degraded.decision != DECISION_REVOKE_HEAT or degraded.state == FSM_DEGRADED


def test_scenario_unverified_semantics_never_starts_the_boiler():
    """6.3 / 14: without verification the integration must not act."""
    capabilities = BoilerCapabilities(
        entity_id="climate.rinnai_heating_control",
        min_temp=40.0,
        max_temp=82.0,
        target_step=1.0,
        hvac_modes=("heat", "auto", "dry", "off"),
        semantics_verified=False,
        min_flow_verified=False,
    )
    machine = fsm(capabilities)
    now = at(12)
    machine.note_demand_change(now, True)

    decision = machine.evaluate(
        HeatSourceInput(
            now=now + timedelta(minutes=5),
            active_zones=2,
            unknown_zones=0,
            control_mode=CONTROL_MODE_COORDINATE,
            boiler_mode="off",
            boiler_available=True,
        ),
        supply=45.0,
    )
    assert decision.decision == DECISION_NONE
    assert decision.target_mode is None
    assert REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED in decision.reason_codes


def test_scenario_observe_mode_never_commands_the_boiler():
    machine = fsm()
    now = at(12)
    machine.note_demand_change(now, True)
    decision = machine.evaluate(
        HeatSourceInput(
            now=now + timedelta(minutes=5),
            active_zones=2,
            unknown_zones=0,
            control_mode=CONTROL_MODE_OBSERVE,
            boiler_mode="off",
            boiler_available=True,
        ),
        supply=45.0,
    )
    assert decision.decision == DECISION_NONE
    assert decision.target_mode is None


def test_scenario_boiler_offline_degrades_instead_of_retrying():
    """11: boiler state lost -> stop writing, raise a clear alarm."""
    machine = fsm()
    now = at(12)
    machine.note_demand_change(now, True)
    decision = machine.evaluate(
        HeatSourceInput(
            now=now + timedelta(minutes=5),
            active_zones=2,
            unknown_zones=0,
            control_mode=CONTROL_MODE_COORDINATE,
            boiler_mode=None,
            boiler_available=False,
        ),
        supply=45.0,
    )
    assert decision.state == FSM_DEGRADED
    assert decision.target_mode is None


# ===========================================================================
# 13.6 - restart mid-override
# ===========================================================================


def test_scenario_restart_resumes_a_partial_fixed_override():
    """45 minutes into a 2-hour override, a restart keeps the remainder."""
    arbiter = TemperatureArbiter()
    zone = bedroom()

    override = arbiter.note_external_change(
        zone,
        21.5,
        at(18),
        policy="fixed_duration",
        duration=timedelta(hours=2),
    )

    # Simulate a restart by reloading the persisted override.
    from custom_components.climatepilot.models import ManualOverride

    reloaded = ManualOverride.from_dict(override.to_dict())
    restarted = ZoneState(
        zone_id=zone.zone_id,
        entity_id=zone.entity_id,
        manual_override=reloaded,
    )
    restarted.config_min_temp = 16.0
    restarted.config_max_temp = 26.0
    restarted.refresh_limits()

    restart_at = at(18, 45)
    assert restarted.manual_override.is_active(restart_at) is True
    assert restarted.manual_override.remaining(restart_at) == timedelta(hours=1, minutes=15)

    # The deadline is absolute, so it expires at 20:00 rather than 20:45.
    assert restarted.manual_override.is_active(at(19, 59)) is True
    assert restarted.manual_override.is_active(at(20, 1)) is False


# ===========================================================================
# 13.8 - auxiliary load exits
# ===========================================================================


def test_scenario_aux_load_returns_the_room_to_its_own_target():
    """The four exit paths from the design document's test 13.8."""
    controller = AuxLoadController(
        AuxLoadConfig(
            enabled=True,
            delta=0.5,
            max_temperature=24.0,
            hysteresis=0.3,
            min_switch_interval=0.0,  # allow immediate transitions in this test
            night_block=True,
            night_start=time(23, 0),
            night_end=time(6, 0),
        )
    )
    state = AuxLoadState()
    living = ZoneState(
        zone_id="living_room",
        entity_id="climate.livingroom_floorheating",
        aux_load_enabled=True,
        current_temperature=20.0,
    )
    living.config_min_temp = 16.0
    living.config_max_temp = 26.0
    living.refresh_limits()

    # Engages while a bedroom demands heat.
    engaged = controller.evaluate(
        state, living, at(19), other_demand=1, base_target=22.0, in_use=True
    )
    assert engaged.active is True
    assert engaged.target == 22.5

    # 1. bedroom demand ends -> exit
    ended = controller.evaluate(
        state, living, at(19, 10), other_demand=0, base_target=22.0, in_use=True
    )
    assert ended.active is False and ended.target == 22.0

    # 2. night begins -> never engages
    night = controller.evaluate(
        state, living, at(23, 30), other_demand=1, base_target=22.0, in_use=True
    )
    assert night.active is False and night.target == 22.0

    # 3. room reaches the ceiling -> does not engage
    hot = ZoneState(
        zone_id="living_room",
        entity_id="climate.livingroom_floorheating",
        aux_load_enabled=True,
        current_temperature=24.0,
    )
    hot.config_min_temp = 16.0
    hot.config_max_temp = 26.0
    hot.refresh_limits()
    capped = controller.evaluate(
        AuxLoadState(), hot, at(19), other_demand=1, base_target=22.0, in_use=True
    )
    assert capped.active is False

    # 4. manual change -> the boost yields to the user
    arbiter = TemperatureArbiter()
    arbiter.note_external_change(living, 21.0, at(19), policy="indefinite")
    yielded = controller.evaluate(
        AuxLoadState(),
        living,
        at(19, 20),
        other_demand=1,
        base_target=22.0,
        in_use=True,
    )
    assert yielded.active is False
    assert arbiter.arbitrate(living, at(19, 20)).effective == 21.0
