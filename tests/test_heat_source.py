"""Unit tests for the heat source demand FSM (design doc 6).

The tests concentrate on the safety contract rather than the happy path:

* unknown is never treated as "no demand";
* no ``heat``/``off`` write without verified device semantics;
* software minimum on-time must never keep the burner firing once every
  actuator is closed (design doc 6.2, last line);
* communication faults stop new writes.
"""

from __future__ import annotations

import ast
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Defensive isolation. The pure engines must remain testable without Home
# Assistant (docs/INTERFACES.md section 0.1), but importing the package runs
# ``custom_components/climatepilot/__init__.py``, which *does* import HA. If
# that file ever breaks again (for example an HA API move such as ``ServiceCall``
# leaving ``homeassistant.const``), these engine tests would die at collection
# even though the engines themselves are clean. So: use the real package when it
# imports, and only fall back to a stub package bound to the real directory.
try:  # pragma: no cover - import guard
    import custom_components.climatepilot as _real_package  # noqa: F401
except Exception:  # pragma: no cover - defensive fallback
    import types

    _parent = sys.modules.get("custom_components")
    if _parent is None:
        _parent = types.ModuleType("custom_components")
        sys.modules["custom_components"] = _parent
    _parent.__path__ = [str(_REPO_ROOT / "custom_components")]  # type: ignore[attr-defined]

    _stub = types.ModuleType("custom_components.climatepilot")
    _stub.__path__ = [str(_REPO_ROOT / "custom_components" / "climatepilot")]  # type: ignore[attr-defined]
    sys.modules["custom_components.climatepilot"] = _stub
    _parent.climatepilot = _stub  # type: ignore[attr-defined]

from custom_components.climatepilot import heat_source as hs
from custom_components.climatepilot.const import (
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
    DECISION_DEGRADED,
    DECISION_NONE,
    DECISION_REQUEST_HEAT,
    DECISION_REVOKE_HEAT,
    DECISION_SET_SUPPLY,
    FSM_DEGRADED,
    FSM_HEATING,
    FSM_IDLE,
    FSM_OBSERVE,
    FSM_PREPARE,
    FSM_SATISFIED,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    REASON_BOILER_MIN_FLOW_UNVERIFIED,
    REASON_CONTROL_PAUSED,
    REASON_DEGRADED,
    REASON_DEMAND_DEBOUNCE,
    REASON_ENTITY_UNAVAILABLE,
    REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED,
    REASON_MIN_OFF_TIME,
    REASON_MIN_ON_TIME,
    REASON_OBSERVE_MODE,
    REASON_PUMP_OVERRUN,
    REASON_RANGE_CLAMP,
    REASON_VALVE_PREOPEN,
)
from custom_components.climatepilot.heat_source import (
    BoilerCapabilities,
    HeatSourceConfig,
    HeatSourceInput,
    HeatSourceStateMachine,
)

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
MODULE_PATH = Path(hs.__file__)

# Debounces shortened so tests exercise transitions without long waits.
FAST = HeatSourceConfig(
    demand_on_debounce=60.0,
    demand_off_debounce=300.0,
    valve_preopen_wait=180.0,
    min_on_time=300.0,
    min_off_time=300.0,
    pump_overrun=180.0,
)

# A huge minimum on-time with a short off-debounce: makes it unambiguous that a
# revoke happened *far* before min_on_time could ever justify holding the burn.
SAFETY = HeatSourceConfig(
    demand_on_debounce=60.0,
    demand_off_debounce=60.0,
    valve_preopen_wait=180.0,
    min_on_time=3600.0,
    min_off_time=300.0,
    pump_overrun=180.0,
)


