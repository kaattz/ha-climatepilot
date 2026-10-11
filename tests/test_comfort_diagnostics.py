"""Unit tests for the comfort analyser (docs/INTERFACES_V0.2.md 3.2).

The behavioural rules under test:

* a missing reading (``None``/``NaN``/``inf``) records **no sample** and yields
  ``unknown`` - never ``0``, which would invent a freezing room;
* overheating needs ``current - target >= overheat_delta`` sustained for
  ``overheat_dwell``;
* underheating needs ``target - current >= underheat_delta`` **and**
  ``demanding=True`` sustained for ``underheat_dwell``;
* below target with no demand is *not* underheating: the advice points at the
  hydraulics and the verdict stays ``ok``, so ``sensor.comfort_verdict`` and
  ``binary_sensor.underheating`` cannot contradict each other;
* a dropout freezes the evidence - one lost reading must not turn the whole
  window into "overheating";
* valid samples are throttled to ``sample_interval``; the ``None`` path is not,
  because safety semantics outrank the memory bound;
* zones are isolated from each other, and all state survives a restart.
"""

from __future__ import annotations

import ast
import inspect
import json
import math
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

from custom_components.climatepilot import comfort_diagnostics as cd
from custom_components.climatepilot.comfort_diagnostics import (
    ComfortAnalyser,
    ComfortSample,
    ComfortVerdict,
)
from custom_components.climatepilot.const import (
    ADVICE_CHECK_HYDRAULICS,
    ADVICE_CHECK_SCHEDULE,
    ADVICE_LOWER_SUPPLY,
    ADVICE_NONE,
    ADVICE_RAISE_SUPPLY,
    COMFORT_OK,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    COMFORT_UNKNOWN,
    DEFAULT_COMFORT_SAMPLE_INTERVAL,
    DEFAULT_COMFORT_WINDOW,
    DEFAULT_OVERHEAT_DELTA,
    DEFAULT_OVERHEAT_DWELL,
    DEFAULT_UNDERHEAT_DELTA,
    DEFAULT_UNDERHEAT_DWELL,
    REASON_BELOW_TARGET_WITHOUT_DEMAND,
)

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
MODULE_PATH = Path(cd.__file__)

LIVING = "living_room"
BEDROOM = "master_bedroom"


def at(seconds: float) -> datetime:
    """Return ``T0`` shifted by ``seconds``."""
    return T0 + timedelta(seconds=seconds)


def analyser(**kwargs) -> ComfortAnalyser:
    """Build an analyser with test-friendly, explicit parameters."""
    params = {
        "overheat_delta": 1.0,
        "overheat_dwell": 1800.0,
        "underheat_delta": 1.5,
        "underheat_dwell": 3600.0,
        "window": 86400.0,
        "sample_interval": 300.0,
    }
    params.update(kwargs)
    return ComfortAnalyser(**params)


def hold(
    an: ComfortAnalyser,
    zone_id: str,
    *,
    current: float,
    target: float,
    demanding: bool,
    start: float,
    duration: float,
    step: float = 300.0,
) -> None:
    """Feed ``current``/``target`` every ``step`` seconds for ``duration``."""
    elapsed = 0.0
    while elapsed <= duration:
        an.note(
            zone_id,
            current=current,
            target=target,
            demanding=demanding,
            now=at(start + elapsed),
        )
        elapsed += step


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
    source = inspect.getsource(cd)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


def test_defaults_come_from_const() -> None:
    """The frozen V0.2 constants are the defaults, not locally invented numbers."""
    signature = inspect.signature(ComfortAnalyser.__init__)
    assert signature.parameters["overheat_delta"].default == DEFAULT_OVERHEAT_DELTA
    assert signature.parameters["overheat_dwell"].default == DEFAULT_OVERHEAT_DWELL
    assert signature.parameters["underheat_delta"].default == DEFAULT_UNDERHEAT_DELTA
    assert signature.parameters["underheat_dwell"].default == DEFAULT_UNDERHEAT_DWELL
    assert signature.parameters["window"].default == DEFAULT_COMFORT_WINDOW
    assert signature.parameters["sample_interval"].default == DEFAULT_COMFORT_SAMPLE_INTERVAL


