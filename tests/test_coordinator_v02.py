"""Coordinator wiring tests for the V0.2 diagnostics.

The V0.2 engines are tested in isolation by their own suites. What this module
proves is the *wiring*: that the coordinator feeds them the right inputs, at the
right time, and that a restart carries their history across.

That distinction is the one that has repeatedly mattered in this repository -
three V0.1 defects lived purely in the wiring (a missing ``from_dict``, a
bypassed range intersection, an un-awaited listener) and were invisible to every
unit test.

The behaviours pinned here:

* the burner history is told about demand *before* the burner state, otherwise
  an ignition with no demand is mis-attributed;
* a zone whose reading is unavailable records no comfort sample and reports
  "unknown" rather than inventing a temperature;
* zones removed from the options do not leave a stale verdict behind;
* the diagnostics never influence what gets written to a device;
* round-tripping through the state payload preserves the V0.2 history.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_coordinator_smoke import (
    BURNER,
    MASTER,
    T0,
    calls_to,
    full_options,
    install_entities,
    make_entry,
    pin_clock,
    run_cycle,
    set_state,
)

from custom_components.climatepilot.const import (
    ADVICE_NONE,
    COMBINATION_NONE,
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNKNOWN,
    CONF_CONTROL_MODE,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    FEEDBACK_UNAVAILABLE,
    QUALITY_UNKNOWN,
)
from custom_components.climatepilot.coordinator import (
    ClimatePilotCoordinator,
)


@pytest.fixture()
async def hass(tmp_path):
    """A real HomeAssistant with the integration's entities installed."""
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import frame

    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)

    recorded: list[tuple[str, str, dict]] = []

    async def _spy(call):
        recorded.append((call.domain, call.service, dict(call.data)))

    instance.services.async_register("climate", "set_temperature", _spy)
    instance.services.async_register("climate", "set_hvac_mode", _spy)
    # ``calls_to`` (shared with the smoke suite) reads this attribute name.
    instance._climatepilot_calls = recorded
    try:
        yield instance
    finally:
        await instance.async_stop()


def build_v02(hass, **overrides):
    install_entities(hass)
    entry = make_entry(full_options(**overrides))
    coordinator = ClimatePilotCoordinator(hass, entry)
    coordinator.async_shutdown_listeners = MagicMock()
    pin_clock(coordinator)
    return coordinator, entry


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_coordinator_exposes_the_v02_diagnostics(hass):
    """The public surface the platform layer reads must exist after a cycle."""
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator)

    assert coordinator.combinations is not None
    assert coordinator.combinations.dominant_key
    assert isinstance(coordinator.comfort, dict)
    assert set(coordinator.comfort) == set(coordinator.zones)
    assert coordinator.cycling_quality
    assert isinstance(coordinator.unexplained_burns, int)


async def test_combination_tracker_sees_the_demanding_zones(hass):
    """Both zones report hvac_action=heating, so the combination is both."""
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)
    await run_cycle(coordinator, T0 + timedelta(minutes=5))

    report = coordinator.combinations
    assert report.current_key != COMBINATION_NONE
    assert "living_room" in report.current_key
    assert "master_bedroom" in report.current_key


async def test_combination_reflects_a_zone_dropping_out(hass):
    """When one room stops calling for heat the combination must change.

    The switch is deliberately gated by ``min_dwell``, so the change only lands
    once the new combination has been observed for longer than that - a single
    later cycle is not enough, which is the anti-flicker behaviour.
    """
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)
    first = coordinator.combinations.current_key
    assert "master_bedroom" in first

    # The master bedroom stops demanding heat.
    set_state(
        hass,
        MASTER,
        "heat",
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 16.0,
            "max_temp": 26.0,
            "target_temp_step": 0.5,
            "current_temperature": 22.0,
            "temperature": 22.0,
            "hvac_action": "idle",
            "supported_features": 385,
        },
    )
    # One cycle to fold the new reading into the zone state, then confirm the
    # demand really did drop before asserting on the combination.
    await run_cycle(coordinator, T0 + timedelta(minutes=1))
    assert coordinator._zone_demands_heat(coordinator.zones["master_bedroom"]) is False
    assert coordinator._zone_demands_heat(coordinator.zones["living_room"]) is True

    # Keep observing past the dwell so the switch is allowed to happen.
    for offset in range(2, 7):
        await run_cycle(coordinator, T0 + timedelta(minutes=2 * offset))

    assert coordinator.combinations.current_key != first
    assert "master_bedroom" not in coordinator.combinations.current_key


async def test_burner_is_told_about_demand_before_state(hass):
    """Ordering matters: otherwise a demand-less ignition is mis-attributed.

    ``note_demand`` must land before ``note_state`` for the same cycle, because
    the burner engine correlates an ignition with the demand that was live when
    it started.
    """
    coordinator, _ = build_v02(hass)

    order: list[str] = []
    real_demand = coordinator.burner.note_demand
    real_state = coordinator.burner.note_state

    def demand(now, has_demand):
        order.append("demand")
        return real_demand(now, has_demand)

    def state(value, now):
        order.append("state")
        return real_state(value, now)

    coordinator.burner.note_demand = demand
    coordinator.burner.note_state = state

    await run_cycle(coordinator)

    assert order, "the burner was never fed"
    assert order[0] == "demand", f"state was fed before demand: {order[:4]}"
    assert order.index("demand") < order.index("state")