def caps(
    *,
    semantics: bool = True,
    min_flow: bool = True,
    hvac_modes: tuple[str, ...] = (HVAC_MODE_HEAT, HVAC_MODE_OFF),
    min_temp: float | None = 35.0,
    max_temp: float | None = 55.0,
    step: float = 0.5,
) -> BoilerCapabilities:
    return BoilerCapabilities(
        entity_id="climate.boiler",
        min_temp=min_temp,
        max_temp=max_temp,
        target_step=step,
        hvac_modes=hvac_modes,
        semantics_verified=semantics,
        min_flow_verified=min_flow,
    )


def machine(
    capabilities: BoilerCapabilities | None = None,
    config: HeatSourceConfig = FAST,
) -> HeatSourceStateMachine:
    return HeatSourceStateMachine(config=config, capabilities=capabilities)


def data(
    now: datetime,
    *,
    active: int = 0,
    unknown: int = 0,
    mode: str = CONTROL_MODE_COORDINATE,
    boiler_mode: str | None = HVAC_MODE_OFF,
    available: bool = True,
    faulted: bool = False,
) -> HeatSourceInput:
    return HeatSourceInput(
        now=now,
        active_zones=active,
        unknown_zones=unknown,
        control_mode=mode,
        boiler_mode=boiler_mode,
        boiler_available=available,
        zones_faulted=faulted,
    )


def run_to_heating(fsm: HeatSourceStateMachine, start: datetime = T0) -> datetime:
    """Drive the FSM from idle to a live heat request; return the heat time."""
    now = start
    fsm.evaluate(data(now, active=1), supply=45.0)
    now += timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(now, active=1), supply=45.0)
    now += timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(now, active=1), supply=45.0)
    assert decision.decision == DECISION_REQUEST_HEAT
    return now


def run_to_revoke(
    fsm: HeatSourceStateMachine,
    heat_at: datetime | None = None,
    *,
    config: HeatSourceConfig = FAST,
    unknown: int = 0,
    boiler_mode: str | None = HVAC_MODE_HEAT,
) -> tuple[object, datetime]:
    """Drive the FSM from heating to a revoke; return ``(decision, at)``.

    The off-debounce clock starts at the *first* no-demand evaluation, so two
    cycles are needed: one to seed the clock, one after it has elapsed.
    """
    start = run_to_heating(fsm) if heat_at is None else heat_at
    first = fsm.evaluate(
        data(start, active=0, unknown=unknown, boiler_mode=boiler_mode), supply=45.0
    )
    assert first.decision != DECISION_REVOKE_HEAT, "revoked before the debounce"
    at = start + timedelta(seconds=config.demand_off_debounce)
    return (
        fsm.evaluate(data(at, active=0, unknown=unknown, boiler_mode=boiler_mode), supply=45.0),
        at,
    )


# ---------------------------------------------------------------------------
# Module purity / clock discipline
# ---------------------------------------------------------------------------
def test_module_does_not_import_homeassistant() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert [n for n in imported if n.split(".")[0] == "homeassistant"] == []


@pytest.mark.parametrize("forbidden", ["now", "utcnow", "today", "time"])
def test_engine_never_reads_the_wall_clock(forbidden: str) -> None:
    source = inspect.getsource(hs)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


# ---------------------------------------------------------------------------
# Semantics gate (design doc 6.3 / 14)
# ---------------------------------------------------------------------------
def test_unverified_semantics_never_issues_heat_or_off() -> None:
    """Without the user's three attestations the FSM must not touch the mode."""
    fsm = machine(caps(semantics=False))
    now = T0
    for _ in range(6):
        decision = fsm.evaluate(data(now, active=3), supply=45.0)
        assert decision.target_mode is None
        assert decision.decision == DECISION_NONE
        now += timedelta(seconds=120)

    assert fsm.state == FSM_OBSERVE
    assert REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED in decision.reason_codes


def test_unverified_semantics_also_reports_min_flow_gate() -> None:
    """Both user-facing gates appear so the entity can explain the inaction."""
    fsm = machine(caps(semantics=False, min_flow=False))
    decision = fsm.evaluate(data(T0, active=2), supply=45.0)
    assert REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED in decision.reason_codes
    assert REASON_BOILER_MIN_FLOW_UNVERIFIED in decision.reason_codes