# ---------------------------------------------------------------------------
# SAFETY: a missing reading is never 0
# ---------------------------------------------------------------------------
def test_none_reading_records_no_sample_and_reports_unknown() -> None:
    """The V0.1 rule: a missing value must never be read as ``0``."""
    an = analyser()
    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(0))
    an.note(LIVING, current=20.0, target=None, demanding=True, now=at(300))

    verdict = an.verdict(LIVING, at(600))
    assert verdict.verdict == COMFORT_UNKNOWN
    assert verdict.advice == ADVICE_NONE
    assert verdict.samples == 0
    assert verdict.overheated_seconds == 0.0
    assert verdict.underheated_seconds == 0.0
    # A missing reading must not appear as a -21 degree deviation either.
    assert verdict.max_under == 0.0


def test_unknown_zone_reports_unknown_without_any_data() -> None:
    verdict = analyser().verdict("never_seen", T0)
    assert verdict.verdict == COMFORT_UNKNOWN
    assert verdict.advice == ADVICE_NONE
    assert verdict.zone_id == "never_seen"
    assert verdict.samples == 0
    assert verdict.reason_codes == (cd.REASON_NO_DATA,)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_readings_are_treated_as_missing(bad: float) -> None:
    """Test checklist item 11: a broken probe must not fake a temperature."""
    an = analyser()
    an.note(LIVING, current=bad, target=21.0, demanding=True, now=at(0))
    an.note(LIVING, current=30.0, target=bad, demanding=True, now=at(300))
    assert an.verdict(LIVING, at(600)).verdict == COMFORT_UNKNOWN


def test_non_numeric_readings_are_treated_as_missing() -> None:
    an = analyser()
    an.note(LIVING, current="warm", target=21.0, demanding=True, now=at(0))  # type: ignore[arg-type]
    assert an.verdict(LIVING, at(10)).verdict == COMFORT_UNKNOWN


def test_missing_reading_makes_an_active_zone_unknown_not_ok() -> None:
    """A room that had been fine must not read ``ok`` once its feed is lost."""
    an = analyser()
    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    assert an.verdict(LIVING, at(600)).verdict == COMFORT_OK

    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(700))
    assert an.verdict(LIVING, at(700)).verdict == COMFORT_UNKNOWN


def test_a_dropout_does_not_turn_the_whole_window_into_overheating() -> None:
    """One lost reading must not pollute the day's statistics.

    The last valid sample stops being extrapolated when the loss is seen, so the
    window is not silently filled with a decade of "overheating".
    """
    an = analyser()
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=600)
    an.note(LIVING, current=None, target=21.0, demanding=False, now=at(700))

    verdict = an.verdict(LIVING, at(700 + 10 * 86400))
    assert verdict.verdict == COMFORT_UNKNOWN
    # Only the ~600 s that were actually observed count, not ten days.
    assert verdict.overheated_seconds < 1000.0


def test_recovery_after_a_dropout_clears_the_gap() -> None:
    an = analyser()
    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(0))
    assert an.verdict(LIVING, at(0)).verdict == COMFORT_UNKNOWN

    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=300, duration=900)
    assert an.verdict(LIVING, at(1200)).verdict == COMFORT_OK


# ---------------------------------------------------------------------------
# Overheating
# ---------------------------------------------------------------------------
def test_overheating_requires_the_full_dwell() -> None:
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=1500)
    assert an.verdict(LIVING, at(1500)).verdict == COMFORT_OK

    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=1800, duration=300)
    assert an.verdict(LIVING, at(2100)).verdict == COMFORT_OVERHEATING


def test_overheating_just_below_the_delta_is_not_flagged() -> None:
    an = analyser(overheat_delta=1.0, overheat_dwell=1800.0)
    hold(an, LIVING, current=21.9, target=21.0, demanding=False, start=0, duration=7200)
    assert an.verdict(LIVING, at(7200)).verdict == COMFORT_OK


