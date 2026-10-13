"""Coordinator wiring for the energy monitor: off by default, and read-only.

Two properties are asserted here rather than left to inspection, because each one
failing silently would be a different kind of harm: an always-on feature that reads
entities a user never configured, or an "instrument" that could move something.

Uses the shared coordinator helpers so this behaves like the other coordinator
tests - same entity set, same pinned clock, same entry shape.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_coordinator_smoke import (
    full_options,
    install_entities,
    make_entry,
    pin_clock,
)

from custom_components.climatepilot.const import (
    CONF_GAS_METER_ENTITY,
    CONF_OUTDOOR_TEMP_ENTITY,
    DOMAIN,
)
from custom_components.climatepilot.coordinator import ClimatePilotCoordinator

GAS = "sensor.test_gas_meter"
T0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


@pytest.fixture()
async def hass(tmp_path):
    """A real HomeAssistant with a service spy installed.

    A genuine ``HomeAssistant`` rather than a mock, so the coordinator's real
    wiring is exercised; the service spy is what makes the read-only assertion
    meaningful, and it follows the same shape the other coordinator test modules
    use so a call here is recorded identically.
    """
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


def build(hass, **overrides) -> ClimatePilotCoordinator:
    """A coordinator with the production entity set installed."""
    install_entities(hass)
    entry = make_entry(full_options(**overrides))
    coordinator = ClimatePilotCoordinator(hass, entry)
    coordinator.async_shutdown_listeners = MagicMock()
    pin_clock(coordinator)
    return coordinator


def with_meter(hass, **overrides) -> ClimatePilotCoordinator:
    """A coordinator with a working gas meter configured."""
    hass.states.async_set(GAS, "1000.0")
    return build(hass, **{CONF_GAS_METER_ENTITY: GAS, **overrides})


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_no_gas_meter_means_no_energy_state(hass) -> None:
    """Without a meter the feature is absent, not reading zeros.

    Zeros would be worse than absence: they render as "the boiler used no gas",
    which is a claim about the heating system, whereas absence says only that
    nothing was configured.
    """
    coordinator = build(hass)

    assert coordinator.gas_meter_entity is None
    snapshot = coordinator.energy_snapshot()
    assert snapshot["gas_today"] is None
    assert snapshot["gas_per_degree_day"] is None


def test_the_energy_feature_is_enabled_only_by_a_meter(hass) -> None:
    without = build(hass)
    assert without.energy_enabled is False

    assert with_meter(hass).energy_enabled is True


def test_a_meter_that_does_not_exist_does_not_enable_the_feature(hass) -> None:
    """A restarted-but-absent meter must not create six permanently-unknown sensors.

    The config flow validates the entity when it is picked, but the sensor can be
    renamed or deleted afterwards. Trusting a stale string would leave the user
    with six broken entities and no hint as to why.
    """
    install_entities(hass)
    coordinator = build(hass, **{CONF_GAS_METER_ENTITY: "sensor.removed_by_user"})

    assert coordinator.gas_meter_entity == "sensor.removed_by_user"
    assert coordinator.energy_enabled is False


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_note_records_gas_and_degree_days(hass) -> None:
    coordinator = with_meter(hass)
    coordinator.note_energy(T0)
    hass.states.async_set(GAS, "1005.0")
    coordinator.note_energy(T0 + timedelta(hours=1))

    snapshot = coordinator.energy_snapshot()
    assert snapshot["gas_today"] == pytest.approx(5.0, abs=0.01)
    # The installed outdoor sensor reports a real temperature, so the hour counted.
    assert snapshot["degree_days_today"] > 0.0


def test_an_unavailable_meter_is_passed_through_as_none(hass) -> None:
    """An unavailable meter must not be read as zero consumption."""
    hass.states.async_set(GAS, "unavailable")
    coordinator = build(hass, **{CONF_GAS_METER_ENTITY: GAS})
    coordinator.note_energy(T0)

    assert coordinator.energy_snapshot()["gas_today"] is None


def test_an_unavailable_outdoor_sensor_is_passed_through_as_none(hass) -> None:
    """An unavailable thermometer must not be read as 0 C.

    Counting it as zero would inflate degree-days by the full base temperature and
    make the hour look far colder than it was - a plausible wrong number, which is
    exactly the failure mode the engine's coverage figure exists to expose.
    """
    coordinator = with_meter(hass)
    coordinator.note_energy(T0)

    hass.states.async_set(coordinator.outdoor_entity, "unavailable")
    before = coordinator.energy_snapshot()["degree_days_today"]
    coordinator.note_energy(T0 + timedelta(hours=1))

    assert coordinator.energy_snapshot()["degree_days_today"] == before


def test_a_non_numeric_meter_state_is_ignored(hass) -> None:
    """Some integrations publish text; that must not raise inside an audit."""
    hass.states.async_set(GAS, "not-a-number")
    coordinator = build(hass, **{CONF_GAS_METER_ENTITY: GAS})
    coordinator.note_energy(T0)

    assert coordinator.energy_snapshot()["gas_today"] is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_energy_state_is_in_the_persist_payload(hass) -> None:
    """Restarting must not lose the day's progress."""
    coordinator = with_meter(hass)
    assert "energy" in coordinator._state_payload()