def test_no_capabilities_at_all_is_treated_as_unverified() -> None:
    fsm = machine(None)
    decision = fsm.evaluate(data(T0, active=2), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert decision.target_mode is None
    assert REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED in decision.reason_codes


def test_unverified_semantics_never_emits_set_supply() -> None:
    """Writing supply also needs verified semantics (INTERFACES 2.3 + lead)."""
    fsm = machine(caps(semantics=False))
    now = T0
    for _ in range(8):
        assert fsm.evaluate(data(now, active=1), supply=50.0).decision != (DECISION_SET_SUPPLY)
        now += timedelta(seconds=120)


def test_can_write_supply_requires_verified_semantics() -> None:
    assert machine(caps(semantics=True)).can_write_supply is True
    assert machine(caps(semantics=False)).can_write_supply is False
    assert machine(None).can_write_supply is False


def test_can_write_supply_is_false_while_degraded() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, available=False), supply=45.0)
    assert fsm.state == FSM_DEGRADED
    assert fsm.can_write_supply is False


def test_capabilities_property_returns_constructor_value() -> None:
    given = caps()
    assert machine(given).capabilities is given
    assert machine(None).capabilities is None


def test_min_flow_unverified_blocks_starting_a_burn() -> None:
    """Hydraulics unverified: no new burn, and the gate is explained."""
    fsm = machine(caps(min_flow=False))
    now = T0
    fsm.evaluate(data(now, active=1), supply=45.0)
    now += timedelta(seconds=FAST.demand_on_debounce)
    decision = fsm.evaluate(data(now, active=1), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert REASON_BOILER_MIN_FLOW_UNVERIFIED in decision.reason_codes
    assert decision.target_mode is None


def test_heat_absent_from_hvac_modes_blocks_starting() -> None:
    fsm = machine(caps(hvac_modes=(HVAC_MODE_OFF,)))
    now = T0
    fsm.evaluate(data(now, active=1), supply=45.0)
    now += timedelta(seconds=FAST.demand_on_debounce)
    decision = fsm.evaluate(data(now, active=1), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert decision.target_mode is None


# ---------------------------------------------------------------------------
# Observe / paused
# ---------------------------------------------------------------------------
def test_observe_mode_never_writes() -> None:
    fsm = machine(caps())
    decision = fsm.evaluate(data(T0, active=5, mode=CONTROL_MODE_OBSERVE), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert decision.state == FSM_OBSERVE
    assert REASON_OBSERVE_MODE in decision.reason_codes


def test_unknown_control_mode_falls_back_to_observe() -> None:
    fsm = machine(caps())
    decision = fsm.evaluate(data(T0, active=5, mode="banana"), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert decision.state == FSM_OBSERVE


def test_paused_mode_never_starts_heating() -> None:
    fsm = machine(caps())
    now = T0
    for _ in range(5):
        decision = fsm.evaluate(data(now, active=3, mode=CONTROL_MODE_PAUSED), supply=45.0)
        assert decision.decision == DECISION_NONE
        assert decision.target_mode is None
        assert REASON_CONTROL_PAUSED in decision.reason_codes
        now += timedelta(seconds=120)


def test_paused_mode_revokes_our_own_outstanding_request() -> None:
    """Pausing must not leave our own heat request running unattended."""
    fsm = machine(caps())
    now = run_to_heating(fsm)
    decision = fsm.evaluate(
        data(now, mode=CONTROL_MODE_PAUSED, boiler_mode=HVAC_MODE_HEAT), supply=45.0
    )
    assert decision.decision == DECISION_REVOKE_HEAT
    assert decision.target_mode == HVAC_MODE_OFF
    assert REASON_CONTROL_PAUSED in decision.reason_codes


# ---------------------------------------------------------------------------
# Demand debounce
# ---------------------------------------------------------------------------
def test_demand_debounce_delays_the_heat_request() -> None:
    fsm = machine(caps())
    first = fsm.evaluate(data(T0, active=1), supply=45.0)
    assert first.decision == DECISION_NONE
    assert first.state == FSM_IDLE
    assert REASON_DEMAND_DEBOUNCE in first.reason_codes
    assert first.wait_seconds == pytest.approx(FAST.demand_on_debounce)


def test_brief_demand_flicker_never_starts_heating() -> None:
    """A demand shorter than the on-debounce must not fire the boiler."""
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    # Demand disappears well before the debounce elapses.
    fsm.evaluate(data(T0 + timedelta(seconds=10), active=0), supply=45.0)
    decision = fsm.evaluate(
        data(T0 + timedelta(seconds=FAST.demand_on_debounce + 1), active=0),
        supply=45.0,
    )
    assert decision.decision == DECISION_NONE
    assert fsm.state == FSM_IDLE


def test_demand_on_debounce_then_prepare() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    decision = fsm.evaluate(data(at, active=1), supply=45.0)
    assert decision.state == FSM_PREPARE
    assert decision.decision == DECISION_NONE
    assert REASON_VALVE_PREOPEN in decision.reason_codes


def test_off_debounce_holds_demand_through_a_brief_gap() -> None:
    """A short demand gap must not flicker the boiler off and on."""
    fsm = machine(caps())
    now = run_to_heating(fsm)
    # Demand drops for less than the off-debounce.
    decision = fsm.evaluate(
        data(now + timedelta(seconds=10), active=0, boiler_mode=HVAC_MODE_HEAT),
        supply=45.0,
    )
    assert decision.decision != DECISION_REVOKE_HEAT
    assert decision.state == FSM_HEATING
    assert REASON_DEMAND_DEBOUNCE in decision.reason_codes


# ---------------------------------------------------------------------------
# Valve pre-open wait
# ---------------------------------------------------------------------------
def test_valve_preopen_wait_delays_the_request() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    decision = fsm.evaluate(data(at, active=1), supply=45.0)
    assert decision.state == FSM_PREPARE
    assert decision.wait_seconds == pytest.approx(FAST.valve_preopen_wait)


def test_request_issued_only_after_the_preopen_wait() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(at, active=1), supply=45.0)
    # One second short of the pre-open window.
    almost = at + timedelta(seconds=FAST.valve_preopen_wait - 1)
    assert fsm.evaluate(data(almost, active=1), supply=45.0).state == FSM_PREPARE

    ready = at + timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(ready, active=1), supply=45.0)
    assert decision.decision == DECISION_REQUEST_HEAT
    assert decision.target_mode == HVAC_MODE_HEAT
    assert fsm.state == FSM_HEATING


def test_demand_lost_during_prepare_returns_to_idle() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    assert fsm.evaluate(data(at, active=1), supply=45.0).state == FSM_PREPARE
    decision = fsm.evaluate(data(at + timedelta(seconds=5), active=0), supply=45.0)
    assert decision.decision == DECISION_NONE
    assert fsm.state == FSM_IDLE


# ---------------------------------------------------------------------------
# HARD SAFETY: all actuators closed beats min_on_time
# ---------------------------------------------------------------------------
def test_all_closed_revokes_immediately_despite_min_on_time() -> None:
    """The core safety rule of design doc 6.2.

    Every valve is closed while the burn is far younger than ``min_on_time``
    (set to a full hour here). Software must revoke the request as soon as the
    off-debounce elapses instead of forcing the burner to keep heating.
    """
    fsm = machine(caps(), config=SAFETY)
    heat_at = run_to_heating(fsm)
    decision, at = run_to_revoke(fsm, heat_at, config=SAFETY)

    burn_age = (at - heat_at).total_seconds()
    assert burn_age < SAFETY.min_on_time, "test must run inside min_on_time"
    assert decision.decision == DECISION_REVOKE_HEAT
    assert decision.target_mode == HVAC_MODE_OFF
    assert decision.state == FSM_SATISFIED
    # min_on_time must not appear as the blocker on this path.
    assert REASON_MIN_ON_TIME not in decision.reason_codes


def test_all_closed_never_returns_request_heat_on_later_cycles() -> None:
    fsm = machine(caps(), config=SAFETY)
    heat_at = run_to_heating(fsm)
    _, at = run_to_revoke(fsm, heat_at, config=SAFETY)
    for step in range(1, 6):
        decision = fsm.evaluate(
            data(at + timedelta(seconds=step * 30), active=0, boiler_mode=HVAC_MODE_OFF),
            supply=45.0,
        )
        assert decision.decision != DECISION_REQUEST_HEAT
        assert decision.target_mode != HVAC_MODE_HEAT


def test_min_off_time_blocks_restarting_too_soon() -> None:
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    _, at = run_to_revoke(fsm, heat_at)

    # New demand arrives immediately after the revoke.
    soon = at + timedelta(seconds=10)
    fsm.evaluate(data(soon, active=1), supply=45.0)
    blocked = fsm.evaluate(
        data(soon + timedelta(seconds=FAST.demand_on_debounce), active=1), supply=45.0
    )
    assert blocked.decision == DECISION_NONE
    assert REASON_MIN_OFF_TIME in blocked.reason_codes
    assert blocked.wait_seconds is not None


def test_restart_allowed_once_min_off_time_elapsed() -> None:
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    _, at = run_to_revoke(fsm, heat_at)

    restart = at + timedelta(seconds=FAST.min_off_time + 1)
    fsm.evaluate(data(restart, active=1), supply=45.0)
    after = restart + timedelta(seconds=FAST.demand_on_debounce)
    assert fsm.evaluate(data(after, active=1), supply=45.0).state == FSM_PREPARE
    ready = after + timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(ready, active=1), supply=45.0)
    assert decision.decision == DECISION_REQUEST_HEAT


# ---------------------------------------------------------------------------
# Unknown zones
# ---------------------------------------------------------------------------
def test_unknown_zones_are_not_counted_as_demand() -> None:
    """unknown != "no demand", but it is also never demand."""
    fsm = machine(caps())
    now = T0
    for _ in range(6):
        decision = fsm.evaluate(data(now, active=0, unknown=3), supply=45.0)
        assert decision.decision == DECISION_NONE
        assert decision.target_mode is None
        now += timedelta(seconds=120)


def test_unknown_zones_do_not_authorise_a_revoke_during_grace() -> None:
    """With unknown zones we cannot prove all valves closed, so we hold."""
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    # Seed the off-debounce clock, then evaluate with an unknown zone present.
    fsm.evaluate(data(heat_at, active=0, unknown=1), supply=45.0)
    at = heat_at + timedelta(seconds=FAST.demand_off_debounce)
    decision = fsm.evaluate(data(at, active=0, unknown=1, boiler_mode=HVAC_MODE_HEAT), supply=45.0)
    assert decision.decision != DECISION_REVOKE_HEAT
    assert REASON_ENTITY_UNAVAILABLE in decision.reason_codes


def test_prolonged_unknown_zones_degrade_for_manual_takeover() -> None:
    """Unknown must not mean "fire forever": degrade and hand over to a human."""
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    fsm.evaluate(data(heat_at, active=0, unknown=2), supply=45.0)
    at = heat_at + timedelta(seconds=FAST.demand_off_debounce)
    fsm.evaluate(data(at, active=0, unknown=2, boiler_mode=HVAC_MODE_HEAT), supply=45.0)

    later = at + timedelta(seconds=FAST.min_on_time + 1)
    decision = fsm.evaluate(
        data(later, active=0, unknown=2, boiler_mode=HVAC_MODE_HEAT), supply=45.0
    )
    assert decision.state == FSM_DEGRADED
    assert decision.decision == DECISION_DEGRADED
    assert REASON_DEGRADED in decision.reason_codes


# ---------------------------------------------------------------------------
# Supply temperature clamping
# ---------------------------------------------------------------------------
def test_supply_is_clamped_to_the_boiler_range() -> None:
    fsm = machine(caps(min_temp=35.0, max_temp=55.0))
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(at, active=1), supply=45.0)
    ready = at + timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(ready, active=1), supply=80.0)
    assert decision.target_supply == pytest.approx(55.0)
    assert decision.target_mode == HVAC_MODE_HEAT
    assert REASON_RANGE_CLAMP in decision.reason_codes