def test_overheating_exactly_at_the_delta_is_flagged() -> None:
    an = analyser(overheat_delta=1.0, overheat_dwell=1800.0)
    hold(an, LIVING, current=22.0, target=21.0, demanding=False, start=0, duration=1800)
    assert an.verdict(LIVING, at(1800)).verdict == COMFORT_OVERHEATING


def test_overheating_with_another_zone_demanding_advises_lowering_supply() -> None:
    an = analyser(overheat_dwell=1800.0)
    hold(an, BEDROOM, current=20.0, target=22.0, demanding=True, start=0, duration=2400)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=2400)

    verdict = an.verdict(LIVING, at(2400))
    assert verdict.verdict == COMFORT_OVERHEATING
    assert verdict.advice == ADVICE_LOWER_SUPPLY
    assert cd.REASON_OTHER_ZONE_DEMANDING in verdict.reason_codes


def test_overheating_with_nobody_else_demanding_advises_checking_the_schedule() -> None:
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=2400)

    verdict = an.verdict(LIVING, at(2400))
    assert verdict.verdict == COMFORT_OVERHEATING
    assert verdict.advice == ADVICE_CHECK_SCHEDULE
    assert cd.REASON_OTHER_ZONE_DEMANDING not in verdict.reason_codes


def test_overheating_is_measured_in_target_units_not_supply_units() -> None:
    """A 2 K overshoot is 2 K, whatever the absolute temperature."""
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=17.0, target=15.0, demanding=False, start=0, duration=1800)
    verdict = an.verdict(LIVING, at(1800))
    assert verdict.verdict == COMFORT_OVERHEATING
    assert verdict.max_over == pytest.approx(2.0)
    assert verdict.average_deviation == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Underheating REQUIRES demanding=True
# ---------------------------------------------------------------------------
def test_underheating_requires_demanding() -> None:
    """Below target AND genuinely calling for heat: a capacity symptom."""
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=0, duration=3600)

    verdict = an.verdict(LIVING, at(3600))
    assert verdict.verdict == COMFORT_UNDERHEATING
    assert verdict.advice == ADVICE_RAISE_SUPPLY
    assert verdict.underheated_seconds == pytest.approx(3600.0)


def test_below_target_without_demand_is_not_underheating() -> None:
    """V0.1 doctrine: below target with no demand is a hydraulics question.

    The verdict must stay ``ok`` - reporting ``underheating`` here would directly
    contradict ``binary_sensor.underheating`` ("持续低于目标**且确实在要热**")
    for the same room.
    """
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.0, target=22.0, demanding=False, start=0, duration=7200)

    verdict = an.verdict(LIVING, at(7200))
    assert verdict.verdict == COMFORT_OK
    assert verdict.advice == ADVICE_CHECK_HYDRAULICS
    assert REASON_BELOW_TARGET_WITHOUT_DEMAND in verdict.reason_codes
    # The deviation is reported so the room is never presented as warm...
    assert verdict.max_under == pytest.approx(2.0)
    # ...but it is not counted as underheating time.
    assert verdict.underheated_seconds == 0.0
    assert verdict.below_target_no_demand_seconds == pytest.approx(7200.0)


def test_below_target_without_demand_before_the_dwell_is_ok_without_advice() -> None:
    """Inside a thermostat's deadband this is simply normal."""
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.5, target=22.0, demanding=False, start=0, duration=1200)

    verdict = an.verdict(LIVING, at(1200))
    assert verdict.verdict == COMFORT_OK
    assert verdict.advice == ADVICE_NONE
    assert REASON_BELOW_TARGET_WITHOUT_DEMAND not in verdict.reason_codes


def test_underheating_needs_the_full_dwell() -> None:
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=0, duration=3000)
    assert an.verdict(LIVING, at(3000)).verdict == COMFORT_OK

    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=3300, duration=900)
    assert an.verdict(LIVING, at(4200)).verdict == COMFORT_UNDERHEATING


def test_below_the_underheat_delta_is_not_flagged_even_when_demanding() -> None:
    an = analyser(underheat_delta=1.5, underheat_dwell=3600.0)
    hold(an, LIVING, current=20.6, target=22.0, demanding=True, start=0, duration=7200)
    assert an.verdict(LIVING, at(7200)).verdict == COMFORT_OK


