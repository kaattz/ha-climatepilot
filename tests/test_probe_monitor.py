"""Unit tests for the pure pipe-probe quality engine (V0.3, design doc 8).

These tests run without Home Assistant: ``probe_monitor`` is a pure engine, and
several tests assert that property directly.

Testing philosophy
------------------

The contract's thresholds are written here as **literals** (85.0, 0.5, 900.0,
3600.0, ...) rather than imported from ``const``, so that a drifted constant
fails a test instead of silently redefining what "correct" means. A separate
test pins the constants themselves to the documented values. Reading a
constant to build an expectation, then asserting the code returns that
constant, proves only that the code can read - not that the number is right.

The two failure modes these tests exist to prevent are stated in the engine's
own docstring: an 85 C DS18B20 sentinel presented as a hot supply pipe, and a
frozen verdict reported on an idle, cooling pipe.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Defensive isolation: see the note in tests/test_heat_source.py. The engine
# itself must not import HA (asserted below); this guard only keeps the *test
# module* importable when the package ``__init__.py`` regresses.
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

from custom_components.climatepilot import probe_monitor as pm
from custom_components.climatepilot.probe_monitor import (
    ProbeAssessment,
    ProbeMonitor,
    ProbePairAssessment,
    ProbeReading,
)

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
MODULE_PATH = Path(pm.__file__)

SUPPLY = "sensor.boiler_supply_pipe_temperature"
RETURN = "sensor.boiler_return_pipe_temperature"


def reading(
    value: float | None,
    at: datetime | None = T0,
    entity_id: str = SUPPLY,
    raw: str | None = None,
) -> ProbeReading:
    """Build a reading whose age relative to ``T0`` is zero."""
    return ProbeReading(entity_id=entity_id, value=value, updated_at=at, raw=raw)


def monitor(**kwargs) -> ProbeMonitor:
    """A monitor with the shipped defaults (frozen at the default if needed)."""
    return ProbeMonitor(**kwargs)


def unchanged_for(
    minutes: float,
    *,
    value: float = 40.0,
    burning: bool,
    entity_id: str = SUPPLY,
    mon: ProbeMonitor | None = None,
) -> ProbeAssessment:
    """Assess the *same* value twice, ``minutes`` apart, with fresh timestamps.

    ``updated_at`` tracks the later moment so the reading is not stale: a probe
    that keeps reporting the identical value is exactly the case rule 5 exists
    for, and rule 4 must not mask it.
    """
    mon = mon or monitor()
    mon.assess(reading(value, T0, entity_id), T0, burning=burning)
    later = T0 + timedelta(minutes=minutes)
    return mon.assess(reading(value, later, entity_id), later, burning=burning)


# ---------------------------------------------------------------------------
# Module purity / clock discipline
# ---------------------------------------------------------------------------
def test_module_does_not_import_homeassistant() -> None:
    """The engine must stay importable without Home Assistant."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.split(".")[0] == "homeassistant"]
    assert offenders == [], f"homeassistant imported: {offenders}"