def test_supply_is_snapped_to_the_target_step() -> None:
    fsm = machine(caps(step=0.5))
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(at, active=1), supply=45.0)
    ready = at + timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(ready, active=1), supply=44.3)
    assert decision.target_supply == pytest.approx(44.5)


def test_supply_snapping_cannot_escape_the_range() -> None:
    fsm = machine(caps(min_temp=35.0, max_temp=55.0, step=0.5))
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(at, active=1), supply=45.0)
    ready = at + timedelta(seconds=FAST.valve_preopen_wait)
    high = fsm.evaluate(data(ready, active=1), supply=54.9)
    assert high.target_supply == pytest.approx(55.0)
    assert 35.0 <= high.target_supply <= 55.0


def test_invalid_supply_is_dropped() -> None:
    fsm = machine(caps())
    fsm.evaluate(data(T0, active=1), supply=45.0)
    at = T0 + timedelta(seconds=FAST.demand_on_debounce)
    fsm.evaluate(data(at, active=1), supply=45.0)
    ready = at + timedelta(seconds=FAST.valve_preopen_wait)
    decision = fsm.evaluate(data(ready, active=1), supply=float("nan"))
    assert decision.decision == DECISION_REQUEST_HEAT
    assert decision.target_supply is None


def test_set_supply_decision_once_already_heating() -> None:
    fsm = machine(caps())
    now = run_to_heating(fsm)
    decision = fsm.evaluate(
        data(now + timedelta(seconds=30), active=1, boiler_mode=HVAC_MODE_HEAT),
        supply=46.0,
    )
    assert decision.decision == DECISION_SET_SUPPLY
    assert decision.target_supply == pytest.approx(46.0)