def test_demand_vanishing_resets_the_underheat_run() -> None:
    """The dwell must be *continuous* demand, not intermittent."""
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=0, duration=1800)
    # Thermostat gives up and stops calling for heat.
    hold(an, LIVING, current=20.0, target=22.0, demanding=False, start=2100, duration=1800)
    verdict = an.verdict(LIVING, at(3900))
    assert verdict.verdict == COMFORT_OK
    assert verdict.advice == ADVICE_CHECK_HYDRAULICS or verdict.advice == ADVICE_NONE


def test_underheat_run_restarts_after_a_normal_period() -> None:
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=22.0, target=22.0, demanding=True, start=0, duration=600)
    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=900, duration=1800)
    assert an.verdict(LIVING, at(2700)).verdict == COMFORT_OK

    hold(an, LIVING, current=20.0, target=22.0, demanding=True, start=3000, duration=2400)
    assert an.verdict(LIVING, at(5400)).verdict == COMFORT_UNDERHEATING


def test_overheating_wins_over_underheating() -> None:
    """Mutually exclusive numerically, but the order is pinned for determinism."""
    an = analyser(overheat_dwell=1800.0, underheat_dwell=3600.0)
    hold(an, LIVING, current=25.0, target=21.0, demanding=True, start=0, duration=3600)
    assert an.verdict(LIVING, at(3600)).verdict == COMFORT_OVERHEATING


def test_comfortable_zone_is_ok_without_advice() -> None:
    an = analyser()
    hold(an, LIVING, current=21.2, target=21.0, demanding=True, start=0, duration=7200)
    verdict = an.verdict(LIVING, at(7200))
    assert verdict.verdict == COMFORT_OK
    assert verdict.advice == ADVICE_NONE
    assert verdict.reason_codes == ()
    assert verdict.average_deviation == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Current state versus window evidence
# ---------------------------------------------------------------------------
def test_a_resolved_excursion_stops_flagging_the_verdict_but_keeps_the_evidence() -> None:
    """The verdict describes *now*; the window still reports what happened."""
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=24.0, target=21.0, demanding=False, start=0, duration=3600)
    assert an.verdict(LIVING, at(3600)).verdict == COMFORT_OVERHEATING

    # The room cools down to target and stays there.
    hold(an, LIVING, current=21.0, target=21.0, demanding=False, start=3900, duration=600)
    verdict = an.verdict(LIVING, at(4500))
    assert verdict.verdict == COMFORT_OK
    assert verdict.advice == ADVICE_NONE
    # The morning's excursion is still visible for the dashboard.
    assert verdict.overheated_seconds > 3000.0
    assert verdict.max_over == pytest.approx(3.0)


def test_verdict_uses_the_evidence_in_the_window() -> None:
    an = analyser(overheat_dwell=1800.0, window=3600.0)
    hold(an, LIVING, current=24.0, target=21.0, demanding=False, start=0, duration=1800)
    assert an.verdict(LIVING, at(1800)).verdict == COMFORT_OVERHEATING

    # Keep the feed alive with comfortable readings: four hours later the
    # morning excursion has rolled out of the window entirely.
    hold(
        an,
        LIVING,
        current=21.0,
        target=21.0,
        demanding=False,
        start=1800 + 4 * 3600,
        duration=600,
    )
    verdict = an.verdict(LIVING, at(1800 + 4 * 3600 + 600))
    assert verdict.verdict == COMFORT_OK
    assert verdict.overheated_seconds == pytest.approx(0.0)
    assert verdict.max_over == pytest.approx(0.0)


def test_an_abandoned_zone_goes_unknown_rather_than_reporting_stale_ok() -> None:
    """No recent evidence is ``unknown``, even if the last reading was fine."""
    an = analyser(overheat_dwell=1800.0, window=3600.0)
    hold(an, LIVING, current=21.0, target=21.0, demanding=False, start=0, duration=600)

    late = an.verdict(LIVING, at(10 * 3600))
    assert late.verdict == COMFORT_UNKNOWN
    assert late.advice == ADVICE_NONE
    assert late.overheated_seconds == 0.0