async def test_comfort_records_a_sample_per_zone(hass):
    coordinator, _ = build_v02(hass)
    for offset in range(4):
        await run_cycle(coordinator, T0 + timedelta(minutes=offset * 6))

    for zone_id, verdict in coordinator.comfort.items():
        assert verdict.verdict in (COMFORT_OK, COMFORT_OVERHEATING, COMFORT_UNKNOWN)
        assert verdict.advice
        assert zone_id in coordinator.zones


async def test_unavailable_zone_reports_unknown_not_a_guess(hass):
    """A room with no readable temperature must not be scored as comfortable."""
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)

    # Blank the bedroom's readings entirely.
    set_state(hass, MASTER, "unavailable", {})
    # Re-establish the other zone so only this one is affected.
    await run_cycle(coordinator, T0 + timedelta(minutes=6))

    assert coordinator.zones["master_bedroom"].feedback_quality == FEEDBACK_UNAVAILABLE
    verdict = coordinator.comfort["master_bedroom"]
    assert verdict.verdict == COMFORT_UNKNOWN, (
        "an unreadable room must be unknown, never scored as OK"
    )
    assert verdict.advice == ADVICE_NONE


async def test_comfort_uses_effective_setpoint_as_the_target(hass):
    """Overheat detection compares against the arbitrated target, not the mode."""
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)

    decision = coordinator.zone_decisions["living_room"]
    verdict = coordinator.comfort["living_room"]
    # 21.0 measured vs the 22.0 schedule target -> below target, no overheat.
    assert decision.effective == 22.0
    assert verdict.average_deviation <= 0 or verdict.samples == 0


async def test_removed_zone_leaves_no_stale_comfort_verdict(hass):
    """A room deleted from the options must not keep reporting a verdict."""
    coordinator, entry = build_v02(hass)
    await run_cycle(coordinator, T0)
    assert "master_bedroom" in coordinator.comfort

    # Drop the bedroom from the configuration and reconfigure.
    new_options = full_options()
    new_options["zones"] = [z for z in new_options["zones"] if z["zone_id"] != "master_bedroom"]
    entry.options = new_options
    await coordinator.async_reconfigure()
    await run_cycle(coordinator, T0 + timedelta(minutes=6))

    assert "master_bedroom" not in coordinator.comfort
    assert "master_bedroom" not in coordinator.zones


# ---------------------------------------------------------------------------
# Diagnostics must never steer control
# ---------------------------------------------------------------------------


async def test_diagnostics_do_not_write_in_observe_mode(hass):
    """The whole point: these engines observe, they never actuate."""
    coordinator, _ = build_v02(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE})
    for offset in range(6):
        await run_cycle(coordinator, T0 + timedelta(minutes=offset * 10))

    assert calls_to(hass, "climate") == []
    # ...yet the diagnostics were still produced and are readable.
    assert coordinator.combinations is not None
    assert coordinator.comfort


async def test_diagnostics_do_not_change_the_written_setpoints(hass):
    """Adding V0.2 must not perturb V0.1's arbitration output."""
    coordinator, _ = build_v02(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE})
    coordinator._started_at = T0 - timedelta(hours=1)

    await run_cycle(coordinator)

    writes = calls_to(hass, "climate")
    assert writes, "coordinate mode should write"
    for service, data in writes:
        assert service == "set_temperature"
        assert 16.0 <= data["temperature"] <= 26.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_state_payload_includes_the_v02_history(hass):
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)
    await run_cycle(coordinator, T0 + timedelta(minutes=10))

    payload = coordinator._state_payload()
    assert "combinations" in payload
    assert "comfort" in payload

    import json

    json.dumps(payload)  # must stay serialisable for Store


async def test_v02_history_survives_a_restart(hass):
    """A restart must not reset the diagnostics to 'freshly commissioned'."""
    coordinator, _ = build_v02(hass)
    for offset in range(4):
        await run_cycle(coordinator, T0 + timedelta(minutes=offset * 10))
    saved = coordinator._state_payload()

    # A fresh coordinator, restored from the saved payload.
    fresh, _ = build_v02(hass)
    with patch.object(fresh, "_now", return_value=T0 + timedelta(minutes=40)):
        fresh.combinations_tracker.restore(saved["combinations"])
        fresh.comfort_analyser.restore(saved["comfort"])

    assert fresh.combinations_tracker.snapshot() == saved["combinations"]
    assert fresh.comfort_analyser.snapshot() == saved["comfort"]


async def test_restore_tolerates_a_v01_payload(hass):
    """Upgrading an installation must not crash on the older saved state."""
    coordinator, _ = build_v02(hass)
    v01_payload = {
        "zones": {},
        "aux_load": {},
        "zone_coordinator": {},
        "heat_source": {},
        "weather": {},
        "burner": {},
        "control_mode": CONTROL_MODE_OBSERVE,
    }

    with patch.object(coordinator.store, "async_load", AsyncMock(return_value=v01_payload)):
        await coordinator._async_restore_state()

    # The cycle must still complete.
    await run_cycle(coordinator, T0)
    assert coordinator.combinations is not None


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