# ---------------------------------------------------------------------------
# Degraded
# ---------------------------------------------------------------------------
def test_boiler_unavailable_degrades_and_stops_writing() -> None:
    fsm = machine(caps())
    decision = fsm.evaluate(data(T0, available=False), supply=45.0)
    assert decision.state == FSM_DEGRADED
    assert decision.decision == DECISION_DEGRADED
    assert decision.target_mode is None
    assert REASON_ENTITY_UNAVAILABLE in decision.reason_codes


def test_boiler_unavailable_never_requests_heat() -> None:
    fsm = machine(caps())
    now = T0
    for _ in range(6):
        decision = fsm.evaluate(data(now, active=5, available=False), supply=45.0)
        assert decision.target_mode != HVAC_MODE_HEAT
        now += timedelta(seconds=120)


def test_faulted_zones_degrade() -> None:
    fsm = machine(caps())
    decision = fsm.evaluate(data(T0, active=1, faulted=True), supply=45.0)
    assert decision.state == FSM_DEGRADED
    assert decision.decision == DECISION_DEGRADED


def test_faulted_zones_revoke_our_own_outstanding_request() -> None:
    """A fault may mean all valves closed: withdraw our request if we can."""
    fsm = machine(caps())
    now = run_to_heating(fsm)
    decision = fsm.evaluate(
        data(now + timedelta(seconds=5), faulted=True, boiler_mode=HVAC_MODE_HEAT),
        supply=45.0,
    )
    assert decision.decision == DECISION_REVOKE_HEAT
    assert decision.target_mode == HVAC_MODE_OFF
    assert decision.state == FSM_DEGRADED