def test_mixed_evidence_is_summarised_with_signed_average() -> None:
    """``average_deviation`` is time-weighted, not a mean over samples.

    The room spent 900 s at +2.0 K and 600 s at -2.0 K, so the deviation it
    actually lived at is +0.4 K. Averaging the samples instead would let an
    arbitrary sampling cadence out-vote reality.
    """
    an = analyser(overheat_dwell=10**9, underheat_dwell=10**9)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=600)
    hold(an, LIVING, current=19.0, target=21.0, demanding=True, start=900, duration=600)

    verdict = an.verdict(LIVING, at(1800))
    assert verdict.verdict == COMFORT_OK
    assert verdict.max_over == pytest.approx(2.0)
    assert verdict.max_under == pytest.approx(2.0)
    assert verdict.average_deviation == pytest.approx(0.4)
    assert verdict.overheated_seconds == pytest.approx(900.0)
    assert verdict.underheated_seconds == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# Sampling throttle
# ---------------------------------------------------------------------------
def test_samples_are_throttled_to_the_sample_interval() -> None:
    an = analyser(sample_interval=300.0)
    for offset in range(0, 3000, 30):  # one reading every 30 s
        an.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(offset))

    verdict = an.verdict(LIVING, at(3000))
    assert verdict.samples == 10  # 3000 s / 300 s
    assert len(an.snapshot()["zones"][LIVING]["samples"]) == 10


def test_throttled_readings_still_extend_the_time_semantics() -> None:
    """Skipping *records* must not freeze the clock.

    If throttled calls did not update the time semantics, a zone fed every 30 s
    would look 300 s stale at every verdict and its dwell would never accumulate.
    Evidence runs to the last ``note`` (2370 s), not to ``now`` - a reading
    cannot vouch for a stretch nobody observed.
    """
    an = analyser(sample_interval=300.0, overheat_dwell=1800.0)
    for offset in range(0, 2400, 30):
        an.note(LIVING, current=23.0, target=21.0, demanding=False, now=at(offset))

    verdict = an.verdict(LIVING, at(2400))
    assert verdict.verdict == COMFORT_OVERHEATING
    assert verdict.overheated_seconds == pytest.approx(2370.0)
    assert verdict.samples == 8  # 0, 300, ... 2100


def test_first_valid_sample_after_a_gap_is_not_throttled() -> None:
    """A dropout must close precisely, not one throttle period late."""
    an = analyser(sample_interval=3600.0)
    an.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(0))
    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(60))
    an.note(LIVING, current=23.0, target=21.0, demanding=False, now=at(120))

    samples = an.snapshot()["zones"][LIVING]["samples"]
    assert len(samples) == 2
    assert samples[1][0] == at(120).isoformat()


def test_zero_sample_interval_records_every_reading() -> None:
    an = analyser(sample_interval=0.0)
    for offset in range(0, 100, 10):
        an.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(offset))
    assert an.verdict(LIVING, at(100)).samples == 10


def test_sample_memory_stays_bounded_over_a_long_run() -> None:
    an = analyser(sample_interval=300.0, window=3600.0)
    for offset in range(0, 40000, 300):
        an.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(offset))
    assert len(an.snapshot()["zones"][LIVING]["samples"]) <= 20


# ---------------------------------------------------------------------------
# Multiple zones
# ---------------------------------------------------------------------------
def test_zones_are_isolated_from_each_other() -> None:
    an = analyser(overheat_dwell=1800.0, underheat_dwell=3600.0)
    hold(an, LIVING, current=24.0, target=21.0, demanding=False, start=0, duration=3600)
    hold(an, BEDROOM, current=20.0, target=22.0, demanding=True, start=0, duration=3600)

    verdicts = an.verdicts(at(3600))
    assert set(verdicts) == {LIVING, BEDROOM}
    assert verdicts[LIVING].verdict == COMFORT_OVERHEATING
    assert verdicts[BEDROOM].verdict == COMFORT_UNDERHEATING