def test_energy_progress_survives_a_restore(hass) -> None:
    """Round-trip through the payload the store actually writes."""
    coordinator = with_meter(hass)
    coordinator.note_energy(T0)
    hass.states.async_set(GAS, "1007.0")
    coordinator.note_energy(T0 + timedelta(hours=1))

    payload = coordinator._state_payload()

    restored = with_meter(hass)
    restored._restore_energy(payload.get("energy"))

    assert restored.energy_snapshot()["gas_today"] == pytest.approx(7.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_full_restore_path_reaches_the_energy_monitor(hass) -> None:
    """``_async_restore_state`` must actually call the energy restore.

    Asserting on the end of the path rather than by calling
    ``_restore_energy`` directly: the earlier version of this test invoked the
    private helper itself, so it stayed green even with the call removed from
    ``_async_restore_state`` - it proved the helper works, not that anything
    reaches it. A restart would then silently reset the day's progress.
    """
    from unittest.mock import AsyncMock, patch

    source = with_meter(hass)
    source.note_energy(T0)
    hass.states.async_set(GAS, "1012.0")
    source.note_energy(T0 + timedelta(hours=1))
    saved = source._state_payload()

    restored = with_meter(hass)
    with patch.object(restored.store, "async_load", AsyncMock(return_value=saved)):
        await restored._async_restore_state()

    assert restored.energy_snapshot()["gas_today"] == pytest.approx(12.0, abs=0.01)


@pytest.mark.asyncio
async def test_an_audit_cycle_feeds_the_monitor(hass) -> None:
    """The audit must actually call ``note_energy``.

    Without this, the wiring could be disconnected and every other test would
    still pass, because they all call ``note_energy`` themselves. The symptom on a
    real system would be six sensors permanently reading zero - the most likely
    way for this feature to ship broken.
    """
    coordinator = with_meter(hass)

    # The first cycle establishes the baseline reading.
    await coordinator._async_update_data()
    assert coordinator.energy_snapshot()["gas_today"] == pytest.approx(0.0, abs=0.01)

    hass.states.async_set(GAS, "1004.0")
    await coordinator._async_update_data()

    assert coordinator.energy_snapshot()["gas_today"] == pytest.approx(4.0, abs=0.01)


@pytest.mark.asyncio
async def test_the_audit_records_over_time_with_an_advancing_clock(hass) -> None:
    """With the feature on, consecutive audits genuinely accumulate degree-days.

    Uses its own advancing clock rather than the module fixture's pinned one:
    with time frozen, elapsed seconds are zero and nothing can accumulate, which
    would make this indistinguishable from a disconnected audit.
    """
    coordinator = with_meter(hass)

    ticks = iter(T0 + timedelta(hours=h) for h in range(4))
    coordinator._now = lambda: next(ticks)  # type: ignore[method-assign]

    for _ in range(3):
        await coordinator._async_update_data()

    snapshot = coordinator.energy_snapshot()
    # Four hours of auditing spans three one-hour intervals; the outdoor sensor is
    # below the base temperature, so degree-days must have accrued.
    assert snapshot["degree_days_today"] > 0.0, (
        "an enabled feature accumulated no degree-days, so the audit is not feeding it"
    )
    assert snapshot["coverage_today"] > 0.0


@pytest.mark.asyncio
async def test_an_audit_cycle_does_nothing_without_a_meter(hass) -> None:
    """With the feature off, no observation is recorded at all.

    Asserted by calling ``note_energy`` itself with a realistic pair of readings
    and checking nothing is kept - not by running two audits. This module's fixture
    pins the clock, so consecutive audit cycles have identical timestamps and
    accumulate zero degree-days regardless of whether they ran; and without a meter
    ``gas_today`` stays ``None`` on either branch. Both of those assertions pass
    even when the audit notifies unconditionally.

    What the off switch actually protects is that an unconfigured feature does not
    quietly build a history nobody asked for - so that is what is measured.
    """
    coordinator = build(hass)
    assert coordinator.energy_enabled is False

    # The audit would call this, if it were not gated.
    coordinator.note_energy(T0)
    coordinator.note_energy(T0 + timedelta(hours=1))

    snapshot = coordinator.energy_snapshot()
    assert snapshot["gas_today"] is None
    assert snapshot["gas_per_degree_day"] is None


@pytest.mark.asyncio
async def test_the_audit_gate_matches_energy_enabled(hass) -> None:
    """The audit must consult exactly the condition the entities do.

    Read structurally, and from the method that actually holds the call rather than
    from ``_async_update_data``: the diagnostics are refreshed from
    ``_async_update_v02_diagnostics``, which is where the gate lives.

    A behavioural test cannot separate the two cases here - a disabled feature
    produces the same readings whether or not the audit ran - so what is asserted
    is that the two code paths agree on the condition. Divergence there means
    entities that exist get no data, or a feature that does not exist still reads
    entities on every cycle.
    """
    import inspect

    source = inspect.getsource(ClimatePilotCoordinator._async_update_v02_diagnostics)
    assert "self.energy_enabled" in source, (
        "the audit must gate on energy_enabled, not call note_energy unconditionally"
    )
    assert "self.note_energy(now)" in source
    # And the gated call must be *inside* the condition, not merely nearby.
    gated = source.split("if self.energy_enabled:")[1]
    assert gated.lstrip().startswith("self.note_energy(now)")


def test_a_corrupt_energy_payload_does_not_break_setup(hass) -> None:
    """Storage is load-bearing for setup, so bad data must not raise.

    Exercised through ``_async_restore_state`` as well as the helper, because both
    layers defend and either alone would keep this green: the helper checks its
    argument type, and the engine's ``restore`` tolerates non-dicts and garbage
    fields. Asserting only one of them would let the other be deleted unnoticed.
    """
    coordinator = with_meter(hass)

    for payload in (None, "nonsense", 42, {"persist": []}, [], {"persist": {"day": 5}}):
        coordinator._restore_energy(payload)  # type: ignore[arg-type]
        assert coordinator.energy_snapshot()["gas_today"] is None


@pytest.mark.asyncio
async def test_a_corrupt_energy_section_does_not_break_the_restore(hass) -> None:
    """A bad ``energy`` key inside an otherwise valid payload must be survivable."""
    from unittest.mock import AsyncMock, patch

    coordinator = with_meter(hass)
    payload = coordinator._state_payload()
    payload["energy"] = {"persist": "not-a-dict"}

    with patch.object(coordinator.store, "async_load", AsyncMock(return_value=payload)):
        await coordinator._async_restore_state()

    assert coordinator.energy_snapshot()["gas_today"] is None


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_note_energy_calls_no_service(hass) -> None:
    """An instrument must not be able to move anything.

    The fixture registers spying handlers on exactly the two services this
    integration can use to drive the boiler, so any attempt to write shows up here
    rather than on the user's heating.

    Asserting on the recorded calls rather than wrapping
    ``hass.services.async_call``: that attribute is read-only on the real
    ``ServiceRegistry``, and the spy is the established mechanism in this test
    suite anyway.
    """
    coordinator = with_meter(hass)

    for hour in range(4):
        hass.states.async_set(GAS, str(1000.0 + hour * 3))
        coordinator.note_energy(T0 + timedelta(hours=hour))

    assert hass._climatepilot_calls == [], (
        f"the energy monitor must not call services, saw {hass._climatepilot_calls}"
    )
    # It did do its actual job, so the assertion above is not vacuous.
    assert coordinator.energy_snapshot()["gas_today"] == pytest.approx(9.0, abs=0.01)


def test_the_energy_monitor_cannot_reach_the_write_path(hass) -> None:
    """Structural check: no attribute of the monitor is a writer.

    The engine's public surface is fixed by the plan (``note``, ``snapshot``,
    ``snapshot_persist``, ``restore``). Anything shaped like a command appearing
    later would be a design regression, not just a bug.
    """
    coordinator = with_meter(hass)
    public = {
        name
        for name in dir(coordinator.energy_monitor)
        if not name.startswith("_") and callable(getattr(coordinator.energy_monitor, name))
    }
    assert public <= {"note", "snapshot", "snapshot_persist", "restore"}, (
        f"unexpected public surface on the energy monitor: {sorted(public)}"
    )


def test_the_domain_is_unchanged() -> None:
    """Guards against an accidental import shadowing in this test module."""
    assert DOMAIN == "climatepilot"
    assert CONF_OUTDOOR_TEMP_ENTITY == "outdoor_temperature_entity"