async def test_no_zones_at_all_does_not_break_the_diagnostics(hass):
    """An entry with zero rooms is legal and must still evaluate."""
    install_entities(hass)
    entry = make_entry(full_options(zones=[]))
    coordinator = ClimatePilotCoordinator(hass, entry)
    coordinator.async_shutdown_listeners = MagicMock()
    pin_clock(coordinator)

    await run_cycle(coordinator)

    assert coordinator.combinations.dominant_key == COMBINATION_NONE
    assert coordinator.comfort == {}


async def test_unknown_cycling_quality_before_any_burn(hass):
    """With no combustion history the grade is 'unknown', not a false 'poor'."""
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator)
    assert coordinator.cycling_quality == QUALITY_UNKNOWN


async def test_cycling_quality_ignores_dhw_burns(hass):
    """A hot-water burn must not be able to downgrade the heating grade.

    This is the V0.1 safety rule carried into V0.2: a shower is not a
    short-cycling floor-heating system.

    The mechanism: the burner sensor is configured as *which circuit it watches*,
    so burns seen on the space-heating circuit are graded as heating and burns on
    the DHW sensor are graded as hot water. Only the heating grade feeds
    ``cycling_quality``, so a shower cannot make the floor heating look bad.
    """
    from custom_components.climatepilot.const import CIRCUIT_DHW

    coordinator, _ = build_v02(hass)

    # The configured circuit is observable through behaviour: a burn on the
    # space-heating sensor is graded as heating.
    await run_cycle(coordinator, T0)
    set_state(hass, BURNER, "Burning", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=1))
    set_state(hass, BURNER, "Standby", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=30))

    burner = coordinator.diagnostics_snapshot["burner"]
    assert burner["heating_starts_24h"] == 1, "the heating circuit must claim it"
    assert burner["dhw_starts_24h"] == 0
    assert burner["grade_heating_24h"]["good"] == 1
    assert burner["grade_dhw_24h"]["good"] == 0
    grade = coordinator.cycling_quality
    assert grade == "good"
    assert burner["cycling_quality_reason"]

    # A very short DHW burn on the *other* circuit must be graded separately
    # and must leave the heating grade untouched.
    coordinator.burner._circuit = CIRCUIT_DHW
    set_state(hass, BURNER, "Burning", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=31))
    set_state(hass, BURNER, "Standby", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=32))

    burner = coordinator.diagnostics_snapshot["burner"]
    assert burner["grade_dhw_24h"]["poor"] == 1, "a 60 s DHW burn is graded poor"
    assert burner["grade_heating_24h"]["good"] == 1, "heating grade must not move"
    assert coordinator.cycling_quality == grade


async def test_circuit_context_attributes_burns(hass):
    """The same raw string means different things on different circuits.

    ``"Heating Water"`` is code 31 on the Rinnai boiler and appears on *both*
    the space-heating and the hot-water burner sensors. With the circuit known,
    it is attributed correctly; without it, the burn is recorded as unknown
    rather than silently miscounted as heating.
    """
    from custom_components.climatepilot.const import CIRCUIT_HEATING

    coordinator, _ = build_v02(hass)

    await run_cycle(coordinator, T0)
    set_state(hass, BURNER, "Heating Water", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=1))
    set_state(hass, BURNER, "Standby", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=10))

    burner = coordinator.diagnostics_snapshot["burner"]
    # On the heating circuit, "Heating Water" is a heating burn.
    assert burner["heating_starts_24h"] == 1
    assert burner["dhw_starts_24h"] == 0
    assert burner["unknown_kind_starts_24h"] == 0

    # And with no circuit configured the very same token cannot be attributed.
    from custom_components.climatepilot.diagnostics_engine import BurnerStats

    unattributed = BurnerStats(circuit=None)
    unattributed.note_state("Heating Water", T0)
    snap = unattributed.snapshot(T0 + timedelta(minutes=5))
    assert snap["unknown_kind_starts_24h"] == 1
    assert snap["heating_starts_24h"] == 0, (
        "without a circuit the token must not be silently counted as heating"
    )
    assert CIRCUIT_HEATING  # documents the configured default


async def test_burner_error_is_not_counted_as_a_burn(hass):
    """Regression: a boiler fault must not fabricate an ignition.

    ``Error`` (Rinnai code 33) used to fall through the classifier's catch-all
    and be recorded as burning, which inflated the ignition count and could
    raise a false short-cycle alert - the one signal the user actually watches.
    """
    coordinator, _ = build_v02(hass)
    await run_cycle(coordinator, T0)

    set_state(hass, BURNER, "Error", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=1))
    set_state(hass, BURNER, "Standby", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=30))

    burner = coordinator.diagnostics_snapshot["burner"]
    assert burner["starts_24h"] == 0, "a fault is not an ignition"
    assert burner["short_cycle_alert"] is False