def test_one_zone_losing_its_feed_does_not_affect_another() -> None:
    an = analyser()
    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    hold(an, BEDROOM, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    an.note(BEDROOM, current=None, target=22.0, demanding=True, now=at(700))

    verdicts = an.verdicts(at(700))
    assert verdicts[LIVING].verdict == COMFORT_OK
    assert verdicts[BEDROOM].verdict == COMFORT_UNKNOWN


def test_drop_zone_forgets_everything_about_it() -> None:
    an = analyser()
    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    an.drop_zone(LIVING)

    assert LIVING not in an.verdicts(at(600))
    assert an.snapshot()["zones"] == {}
    assert an.verdict(LIVING, at(600)).verdict == COMFORT_UNKNOWN


def test_drop_zone_of_an_unknown_zone_is_a_no_op() -> None:
    an = analyser()
    an.drop_zone("never_seen")
    an.drop_zone("")  # type: ignore[arg-type]
    assert an.verdicts(T0) == {}


def test_verdicts_includes_zones_with_a_lost_feed() -> None:
    """A lost room must stay visible rather than silently disappearing."""
    an = analyser()
    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(0))
    assert set(an.verdicts(at(0))) == {LIVING}


def test_blank_zone_ids_are_ignored() -> None:
    an = analyser()
    an.note("", current=21.0, target=21.0, demanding=True, now=at(0))
    an.note("   ", current=21.0, target=21.0, demanding=True, now=at(0))
    assert an.verdicts(at(0)) == {}


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_verdicts_is_idempotent_for_a_fixed_now() -> None:
    """The coordinator calls ``verdicts(now)`` more than once per cycle."""
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=3600)
    now = at(3600)

    first = an.verdicts(now)
    second = an.verdicts(now)
    assert first == second
    assert an.snapshot() == an.snapshot()
    # A third call after the second pruning pass is still identical.
    assert an.verdicts(now) == first


def test_verdict_after_pruning_matches_the_verdict_before_it() -> None:
    """Pruning keeps the state at the cutoff, so results do not shift."""
    an = analyser(overheat_dwell=1800.0, window=7200.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=7200)

    now = at(7200)
    before = an.verdict(LIVING, now)
    an.verdict(LIVING, now)  # prune pass
    an.verdict(LIVING, now)  # prune pass
    after = an.verdict(LIVING, now)
    assert before == after


# ---------------------------------------------------------------------------
# as_dict export
# ---------------------------------------------------------------------------
def test_as_dict_is_json_serializable_and_explains_the_advice() -> None:
    an = analyser(underheat_dwell=3600.0)
    hold(an, LIVING, current=20.0, target=22.0, demanding=False, start=0, duration=5400)
    payload = an.verdict(LIVING, at(5400)).as_dict()
    json.dumps(payload)  # must not raise

    assert payload["zone_id"] == LIVING
    assert payload["verdict"] == COMFORT_OK
    assert payload["advice"] == ADVICE_CHECK_HYDRAULICS
    assert payload["below_target_no_demand_seconds"] == pytest.approx(5400.0)
    assert REASON_BELOW_TARGET_WITHOUT_DEMAND in payload["reason_codes"]