def test_faulted_zones_without_our_request_do_not_write_mode() -> None:
    fsm = machine(caps())
    decision = fsm.evaluate(data(T0, faulted=True, boiler_mode=HVAC_MODE_OFF), supply=45.0)
    assert decision.decision == DECISION_DEGRADED
    assert decision.target_mode is None


# ---------------------------------------------------------------------------
# Pump overrun / satisfied
# ---------------------------------------------------------------------------
def test_pump_overrun_is_tracked_after_revoke() -> None:
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    _, at = run_to_revoke(fsm, heat_at)
    assert fsm.state == FSM_SATISFIED

    during = fsm.evaluate(data(at + timedelta(seconds=30), active=0), supply=45.0)
    assert during.state == FSM_SATISFIED
    assert REASON_PUMP_OVERRUN in during.reason_codes
    assert during.wait_seconds == pytest.approx(FAST.pump_overrun - 30)

    after = fsm.evaluate(data(at + timedelta(seconds=FAST.pump_overrun + 1), active=0), supply=45.0)
    assert after.state == FSM_IDLE


# ---------------------------------------------------------------------------
# note_demand_change
# ---------------------------------------------------------------------------
def test_note_demand_change_seeds_the_on_debounce() -> None:
    fsm = machine(caps())
    fsm.note_demand_change(T0, True)
    decision = fsm.evaluate(data(T0 + timedelta(seconds=10), active=1), supply=45.0)
    assert REASON_DEMAND_DEBOUNCE in decision.reason_codes