def test_module_only_depends_on_the_standard_library_and_const() -> None:
    """INTERFACES_V0.3 section 4.1: stdlib plus ``.const`` - no other engine.

    ``sys.stdlib_module_names`` is Python's own list, so this stays correct as
    the standard library changes and cannot drift with a hand-maintained copy.
    Importing a sibling engine would break the module's independent
    testability, which is the property the contract is protecting.
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    local: list[str] = []
    external: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            external.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import: must be a sibling module
                local.append(node.module or "")
            else:
                external.append((node.module or "").split(".")[0])

    assert external, "expected at least the stdlib imports"
    non_stdlib = sorted(set(external) - sys.stdlib_module_names)
    assert non_stdlib == [], f"non-stdlib imports: {non_stdlib}"
    # ``const`` is the only sibling module this engine may reach for.
    assert sorted(set(local)) == ["const"], f"sibling imports: {sorted(set(local))}"


@pytest.mark.parametrize("forbidden", ["now", "utcnow", "today"])
def test_engine_never_reads_the_wall_clock(forbidden: str) -> None:
    """All time must arrive through ``now``; no ambient clock access."""
    source = inspect.getsource(pm)
    assert f"datetime.{forbidden}(" not in source


def test_engine_never_calls_time_time() -> None:
    assert "time.time(" not in inspect.getsource(pm)


# ---------------------------------------------------------------------------
# Contract: the frozen section 4.1 signatures
# ---------------------------------------------------------------------------
def test_interface_signatures_match_the_contract() -> None:
    """Pin section 4.1 verbatim, including the ``return_`` underscore."""
    init = inspect.signature(ProbeMonitor.__init__).parameters
    expected_defaults = {
        "stale_after": 900.0,
        "frozen_after": 3600.0,
        "min_value": -20.0,
        "max_value": 110.0,
        "max_divergence": 25.0,
        "sentinel": 85.0,
        "sentinel_tolerance": 0.5,
    }
    for name in expected_defaults:
        assert name in init, f"__init__ lost parameter {name}"
        assert init[name].kind is inspect.Parameter.KEYWORD_ONLY
    # The signature's own literal defaults must match the contract's numbers.
    for name, default in expected_defaults.items():
        assert init[name].default == pytest.approx(default), name

    assess = inspect.signature(ProbeMonitor.assess).parameters
    assert list(assess) == ["self", "reading", "now", "burning"]
    assert assess["burning"].default is False
    assert assess["burning"].kind is inspect.Parameter.KEYWORD_ONLY

    pair = inspect.signature(ProbeMonitor.assess_pair).parameters
    assert list(pair) == ["self", "supply", "return_", "now", "burning"]
    assert pair["burning"].default is False
    assert pair["burning"].kind is inspect.Parameter.KEYWORD_ONLY

    assert list(inspect.signature(ProbeMonitor.snapshot).parameters) == ["self"]
    assert list(inspect.signature(ProbeMonitor.restore).parameters) == ["self", "data"]


def test_dataclass_shapes_match_the_contract() -> None:
    """Field names, order and optionality, exactly as section 4.1 writes them."""
    assert [f.name for f in dataclasses.fields(ProbeReading)] == [
        "entity_id",
        "value",
        "updated_at",
        "raw",
    ]
    assert [f.name for f in dataclasses.fields(ProbeAssessment)] == [
        "entity_id",
        "value",
        "quality",
        "reason_codes",
        "age_seconds",
        "frozen_seconds",
    ]
    assert [f.name for f in dataclasses.fields(ProbePairAssessment)] == [
        "supply",
        "return_",
        "divergence",
        "reason_codes",
    ]
    # ``raw`` is the only optional field on a reading; a missing timestamp is a
    # legitimate "cannot prove freshness" input rather than a construction error.
    assert ProbeReading(entity_id=SUPPLY, value=None).updated_at is None
    assert ProbeReading(entity_id=SUPPLY, value=None).raw is None


def test_contract_constants_have_the_documented_values() -> None:
    """Pin the frozen V0.3 numbers themselves.

    Deliberately reads the real constants: if one drifts, the *literal-based*
    boundary tests above keep their meaning and this test names the culprit.
    """
    from custom_components.climatepilot import const

    assert const.DS18B20_ERROR_C == 85.0
    assert const.DS18B20_ERROR_TOLERANCE == 0.5
    assert const.DEFAULT_PROBE_STALE_AFTER == 900.0
    assert const.DEFAULT_PROBE_FROZEN_AFTER == 3600.0
    assert const.DEFAULT_PROBE_MIN_C == -20.0
    assert const.DEFAULT_PROBE_MAX_C == 110.0
    # Guards against the regression where this was <= the healthy delta-T band
    # (5..15 C), which discarded every healthy 45/38 pair as "divergent".
    assert const.DEFAULT_PROBE_MAX_DIVERGENCE > const.DEFAULT_DELTA_T_HEALTHY_MAX


def test_assessments_are_frozen_dataclasses() -> None:
    result = monitor().assess(reading(45.0), T0)
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        result.value = 1.0  # type: ignore[misc]


def test_monitor_exposes_no_write_surface() -> None:
    """HARD CONSTRAINT: probes are monitoring inputs, never control outputs.

    The public API is exactly the four contracted operations. Nothing here can
    write, request, block or authorise a boiler action, which is what makes
    "a failed probe never changes a write" true by construction.
    """
    public = {name for name in dir(ProbeMonitor) if not name.startswith("_")}
    assert public == {"assess", "assess_pair", "snapshot", "restore"}
    for suspicious in ("turn_on", "turn_off", "set_", "write", "request", "apply"):
        assert not [name for name in public if name.startswith(suspicious)], suspicious


# ---------------------------------------------------------------------------
# Rule 1 - no value at all
# ---------------------------------------------------------------------------
def test_none_value_is_unavailable_with_the_missing_reason() -> None:
    result = monitor().assess(reading(None), T0)
    # Deliberately NOT "unavailable": HA reserves that string and would render
    # the entity as genuinely unavailable (greyed out), making "no probe is
    # reporting" look identical to "ClimatePilot is dead".
    assert result.quality == "probe_unavailable"
    assert result.reason_codes == ("probe_not_configured",)
    assert result.value is None
    assert result.usable is False


@pytest.mark.parametrize(
    "bad",
    [float("nan"), float("inf"), float("-inf"), "warm", "", object()],
)
def test_non_numeric_and_non_finite_values_are_unavailable(bad: object) -> None:
    """A reading that is not a finite number is not a temperature.

    ``NaN`` is the dangerous one: every comparison against a bound is False, so
    a naive range check would wave it through as plausible.
    """
    result = monitor().assess(reading(bad), T0)  # type: ignore[arg-type]
    assert result.quality == "probe_unavailable"
    assert result.value is None
    assert "probe_not_configured" in result.reason_codes


def test_unavailable_never_carries_a_value() -> None:
    for probe in (None, float("nan")):
        assert monitor().assess(reading(probe), T0).value is None


def test_probe_quality_strings_avoid_the_ha_reserved_state() -> None:
    """``PROBE_UNAVAILABLE`` must not be literally "unavailable".

    Home Assistant reserves that string: an entity publishing it is rendered as
    genuinely unavailable (greyed out), indistinguishable from a dead
    integration. The whole point of this quality is to say "no probe is
    reporting" while the integration itself is perfectly healthy.
    """
    from custom_components.climatepilot import const

    assert const.PROBE_UNAVAILABLE != "unavailable"
    assert const.PROBE_UNAVAILABLE == "probe_unavailable"
    # The other four are free to match HA's vocabulary, and do.
    assert const.PROBE_VALID == "valid"
    assert const.PROBE_STALE == "stale"
    assert const.PROBE_IMPLAUSIBLE == "implausible"
    assert const.PROBE_FROZEN == "frozen"
    assert len(const.PROBE_QUALITIES) == 5
    assert const.PROBE_UNAVAILABLE in const.PROBE_QUALITIES


# ---------------------------------------------------------------------------
# Rule 2 - the DS18B20 85 C sentinel (the most important check)
# ---------------------------------------------------------------------------
def test_exactly_85_degrees_is_the_ds18b20_sentinel() -> None:
    """85.0 means "conversion failed", never "very hot supply pipe"."""
    result = monitor().assess(reading(85.0), T0)
    assert result.quality == "implausible"
    assert result.reason_codes == ("probe_sentinel_value_85c",)
    assert result.value is None, "a sentinel reading must not be readable as a temperature"


@pytest.mark.parametrize("value", [85.0, 84.5, 85.5])
def test_sentinel_window_is_inclusive_on_both_sides(value: float) -> None:
    """abs(value - 85.0) <= 0.5, so the edges are inside the window."""
    result = monitor().assess(reading(value), T0)
    assert result.quality == "implausible"
    assert "probe_sentinel_value_85c" in result.reason_codes


@pytest.mark.parametrize("value", [84.4, 84.0, 85.6, 86.0])
def test_just_outside_the_sentinel_window_is_a_real_reading(value: float) -> None:
    """A genuinely hot pipe near 85 C must not be discarded as a sentinel."""
    result = monitor().assess(reading(value), T0)
    assert result.quality == "valid"
    assert result.reason_codes == ()
    assert result.value == pytest.approx(value)


def test_sentinel_window_scales_with_a_custom_tolerance() -> None:
    """The tolerance is a parameter, not a hardcoded 0.5."""
    strict = monitor(sentinel=85.0, sentinel_tolerance=0.1)
    assert strict.assess(reading(85.0), T0).quality == "implausible"
    assert strict.assess(reading(84.8), T0).quality == "valid"


def test_sentinel_is_checked_before_the_range_check() -> None:
    """Rule 2 precedes rule 3, so 85 reports *why* it is refused.

    With a narrowed plausible window, 85.0 is also out of range - but the
    honest explanation is the sentinel, and that is what a user needs to see.
    """
    narrowed = monitor(max_value=80.0)
    result = narrowed.assess(reading(85.0), T0)
    assert result.reason_codes == ("probe_sentinel_value_85c",)


def test_sentinel_is_checked_before_staleness() -> None:
    """Rule 2 precedes rule 4: a stale sentinel is still the sentinel."""
    result = monitor().assess(reading(85.0, T0 - timedelta(days=1)), T0)
    assert result.reason_codes == ("probe_sentinel_value_85c",)
    assert result.quality == "implausible"


def test_sentinel_is_not_usable_even_when_the_age_is_perfect() -> None:
    fresh = monitor().assess(reading(85.0, T0), T0)
    assert fresh.age_seconds == pytest.approx(0.0)
    assert fresh.usable is False


# ---------------------------------------------------------------------------
# Rule 3 - plausible range
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [-20.0001, -20.5, -100.0])
def test_below_the_minimum_is_out_of_range(value: float) -> None:
    result = monitor().assess(reading(value), T0)
    assert result.quality == "implausible"
    assert result.reason_codes == ("probe_out_of_range",)
    assert result.value is None


@pytest.mark.parametrize("value", [110.0001, 110.5, 200.0])
def test_above_the_maximum_is_out_of_range(value: float) -> None:
    result = monitor().assess(reading(value), T0)
    assert result.quality == "implausible"
    assert result.reason_codes == ("probe_out_of_range",)
    assert result.value is None


@pytest.mark.parametrize("value", [-20.0, 110.0])
def test_range_bounds_are_inclusive(value: float) -> None:
    """Rule 3 is ``< min`` / ``> max``; the bounds themselves are plausible."""
    result = monitor().assess(reading(value), T0)
    assert result.quality == "valid"
    assert result.value == pytest.approx(value)


def test_out_of_range_is_checked_before_staleness() -> None:
    """Rule 3 precedes rule 4."""
    result = monitor().assess(reading(200.0, T0 - timedelta(days=1)), T0)
    assert result.reason_codes == ("probe_out_of_range",)


# ---------------------------------------------------------------------------
# Rule 4 - staleness
# ---------------------------------------------------------------------------
def test_age_exactly_at_the_limit_is_still_valid() -> None:
    """Rule 4 is ``age > stale_after``: the boundary itself is still trusted."""
    result = monitor().assess(reading(45.0, T0 - timedelta(seconds=900.0)), T0)
    assert result.quality == "valid"
    assert result.age_seconds == pytest.approx(900.0)


def test_just_over_the_limit_is_stale() -> None:
    result = monitor().assess(reading(45.0, T0 - timedelta(seconds=900.1)), T0)
    assert result.quality == "stale"
    assert result.reason_codes == ("probe_reading_stale",)
    assert result.value is None


def test_stale_never_carries_a_value() -> None:
    """A stale number must not be readable as the current temperature."""
    result = monitor().assess(reading(45.0, T0 - timedelta(hours=5)), T0)
    assert result.value is None
    assert result.usable is False


def test_missing_timestamp_is_stale_not_valid() -> None:
    """Without a timestamp freshness cannot be proven, so we fail safe."""
    result = monitor().assess(ProbeReading(entity_id=SUPPLY, value=45.0), T0)
    assert result.quality == "stale"
    assert result.reason_codes == ("probe_reading_stale",)
    assert result.age_seconds is None


def test_naive_timestamp_against_an_aware_clock_is_stale_not_a_crash() -> None:
    """A tz-aware/naive mix is unknowable freshness, not an exception."""
    naive = datetime(2026, 1, 15, 8, 0)  # no tzinfo, while ``now`` is UTC
    result = monitor().assess(ProbeReading(entity_id=SUPPLY, value=45.0, updated_at=naive), T0)
    assert result.quality == "stale"
    assert result.age_seconds is None


def test_future_timestamp_is_fresh_and_reports_negative_age() -> None:
    """Clock skew must not fake staleness; the skew stays visible instead."""
    result = monitor().assess(reading(45.0, T0 + timedelta(minutes=5)), T0)
    assert result.quality == "valid"
    assert result.age_seconds == pytest.approx(-300.0)


def test_stale_is_checked_before_frozen() -> None:
    """Rule 4 precedes rule 5: a probe that went quiet is stale, not frozen."""
    stale_at = T0 - timedelta(hours=5)
    result = monitor().assess(reading(40.0, stale_at), T0, burning=True)
    assert result.quality == "stale"


def test_stale_after_scales_with_the_parameter() -> None:
    short = monitor(stale_after=60.0)
    assert short.assess(reading(45.0, T0 - timedelta(seconds=61)), T0).quality == "stale"
    assert short.assess(reading(45.0, T0 - timedelta(seconds=60)), T0).quality == "valid"


# ---------------------------------------------------------------------------
# Rule 5 - frozen, and only while burning
# ---------------------------------------------------------------------------
def test_unchanged_value_while_not_burning_is_valid() -> None:
    """A cooling, idle pipe legitimately holds one value for hours.

    Reporting frozen here would be a false alarm on every quiet night.
    """
    result = unchanged_for(300, burning=False)
    assert result.quality == "valid"
    assert result.frozen_seconds == pytest.approx(300 * 60.0)


def test_unchanged_value_while_burning_is_frozen() -> None:
    result = unchanged_for(200, burning=True)
    assert result.quality == "frozen"
    assert result.reason_codes == ("probe_value_frozen",)
    assert result.value is None


def test_frozen_boundary_exactly_at_the_limit_is_valid() -> None:
    """Rule 5 is ``frozen_seconds > frozen_after``; 3600s exactly is not yet."""
    result = unchanged_for(60, burning=True)
    assert result.frozen_seconds == pytest.approx(3600.0)
    assert result.quality == "valid"


def test_frozen_boundary_just_over_the_limit_is_frozen() -> None:
    result = unchanged_for(60.01, burning=True)
    assert result.frozen_seconds > 3600.0
    assert result.quality == "frozen"


def test_frozen_recovery_when_the_value_moves() -> None:
    """A probe that starts moving again is trusted again."""
    mon = monitor()
    mon.assess(reading(40.0, T0), T0, burning=True)
    later = T0 + timedelta(hours=2)
    frozen = mon.assess(reading(40.0, later), later, burning=True)
    assert frozen.quality == "frozen"

    moved = later + timedelta(minutes=5)
    recovered = mon.assess(reading(42.0, moved), moved, burning=True)
    assert recovered.quality == "valid"
    assert recovered.frozen_seconds == pytest.approx(0.0)


def test_the_frozen_clock_restarts_at_the_change_not_at_first_sighting() -> None:
    """A value that just moved must not inherit the *previous* value's clock.

    The bug this pins: if the "last changed" timestamp is only ever set on the
    first sighting (a ``setdefault``-style mistake), then a probe that has been
    moving reads as unchanged for the entire elapsed span. The false ``frozen``
    verdict then appears one call *after* the change, which is exactly where a
    recovery check is not looking.
    """
    mon = monitor()
    mon.assess(reading(40.0, T0), T0, burning=True)

    # Two hours later the value genuinely moves: the clock restarts here.
    moved_at = T0 + timedelta(hours=2)
    mon.assess(reading(42.0, moved_at), moved_at, burning=True)

    # Only a minute of stillness later, it must NOT be frozen.
    soon = moved_at + timedelta(minutes=1)
    result = mon.assess(reading(42.0, soon), soon, burning=True)
    assert result.frozen_seconds == pytest.approx(60.0)
    assert result.quality == "valid", "a value held for 1 minute is not frozen"

    # And the clock keeps counting from the change, so it fires at the real limit.
    just_over = moved_at + timedelta(seconds=3601)
    fired = mon.assess(reading(42.0, just_over), just_over, burning=True)
    assert fired.frozen_seconds == pytest.approx(3601.0)
    assert fired.quality == "frozen"


def test_frozen_clock_starts_while_idle_and_fires_once_burning() -> None:
    """The unchanged span is tracked regardless of ``burning``.

    Only the *verdict* is gated. A pipe that has held one value all afternoon
    is already suspect the moment the boiler fires, so the span is not reset
    when ``burning`` flips to True.
    """
    mon = monitor()
    mon.assess(reading(40.0, T0), T0, burning=False)
    later = T0 + timedelta(hours=3)
    result = mon.assess(reading(40.0, later), later, burning=True)
    assert result.quality == "frozen"
    assert result.frozen_seconds == pytest.approx(3 * 3600.0)


def test_unavailable_reading_breaks_the_frozen_baseline() -> None:
    """A dropout means the value was not observed, so it was not "unchanged"."""
    mon = monitor()
    mon.assess(reading(40.0, T0), T0, burning=True)
    gap = T0 + timedelta(minutes=30)
    mon.assess(reading(None, gap), gap, burning=True)

    back = gap + timedelta(minutes=1)
    result = mon.assess(reading(40.0, back), back, burning=True)
    assert result.quality == "valid"
    assert result.frozen_seconds == pytest.approx(0.0)


def test_sentinel_recovery_resets_the_frozen_clock() -> None:
    """85 C then a real value is a *change*, so it must not read as frozen."""
    mon = monitor()
    mon.assess(reading(85.0, T0), T0, burning=True)
    later = T0 + timedelta(hours=2)
    result = mon.assess(reading(45.0, later), later, burning=True)
    assert result.quality == "valid"
    assert result.frozen_seconds == pytest.approx(0.0)


def test_slowly_moving_probe_never_reads_as_frozen() -> None:
    """A drifting pipe during a long burn is normal and must stay valid.

    Each call changes the value, so the frozen clock restarts every time even
    though hours pass overall. A stale-clock bug turns this into a false
    ``frozen`` alarm midway through the burn.
    """
    mon = monitor()
    for step in range(12):  # 2 hours of burning, a 0.5 C drift every 10 minutes
        at = T0 + timedelta(minutes=step * 10)
        value = 40.0 + step * 0.5
        result = mon.assess(reading(value, at), at, burning=True)
        assert result.quality == "valid", (step, value)
        assert result.frozen_seconds == pytest.approx(0.0)


def test_frozen_after_scales_with_the_parameter() -> None:
    quick = monitor(frozen_after=60.0)
    assert unchanged_for(2, burning=True, mon=quick).quality == "frozen"
    assert unchanged_for(1, burning=True, mon=quick).quality == "valid"


def test_assess_is_idempotent_for_the_same_inputs() -> None:
    mon = monitor()
    first = mon.assess(reading(45.0), T0, burning=False)
    second = mon.assess(reading(45.0), T0, burning=False)
    assert first == second


def test_reading_without_an_entity_id_cannot_be_frozen() -> None:
    """Identity is what makes "the same probe as last time" checkable."""
    mon = monitor()
    mon.assess(ProbeReading(entity_id="", value=40.0, updated_at=T0), T0, burning=True)
    later = T0 + timedelta(hours=3)
    result = mon.assess(
        ProbeReading(entity_id="", value=40.0, updated_at=later), later, burning=True
    )
    assert result.frozen_seconds is None
    assert result.quality == "valid"


# ---------------------------------------------------------------------------
# assess_pair - divergence, and the refusal to invent one
# ---------------------------------------------------------------------------
def test_healthy_delta_t_seven_is_usable() -> None:
    """Regression: a textbook floor-heating pair must not be refused.

    Supply 45 / return 38 is a 7 C delta-T - the healthy band's own range. When
    ``max_divergence`` was 5.0 this was downgraded to implausible/implausible
    and the whole V0.3 feature silently reported nothing.
    """
    pair = monitor().assess_pair(reading(45.0), reading(38.0, entity_id=RETURN), T0)
    assert pair.divergence == pytest.approx(7.0)
    assert pair.both_usable is True
    assert pair.reason_codes == ()


def test_small_delta_t_two_is_usable() -> None:
    """Regression companion: a low delta-T is a *hydraulic* signal, not a fault."""
    pair = monitor().assess_pair(reading(45.0), reading(43.0, entity_id=RETURN), T0)
    assert pair.divergence == pytest.approx(2.0)
    assert pair.both_usable is True


@pytest.mark.parametrize("delta", [5.0, 7.0, 14.0, 15.0, 20.0, 24.9])
def test_healthy_band_delta_ts_are_all_usable(delta: float) -> None:
    """Every delta-T the healthy band considers normal must stay usable."""
    pair = monitor().assess_pair(reading(45.0), reading(45.0 - delta, entity_id=RETURN), T0)
    assert pair.both_usable is True, delta
    assert pair.divergence == pytest.approx(delta)


def test_divergence_exactly_at_the_max_is_usable() -> None:
    """The comparison is ``> max_divergence``, so the limit itself passes."""
    pair = monitor(max_divergence=5.0).assess_pair(
        reading(45.0), reading(40.0, entity_id=RETURN), T0
    )
    assert pair.divergence == pytest.approx(5.0)
    assert pair.both_usable is True


def test_divergence_beyond_the_max_downgrades_both_sides() -> None:
    """Which probe is wrong is unknowable, so neither is believed."""
    pair = monitor(max_divergence=5.0).assess_pair(
        reading(70.0), reading(20.0, entity_id=RETURN), T0
    )
    assert pair.divergence == pytest.approx(50.0)
    assert pair.supply.quality == "implausible"
    assert pair.return_.quality == "implausible"
    assert pair.supply.value is None
    assert pair.return_.value is None
    assert pair.both_usable is False
    assert pair.reason_codes == ("probe_pair_divergent",)


def test_divergent_pair_records_the_reason_on_both_sides() -> None:
    pair = monitor(max_divergence=5.0).assess_pair(
        reading(70.0), reading(20.0, entity_id=RETURN), T0
    )
    for side in (pair.supply, pair.return_):
        assert "probe_pair_divergent" in side.reason_codes


def test_pair_with_a_missing_side_has_no_divergence() -> None:
    """A one-sided reading must never become a delta-T."""
    pair = monitor().assess_pair(reading(45.0), reading(None, entity_id=RETURN), T0)
    assert pair.divergence is None
    assert pair.both_usable is False
    assert pair.return_.quality == "probe_unavailable"


def test_pair_does_not_treat_a_missing_side_as_zero() -> None:
    """If the absent side were 0 C, divergence would be 45.0."""
    pair = monitor().assess_pair(reading(45.0), reading(None, entity_id=RETURN), T0)
    assert pair.divergence is None
    assert pair.divergence != pytest.approx(45.0)
    assert pair.supply.usable is True


@pytest.mark.parametrize(
    ("supply", "return_"),
    [
        (85.0, 40.0),  # sentinel
        (45.0, 85.5),  # sentinel on the other side
        (200.0, 40.0),  # out of range
        (45.0, float("nan")),  # not a number
    ],
)
def test_pair_with_an_unusable_side_has_no_divergence(supply: float, return_: float) -> None:
    pair = monitor().assess_pair(reading(supply), reading(return_, entity_id=RETURN), T0)
    assert pair.divergence is None
    assert pair.both_usable is False


def test_pair_with_a_stale_side_has_no_divergence() -> None:
    pair = monitor().assess_pair(
        reading(45.0, T0 - timedelta(hours=4)),
        reading(38.0, entity_id=RETURN),
        T0,
    )
    assert pair.divergence is None
    assert pair.supply.quality == "stale"


def test_pair_with_a_frozen_side_has_no_divergence() -> None:
    mon = monitor()
    mon.assess(reading(40.0), T0, burning=True)
    later = T0 + timedelta(hours=2)
    pair = mon.assess_pair(
        reading(40.0, later),
        reading(38.0, later, entity_id=RETURN),
        later,
        burning=True,
    )
    assert pair.supply.quality == "frozen"
    assert pair.divergence is None


def test_pair_propagates_burning_to_both_sides() -> None:
    mon = monitor()
    mon.assess(reading(40.0, entity_id=SUPPLY), T0, burning=True)
    mon.assess(reading(40.0, entity_id=RETURN), T0, burning=True)
    later = T0 + timedelta(hours=2)
    pair = mon.assess_pair(
        reading(40.0, later), reading(40.0, later, entity_id=RETURN), later, burning=True
    )
    assert pair.supply.quality == "frozen"
    assert pair.return_.quality == "frozen"


def test_pair_without_burning_leaves_identical_probes_valid() -> None:
    """Idle and steady is normal, on both sides."""
    mon = monitor()
    mon.assess(reading(40.0, entity_id=SUPPLY), T0, burning=False)
    mon.assess(reading(40.0, entity_id=RETURN), T0, burning=False)
    later = T0 + timedelta(hours=3)
    pair = mon.assess_pair(
        reading(40.0, later),
        reading(40.0, later, entity_id=RETURN),
        later,
        burning=False,
    )
    assert pair.both_usable is True
    assert pair.divergence == pytest.approx(0.0)


def test_divergence_is_signed_agnostic() -> None:
    """Whether supply or return reads hotter, the gap is the same size."""
    forward = monitor().assess_pair(reading(45.0), reading(38.0, entity_id=RETURN), T0)
    reverse = monitor().assess_pair(reading(38.0), reading(45.0, entity_id=RETURN), T0)
    assert forward.divergence == pytest.approx(reverse.divergence)


def test_pair_carries_the_hard_constraints() -> None:
    """Section 1: not a control input, and not a power measurement."""
    payload = monitor().assess_pair(reading(45.0), reading(38.0, entity_id=RETURN), T0).as_dict()
    assert payload["is_control_input"] is False
    assert payload["is_power_measurement"] is False


# ---------------------------------------------------------------------------
# as_dict / diagnostics export
# ---------------------------------------------------------------------------
def test_assessment_as_dict_is_json_serializable() -> None:
    payload = monitor().assess(reading(45.0), T0).as_dict()
    json.dumps(payload)
    assert payload["quality"] == "valid"
    assert payload["usable"] is True
    assert payload["reason_codes"] == []
    assert payload["age_seconds"] == pytest.approx(0.0)


def test_assessment_as_dict_uses_none_not_nan_for_a_refused_value() -> None:
    """NaN would survive json.dumps as a non-standard literal, so must not leak."""
    payload = monitor().assess(reading(85.0), T0).as_dict()
    assert payload["value"] is None
    assert "NaN" not in json.dumps(payload)
    assert payload["usable"] is False


def test_pair_as_dict_is_json_serializable_and_names_the_return_side() -> None:
    payload = monitor().assess_pair(reading(45.0), reading(38.0, entity_id=RETURN), T0).as_dict()
    json.dumps(payload)
    assert set(payload) >= {"supply", "return", "divergence", "both_usable", "reason_codes"}
    assert payload["divergence"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip() -> None:
    mon = monitor()
    mon.assess(reading(45.0), T0, burning=True)
    data = mon.snapshot()
    json.dumps(data)

    restored = monitor()
    restored.restore(data)
    assert restored.snapshot() == data


def test_restore_rehydrates_frozen_detection() -> None:
    """The frozen clock must survive a restart, not reset to 'never'."""
    mon = monitor()
    mon.assess(reading(40.0), T0, burning=True)
    saved = mon.snapshot()

    fresh = monitor()
    fresh.restore(saved)
    later = T0 + timedelta(hours=2)
    result = fresh.assess(reading(40.0, later), later, burning=True)
    assert result.quality == "frozen"
    assert result.frozen_seconds == pytest.approx(2 * 3600.0)


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        {},
        {"last_values": None, "last_changed_at": None},
        {"last_values": [], "last_changed_at": {}},
        {"last_values": {}, "last_changed_at": {}},
        {"last_values": {SUPPLY: "oops"}, "last_changed_at": {SUPPLY: T0.isoformat()}},
        {"last_values": {SUPPLY: 45.0}, "last_changed_at": {SUPPLY: 12345}},
        {"last_values": {SUPPLY: 45.0}, "last_changed_at": {}},
        {"last_values": {SUPPLY: 45.0}, "last_changed_at": {SUPPLY: "not-a-date"}},
        {"last_values": {"": 45.0}, "last_changed_at": {"": T0.isoformat()}},
        {"last_values": {SUPPLY: float("nan")}, "last_changed_at": {SUPPLY: T0.isoformat()}},
        {"unrelated": "payload"},
        [],
        "a string",
        42,
        {"last_values": {SUPPLY: 45.0}, "last_changed_at": {SUPPLY: "2026-01-15T08:00:00+00:00"}},
    ],
)
def test_restore_is_tolerant_of_dirty_data(garbage: object) -> None:
    """Persisted state is loaded at setup: it must never raise."""
    mon = monitor()
    mon.restore(garbage)  # type: ignore[arg-type]
    payload = mon.snapshot()
    json.dumps(payload)
    # Whatever was accepted, the monitor must still work afterwards.
    assert mon.assess(reading(45.0), T0).quality == "valid"


def test_restore_drops_a_value_without_a_usable_timestamp() -> None:
    """A value with no time cannot support a frozen verdict, so it is dropped."""
    mon = monitor()
    mon.restore({"last_values": {SUPPLY: 40.0}, "last_changed_at": {SUPPLY: None}})
    assert mon.snapshot()["last_values"] == {}


def test_restore_then_snapshot_is_stable() -> None:
    mon = monitor()
    mon.assess(reading(45.0), T0, burning=True)
    once = mon.snapshot()

    again = monitor()
    again.restore(once)
    assert again.snapshot() == once
    again.restore(again.snapshot())
    assert again.snapshot() == once


def test_restore_resets_state_before_loading() -> None:
    """Restoring must not merge with whatever the monitor already held."""
    mon = monitor()
    mon.assess(reading(45.0, entity_id="sensor.stale_leftover"), T0)
    mon.restore({})
    assert mon.snapshot()["last_values"] == {}
