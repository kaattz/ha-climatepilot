"""Coordinator wiring tests for the V0.3 probe diagnostics.

The V0.3 engines are tested in isolation by their own suites. This module proves
the wiring, and - more importantly - the *delivery standard* from design doc 12:

    a probe failure must not affect core heating.

That is the whole point of V0.3 being additive. A temperature probe is a
monitoring input; if losing one could change what gets written to a boiler, the
feature would have made the system less safe, not more informative.

The behaviours pinned here:

* an unconfigured probe is not a fault (most installations have none);
* an 85 °C DS18B20 sentinel never reaches the control loop as a hot pipe;
* a probe fault changes no write, in any control mode;
* ``None`` is carried through as "no usable probe", never substituted;
* the delta-T engine only ever sees values that survived the probe checks;
* the probe and delta-T history survives a restart.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_coordinator_smoke import (
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
    CONF_CONTROL_MODE,
    CONF_DELTA_T_HEALTHY_MAX,
    CONF_DELTA_T_HEALTHY_MIN,
    CONF_PROBE_STALE_SECONDS,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SUPPLY_PROBE_ENTITY,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    DS18B20_ERROR_C,
    PROBE_IMPLAUSIBLE,
    PROBE_UNAVAILABLE,
    PROBE_VALID,
)
from custom_components.climatepilot.coordinator import (
    ClimatePilotCoordinator,
)

SUPPLY_PROBE = "sensor.boiler_supply_pipe_temperature"
RETURN_PROBE = "sensor.boiler_return_pipe_temperature"


@pytest.fixture()
async def hass(tmp_path):
    """A real HomeAssistant with a probe-equipped installation."""
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import frame

    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)

    recorded: list[tuple[str, str, dict]] = []

    async def _spy(call):
        recorded.append((call.domain, call.service, dict(call.data)))

    instance.services.async_register("climate", "set_temperature", _spy)
    instance.services.async_register("climate", "set_hvac_mode", _spy)
    instance._climatepilot_calls = recorded
    try:
        yield instance
    finally:
        await instance.async_stop()


def probe_options(**overrides) -> dict:
    base = {
        CONF_SUPPLY_PROBE_ENTITY: SUPPLY_PROBE,
        CONF_RETURN_TEMP_ENTITY: RETURN_PROBE,
    }
    base.update(overrides)
    return base


def build_v03(hass, *, probes: bool = True, **overrides):
    install_entities(hass)
    options = full_options(**overrides)
    if probes:
        options.update(probe_options())
    entry = make_entry(options)
    coordinator = ClimatePilotCoordinator(hass, entry)
    coordinator.async_shutdown_listeners = MagicMock()
    pin_clock(coordinator)
    return coordinator, entry


def set_probe(hass, entity_id: str, value, *, when: datetime | None = None) -> None:
    """Set a probe state. ``value=None`` means the entity is unavailable."""
    if value is None:
        set_state(hass, entity_id, "unavailable", {})
        return
    set_state(
        hass,
        entity_id,
        str(value),
        {"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    state = hass.states.get(entity_id)
    if state is not None:
        state.last_updated = when or datetime.now(UTC)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_coordinator_exposes_the_v03_diagnostics(hass):
    """The public surface the platform layer reads must exist after a cycle."""
    coordinator, _ = build_v03(hass)
    await run_cycle(coordinator)

    assert coordinator.probes is not None
    assert coordinator.delta_t is not None
    assert isinstance(coordinator.probe_fault, bool)
    assert coordinator.probes_configured is True


async def test_probes_read_real_pipe_temperatures(hass):
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator)

    assert coordinator.probes.supply.quality == PROBE_VALID
    assert coordinator.probes.return_.quality == PROBE_VALID
    assert coordinator.probes.supply.value == 45.0
    assert coordinator.probes.return_.value == 38.0
    assert coordinator.probes.divergence == pytest.approx(7.0, abs=0.01)
    assert coordinator.delta_t.delta_t == pytest.approx(7.0, abs=0.01)


async def test_missing_probe_entity_is_unavailable_not_zero(hass):
    """A configured probe whose entity vanished must never read as 0 °C."""
    coordinator, _ = build_v03(hass)
    await run_cycle(coordinator)

    assert coordinator.probes.return_.value is None
    assert coordinator.probes.return_.quality == PROBE_UNAVAILABLE
    assert coordinator.probes.divergence is None
    assert coordinator.delta_t.delta_t is None


async def test_unconfigured_probes_are_not_a_fault(hass):
    """Most installations have no probes; that must not raise a permanent alert."""
    coordinator, _ = build_v03(hass, probes=False)
    await run_cycle(coordinator)

    assert coordinator.probes_configured is False
    assert coordinator.probe_fault is False
    assert coordinator.probes.divergence is None
    assert coordinator.delta_t.delta_t is None


# ---------------------------------------------------------------------------
# The 85 °C sentinel - the single most important V0.3 check
# ---------------------------------------------------------------------------


async def test_ds18b20_sentinel_is_rejected_not_treated_as_hot_pipe(hass):
    """85 °C means "conversion failed", not "very hot supply pipe".

    This is the failure mode V0.3 exists to prevent: a broken DS18B20 reports
    exactly 85 °C, which looks like a plausible supply temperature. Left
    unchecked it either invents a boiler fault or produces an enormous delta-T
    that looks like a hydraulic success.
    """
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, DS18B20_ERROR_C)
    set_probe(hass, RETURN_PROBE, 40.0)
    await run_cycle(coordinator)

    supply = coordinator.probes.supply
    assert supply.quality == PROBE_IMPLAUSIBLE
    assert supply.value is None, "an implausible reading must not carry a value"
    assert "probe_sentinel_value_85c" in supply.reason_codes

    # And the bogus value must not become a delta-T.
    assert coordinator.delta_t.delta_t is None
    assert coordinator.probe_fault is True


async def test_sentinel_near_misses_are_handled_by_the_documented_window(hass):
    """The tolerance window decides; a real 84 °C reading is not a sentinel.

    The return probe is kept a plausible distance below the supply so this test
    isolates the *sentinel* rule. A hot supply paired with a cold return is a
    different rule (pair divergence) and is covered separately.
    """
    from custom_components.climatepilot.const import (
        DS18B20_ERROR_TOLERANCE,
        PROBE_IMPLAUSIBLE,
    )

    sentinel_low = DS18B20_ERROR_C - DS18B20_ERROR_TOLERANCE
    sentinel_high = DS18B20_ERROR_C + DS18B20_ERROR_TOLERANCE

    for supply, expected in (
        (sentinel_low, PROBE_IMPLAUSIBLE),  # inside the window
        (DS18B20_ERROR_C, PROBE_IMPLAUSIBLE),  # exactly the sentinel
        (sentinel_high, PROBE_IMPLAUSIBLE),  # inside the window
        (sentinel_low - 0.1, PROBE_VALID),  # just outside: a real reading
    ):
        coordinator, _ = build_v03(hass)
        set_probe(hass, SUPPLY_PROBE, supply)
        # Keep the pair plausible so only the sentinel rule is under test.
        set_probe(hass, RETURN_PROBE, supply - 7.0)
        await run_cycle(coordinator)
        assert coordinator.probes.supply.quality == expected, supply


# ---------------------------------------------------------------------------
# The delivery standard: a probe fault must not affect heating
# ---------------------------------------------------------------------------


async def test_probe_fault_changes_no_write_in_coordinate_mode(hass):
    """The headline V0.3 guarantee.

    Compare the writes produced with healthy probes against those produced with
    a wholly failed probe set: they must be identical, because probes are
    monitoring inputs and never gate control.
    """

    async def writes_with(supply_value, return_value):
        coordinator, _ = build_v03(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE})
        coordinator._started_at = T0 - timedelta(hours=1)
        set_probe(hass, SUPPLY_PROBE, supply_value)
        set_probe(hass, RETURN_PROBE, return_value)
        before = len(hass._climatepilot_calls)
        await run_cycle(coordinator)
        return hass._climatepilot_calls[before:]

    healthy = await writes_with(45.0, 38.0)
    sentinel = await writes_with(DS18B20_ERROR_C, DS18B20_ERROR_C)
    dead = await writes_with(None, None)

    assert healthy, "coordinate mode should write something"
    assert sentinel == healthy, "a sentinel probe must not alter control"
    assert dead == healthy, "a dead probe must not alter control"


async def test_probe_fault_does_not_write_in_observe_mode(hass):
    coordinator, _ = build_v03(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE})
    set_probe(hass, SUPPLY_PROBE, DS18B20_ERROR_C)
    set_probe(hass, RETURN_PROBE, None)
    for offset in range(4):
        await run_cycle(coordinator, T0 + timedelta(minutes=offset * 10))

    assert calls_to(hass, "climate") == []
    assert coordinator.probe_fault is True


async def test_probe_fault_does_not_disturb_the_heat_state(hass):
    """The FSM must reach the same state with or without usable probes."""
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator, T0)
    healthy_state = coordinator.heat_state

    set_probe(hass, SUPPLY_PROBE, DS18B20_ERROR_C)
    await run_cycle(coordinator, T0 + timedelta(minutes=5))
    assert coordinator.heat_state == healthy_state


# ---------------------------------------------------------------------------
# The delta-T engine only sees values that survived the checks
# ---------------------------------------------------------------------------


async def test_delta_t_engine_never_receives_a_rejected_value(hass):
    """Rejected readings must not reach the trend engine as numbers."""
    coordinator, _ = build_v03(hass)

    seen: list[tuple[float | None, float | None]] = []
    real_note = coordinator.delta_t_engine.note

    def spy_note(*, supply, return_, burning, active_zones, now, **kwargs):
        seen.append((supply, return_))
        return real_note(
            supply=supply,
            return_=return_,
            burning=burning,
            active_zones=active_zones,
            now=now,
            **kwargs,
        )

    coordinator.delta_t_engine.note = spy_note

    set_probe(hass, SUPPLY_PROBE, DS18B20_ERROR_C)
    set_probe(hass, RETURN_PROBE, 40.0)
    await run_cycle(coordinator, T0)

    assert seen, "the engine must have been fed"
    supply_value, return_value = seen[-1]
    assert supply_value is None, "the sentinel value must be filtered out"
    assert return_value == 40.0


async def test_probe_divergence_downgrades_both_sides(hass):
    """Two wildly disagreeing probes: neither is trusted."""
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 70.0)
    set_probe(hass, RETURN_PROBE, 20.0)
    await run_cycle(coordinator)

    assert coordinator.probes.supply.quality == PROBE_IMPLAUSIBLE
    assert coordinator.probes.return_.quality == PROBE_IMPLAUSIBLE
    assert "probe_pair_divergent" in coordinator.probes.reason_codes
    assert coordinator.delta_t.delta_t is None


async def test_healthy_delta_t_survives_the_probe_layer(hass):
    """Regression: a textbook-healthy delta-T must reach the report.

    The supply and return probes sit on the *same* hydraulic loop, so their
    difference **is** the delta-T the diagnostics exist to measure. An earlier
    version of ``DEFAULT_PROBE_MAX_DIVERGENCE`` (5.0 C) sat at or below the
    healthy delta-T range (5-15 C), so the probe layer classified every
    well-performing system as "divergent", handed the trend engine two ``None``
    values, and the whole V0.3 feature silently reported nothing.

    This walks the healthy range end to end through the real coordinator.
    """
    for supply, return_, expected in (
        (45.0, 43.0, 2.0),  # low but real (flow/valve position)
        (45.0, 38.0, 7.0),  # textbook healthy
        (52.0, 38.0, 14.0),  # near the healthy upper bound
        (53.0, 38.0, 15.0),  # the healthy upper bound itself
    ):
        coordinator, _ = build_v03(hass)
        set_probe(hass, SUPPLY_PROBE, supply)
        set_probe(hass, RETURN_PROBE, return_)
        await run_cycle(coordinator)

        assert coordinator.probes.both_usable is True, (supply, return_)
        assert coordinator.probes.divergence == pytest.approx(expected, abs=0.01)
        assert coordinator.delta_t.delta_t == pytest.approx(expected, abs=0.01), (
            f"a healthy {expected} C delta-T must reach the report"
        )


async def test_implausible_pairing_is_still_rejected(hass):
    """Widening the threshold must not stop it catching a genuinely wrong pair."""
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 75.0)  # one probe on a hot pipe
    set_probe(hass, RETURN_PROBE, 15.0)  # the other on a cold one
    await run_cycle(coordinator)

    assert coordinator.probes.both_usable is False
    assert "probe_pair_divergent" in coordinator.probes.reason_codes
    assert coordinator.delta_t.delta_t is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_state_payload_includes_the_v03_history(hass):
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator)

    payload = coordinator._state_payload()
    assert "probes" in payload
    assert "delta_t" in payload

    import json

    json.dumps(payload)


async def test_v03_history_survives_a_restart(hass):
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    for offset in range(4):
        await run_cycle(coordinator, T0 + timedelta(minutes=offset * 2))
    saved = coordinator._state_payload()

    fresh, _ = build_v03(hass)
    fresh.probe_monitor.restore(saved["probes"])
    fresh.delta_t_engine.restore(saved["delta_t"])

    assert fresh.probe_monitor.snapshot() == saved["probes"]
    assert fresh.delta_t_engine.snapshot() == saved["delta_t"]


async def test_restore_tolerates_a_v02_payload(hass):
    """Upgrading an existing installation must not crash on older saved state."""
    coordinator, _ = build_v03(hass)
    v02_payload = {
        "zones": {},
        "aux_load": {},
        "zone_coordinator": {},
        "heat_source": {},
        "weather": {},
        "burner": {},
        "combinations": {},
        "comfort": {},
        "control_mode": CONTROL_MODE_OBSERVE,
    }

    with patch.object(coordinator.store, "async_load", AsyncMock(return_value=v02_payload)):
        await coordinator._async_restore_state()

    await run_cycle(coordinator, T0)
    assert coordinator.probes is not None


# ---------------------------------------------------------------------------
# Configuration edges
# ---------------------------------------------------------------------------


async def test_probe_staleness_is_reported(hass):
    """A probe that stopped reporting must be flagged, not silently reused."""
    coordinator, _ = build_v03(hass, **{CONF_PROBE_STALE_SECONDS: 60})
    set_probe(hass, SUPPLY_PROBE, 45.0, when=T0 - timedelta(hours=1))
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator, T0)

    assert coordinator.probes.supply.quality != PROBE_VALID
    assert coordinator.probe_fault is True


async def test_inverted_delta_t_healthy_range_does_not_break_a_cycle(hass):
    """A mis-configured range must degrade the verdict, not raise."""
    coordinator, _ = build_v03(
        hass,
        **{CONF_DELTA_T_HEALTHY_MIN: 15.0, CONF_DELTA_T_HEALTHY_MAX: 5.0},
    )
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator)

    assert coordinator.delta_t is not None
    assert coordinator.delta_t.delta_t == pytest.approx(7.0, abs=0.01)


async def test_probes_are_read_each_cycle(hass):
    """The reported value must track the entity, not freeze at first read."""
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 40.0)
    set_probe(hass, RETURN_PROBE, 35.0)
    await run_cycle(coordinator, T0)
    assert coordinator.probes.supply.value == 40.0

    set_probe(hass, SUPPLY_PROBE, 50.0)
    set_probe(hass, RETURN_PROBE, 35.0)
    await run_cycle(coordinator, T0 + timedelta(minutes=5))
    assert coordinator.probes.supply.value == 50.0
    assert coordinator.delta_t.delta_t == pytest.approx(15.0, abs=0.01)


async def test_burner_state_reaches_the_probe_assessment(hass):
    """``frozen`` detection needs to know whether the boiler is firing."""
    coordinator, _ = build_v03(hass)
    set_probe(hass, SUPPLY_PROBE, 45.0)
    set_probe(hass, RETURN_PROBE, 38.0)
    await run_cycle(coordinator)

    # The burner entity exists in the fixture; whatever its state, the cycle
    # must have consulted it rather than assuming "not burning".
    assert coordinator.diagnostics_snapshot.get("burner") is not None