def test_note_demand_change_seeds_the_off_debounce() -> None:
    fsm = machine(caps())
    now = run_to_heating(fsm)
    fsm.note_demand_change(now + timedelta(seconds=1), False)
    decision = fsm.evaluate(
        data(now + timedelta(seconds=5), active=0, boiler_mode=HVAC_MODE_HEAT),
        supply=45.0,
    )
    assert decision.decision != DECISION_REVOKE_HEAT


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip() -> None:
    fsm = machine(caps())
    run_to_heating(fsm)
    before = fsm.snapshot()

    restored = machine(caps())
    restored.restore(before)

    assert restored.snapshot() == before
    assert restored.state == FSM_HEATING


def test_restore_keeps_a_live_burn_across_restart() -> None:
    """A restart must not silently drop an outstanding heat request."""
    fsm = machine(caps())
    now = run_to_heating(fsm)

    restored = machine(caps())
    restored.restore(fsm.snapshot())

    decision = restored.evaluate(
        data(now + timedelta(seconds=30), active=1, boiler_mode=HVAC_MODE_HEAT),
        supply=45.0,
    )
    assert decision.state == FSM_HEATING
    assert decision.decision == DECISION_SET_SUPPLY


def test_restore_of_inconsistent_heating_state_falls_back_to_idle() -> None:
    """A heating state with no burn start would otherwise fire forever."""
    fsm = machine(caps())
    fsm.restore({"state": FSM_HEATING, "heat_requested": True, "heat_started_at": None})
    assert fsm.state == FSM_IDLE


def test_restore_is_tolerant_of_garbage() -> None:
    fsm = machine(caps())
    fsm.restore({})
    fsm.restore({"state": "not_a_state", "demand_on_since": 12345})
    assert fsm.state == FSM_IDLE
    assert fsm.snapshot()["demand_on_since"] is None


def test_restored_min_off_time_still_blocks_restart() -> None:
    fsm = machine(caps())
    heat_at = run_to_heating(fsm)
    _, at = run_to_revoke(fsm, heat_at)

    restored = machine(caps())
    restored.restore(fsm.snapshot())

    soon = at + timedelta(seconds=10)
    restored.evaluate(data(soon, active=1), supply=45.0)
    blocked = restored.evaluate(
        data(soon + timedelta(seconds=FAST.demand_on_debounce), active=1), supply=45.0
    )
    assert blocked.decision == DECISION_NONE
    assert REASON_MIN_OFF_TIME in blocked.reason_codes