def test_as_dict_of_an_unknown_zone_is_safe() -> None:
    payload = ComfortVerdict(
        zone_id="x",
        verdict=COMFORT_UNKNOWN,
        advice=ADVICE_NONE,
        average_deviation=0.0,
        max_over=0.0,
        max_under=0.0,
        overheated_seconds=0.0,
        underheated_seconds=0.0,
        samples=0,
        reason_codes=(),
    ).as_dict()
    json.dumps(payload)
    assert payload["below_target_no_demand_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip_preserves_verdicts() -> None:
    an = analyser(overheat_dwell=1800.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=3600)
    hold(an, BEDROOM, current=20.0, target=22.0, demanding=True, start=0, duration=3600)
    now = at(3600)

    restored = analyser(overheat_dwell=1800.0)
    restored.restore(an.snapshot())

    assert restored.verdicts(now) == an.verdicts(now)
    assert restored.snapshot() == an.snapshot()


def test_restore_keeps_a_lost_feed_lost() -> None:
    """Resuming after a restart must not pretend the gap never happened."""
    an = analyser()
    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    an.note(LIVING, current=None, target=21.0, demanding=True, now=at(700))

    restored = analyser()
    restored.restore(an.snapshot())
    assert restored.verdict(LIVING, at(700)).verdict == COMFORT_UNKNOWN


def test_restore_preserves_the_sampling_throttle_clock() -> None:
    an = analyser(sample_interval=300.0)
    an.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(0))

    restored = analyser(sample_interval=300.0)
    restored.restore(an.snapshot())
    # Only 100 s later: still inside the throttle, so nothing is recorded.
    restored.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(100))
    assert restored.verdict(LIVING, at(100)).samples == 1

    restored.note(LIVING, current=21.0, target=21.0, demanding=True, now=at(300))
    assert restored.verdict(LIVING, at(300)).samples == 2


def test_restore_is_tolerant_of_garbage() -> None:
    an = analyser()
    an.restore(None)
    an.restore({})
    an.restore({"zones": "not-a-dict"})
    an.restore({"zones": {123: {"samples": []}, "": {"samples": []}}})
    an.restore({"zones": {LIVING: "not-a-dict"}})
    assert an.verdicts(T0) == {}


def test_restore_skips_malformed_samples_but_keeps_valid_ones() -> None:
    an = analyser()
    an.restore(
        {
            "zones": {
                LIVING: {
                    "samples": [
                        "junk",
                        [],
                        ["not-a-date", 21.0, 21.0, True],
                        ["2026-01-15T08:00:00+00:00", "nan", 21.0, True],
                        ["2026-01-15T08:00:00+00:00", 21.0, None, True],
                        ["2026-01-15T08:00:00+00:00", 23.0, 21.0, False],
                    ],
                    "gaps": ["junk", 5],
                    "last_sample_at": None,
                    "gap_open": "yes",
                }
            }
        }
    )
    verdict = an.verdict(LIVING, at(300))
    assert verdict.samples == 1
    assert verdict.max_over == pytest.approx(2.0)
    # last_sample_at was missing, so it is recovered from the newest sample and
    # the throttle clock is still sane.
    assert verdict.verdict == COMFORT_OK


def test_restore_never_raises_on_hostile_types() -> None:
    an = analyser()
    for payload in (
        {"zones": {LIVING: {"samples": 5, "gaps": {"a": 1}}}},
        {"zones": {LIVING: {"samples": [{"a": 1}], "gaps": [None]}}},
        {"zones": {LIVING: {"samples": [[1, 2, 3, 4, 5]], "gap_open": 1}}},
        {"zones": [1, 2, 3]},
    ):
        an.restore(payload)  # must not raise
    assert an.verdicts(T0) == {} or all(
        v.verdict in (COMFORT_UNKNOWN, COMFORT_OK) for v in an.verdicts(T0).values()
    )


def test_restore_clears_previous_state() -> None:
    an = analyser()
    hold(an, LIVING, current=21.0, target=21.0, demanding=True, start=0, duration=600)
    an.restore({"zones": {BEDROOM: {"samples": [], "gaps": []}}})
    assert set(an.verdicts(at(600))) == {BEDROOM}


def test_restored_samples_are_pruned_by_the_window() -> None:
    an = analyser(window=3600.0)
    hold(an, LIVING, current=23.0, target=21.0, demanding=False, start=0, duration=1800)

    restored = analyser(window=3600.0)
    restored.restore(an.snapshot())
    restored.verdict(LIVING, at(100000))
    assert len(restored.snapshot()["zones"][LIVING]["samples"]) <= 2


def test_sample_dataclass_is_frozen_and_slotted() -> None:
    sample = ComfortSample(at=T0, current=21.0, target=21.0, demanding=True)
    assert sample.current == 21.0
    with pytest.raises((AttributeError, TypeError)):
        sample.current = 22.0  # type: ignore[misc]
