"""Tests for the Delta-T / condensing / hydraulic / calibration engine (V0.3).

Expected values here are derived from the documented contract
(``docs/INTERFACES_V0.3.md`` section 4.2 and the frozen ``const`` block), not
from re-running the engine's own arithmetic. Where a figure could be produced
two ways, the test states the number the specification implies and lets the
engine disagree loudly if it wants to.
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

# Import isolation via the shared alias loader (see tests/_engine_loader.py).
# The engine is loaded under an alias package name so importing it never runs
# ``custom_components/climatepilot/__init__.py`` (which pulls in Home Assistant
# and the coordinator). The later ``test_..._does_not_import_homeassistant``
# assertion is what actually holds the "pure engine" line.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _engine_loader import load_engine

dtd = load_engine("delta_t_diagnostics")
_const = load_engine("const")

CALIBRATION_INSUFFICIENT_DATA = _const.CALIBRATION_INSUFFICIENT_DATA
CALIBRATION_LOWER_CURVE = _const.CALIBRATION_LOWER_CURVE
CALIBRATION_NONE = _const.CALIBRATION_NONE
CALIBRATION_RAISE_CURVE = _const.CALIBRATION_RAISE_CURVE
CONDENSING_LIKELY = _const.CONDENSING_LIKELY
CONDENSING_UNKNOWN = _const.CONDENSING_UNKNOWN
CONDENSING_UNLIKELY = _const.CONDENSING_UNLIKELY
DEFAULT_CONDENSING_RETURN_C = _const.DEFAULT_CONDENSING_RETURN_C
DEFAULT_DELTA_T_HEALTHY_MAX = _const.DEFAULT_DELTA_T_HEALTHY_MAX
DEFAULT_DELTA_T_HEALTHY_MIN = _const.DEFAULT_DELTA_T_HEALTHY_MIN
DEFAULT_DELTA_T_MIN_STABLE_SECONDS = _const.DEFAULT_DELTA_T_MIN_STABLE_SECONDS
DEFAULT_DELTA_T_SAMPLE_INTERVAL = _const.DEFAULT_DELTA_T_SAMPLE_INTERVAL
DEFAULT_DELTA_T_WINDOW = _const.DEFAULT_DELTA_T_WINDOW
HYDRAULIC_HIGH_FLOW = _const.HYDRAULIC_HIGH_FLOW
HYDRAULIC_LOW_FLOW = _const.HYDRAULIC_LOW_FLOW
HYDRAULIC_OK = _const.HYDRAULIC_OK
HYDRAULIC_UNKNOWN = _const.HYDRAULIC_UNKNOWN
PROBE_QUALITIES = _const.PROBE_QUALITIES
PROBE_UNAVAILABLE = _const.PROBE_UNAVAILABLE
PROBE_VALID = _const.PROBE_VALID
REASON_DELTA_T_HIGH = _const.REASON_DELTA_T_HIGH
REASON_DELTA_T_LOW = _const.REASON_DELTA_T_LOW
REASON_DELTA_T_OK = _const.REASON_DELTA_T_OK
REASON_DELTA_T_UNSTABLE = _const.REASON_DELTA_T_UNSTABLE
REASON_NOT_A_POWER_MEASUREMENT = _const.REASON_NOT_A_POWER_MEASUREMENT
REASON_PROBE_MISSING = _const.REASON_PROBE_MISSING

DeltaTDiagnostics = dtd.DeltaTDiagnostics
DeltaTReport = dtd.DeltaTReport
DeltaTSample = dtd.DeltaTSample

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
MODULE_PATH = Path(dtd.__file__)

#: Defaults, spelled out so a change to the frozen const block fails here.
DEFAULT_INTERVAL = 60.0
DEFAULT_WINDOW = 3600.0
DEFAULT_STABLE = 600.0
DEFAULT_HEALTHY_MIN = 5.0
DEFAULT_HEALTHY_MAX = 15.0
DEFAULT_CONDENSING_RETURN = 55.0


def engine(**kwargs: float) -> DeltaTDiagnostics:
    """Build an engine, allowing tests to shorten the settle window."""
    return DeltaTDiagnostics(**kwargs)  # type: ignore[arg-type]


def feed_stable(
    stats: DeltaTDiagnostics,
    *,
    start: datetime = T0,
    count: int,
    delta: float,
    supply: float = 45.0,
    interval: float = DEFAULT_INTERVAL,
    burning: bool = True,
    underheating: int = 0,
    overheating: int = 0,
) -> datetime:
    """Feed ``count`` evenly spaced settled samples; return the last timestamp."""
    last = start
    for index in range(count):
        last = start + timedelta(seconds=index * interval)
        stats.note(
            supply=supply,
            return_=supply - delta,
            burning=burning,
            active_zones=1,
            now=last,
            underheating_zones=underheating,
            overheating_zones=overheating,
        )
    return last


def settled_engine(**kwargs: float) -> DeltaTDiagnostics:
    """An engine with a short settle requirement, for readable tests."""
    kwargs.setdefault("min_stable_seconds", 120.0)
    return engine(**kwargs)


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


def test_module_does_not_import_other_engines() -> None:
    """This engine must stay independently testable (task requirement)."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [
        name
        for name in imported
        if name.lstrip(".") in {"probe_monitor", "comfort_diagnostics", "coordinator"}
    ]
    assert offenders == [], f"must not import {offenders}"


@pytest.mark.parametrize("forbidden", ["now", "utcnow", "today", "time"])
def test_engine_never_reads_the_wall_clock(forbidden: str) -> None:
    source = inspect.getsource(dtd)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


# ---------------------------------------------------------------------------
# Contract: the frozen signatures
# ---------------------------------------------------------------------------
def test_interface_signatures_match_the_contract() -> None:
    """Pin the section 4.2 signatures, including the two zone-evidence kwargs."""
    note_params = inspect.signature(DeltaTDiagnostics.note).parameters
    for name in (
        "self",
        "supply",
        "return_",
        "burning",
        "active_zones",
        "now",
        "underheating_zones",
        "overheating_zones",
    ):
        assert name in note_params, f"note() lost parameter {name}"
    assert note_params["underheating_zones"].default == 0
    assert note_params["overheating_zones"].default == 0

    init_params = inspect.signature(DeltaTDiagnostics.__init__).parameters
    for name in (
        "window",
        "sample_interval",
        "min_stable_seconds",
        "healthy_min",
        "healthy_max",
        "condensing_return_c",
    ):
        assert name in init_params, f"__init__() lost parameter {name}"


def test_constructor_defaults_follow_the_frozen_constants() -> None:
    defaults = inspect.signature(DeltaTDiagnostics.__init__).parameters
    assert defaults["window"].default == DEFAULT_DELTA_T_WINDOW
    assert defaults["sample_interval"].default == DEFAULT_DELTA_T_SAMPLE_INTERVAL
    assert defaults["min_stable_seconds"].default == DEFAULT_DELTA_T_MIN_STABLE_SECONDS
    assert defaults["healthy_min"].default == DEFAULT_DELTA_T_HEALTHY_MIN
    assert defaults["healthy_max"].default == DEFAULT_DELTA_T_HEALTHY_MAX
    assert defaults["condensing_return_c"].default == DEFAULT_CONDENSING_RETURN_C


def test_report_field_set_matches_the_contract() -> None:
    expected = {
        "delta_t",
        "average",
        "minimum",
        "maximum",
        "trend_per_hour",
        "stable_seconds",
        "samples",
        "quality",
        "condensing",
        "hydraulic",
        "calibration",
        "reason_codes",
    }
    assert set(DeltaTReport.__dataclass_fields__) == expected


def test_sample_dataclass_shape() -> None:
    assert set(DeltaTSample.__dataclass_fields__) == {
        "at",
        "delta_t",
        "supply_c",
        "return_c",
    }


# ---------------------------------------------------------------------------
# HARD CONSTRAINT: temperature difference is not heat power
# ---------------------------------------------------------------------------
def test_is_power_measurement_is_strictly_false() -> None:
    """Must be the boolean False, not merely falsy (section 1 hard constraint)."""
    data = engine().report(T0).as_dict()
    assert "is_power_measurement" in data
    assert data["is_power_measurement"] is False


def test_is_power_measurement_false_even_with_a_rich_settled_window() -> None:
    """No amount of good data turns delta-T into a power measurement."""
    stats = engine(min_stable_seconds=120.0)
    last = feed_stable(stats, count=5, delta=8.0)
    data = stats.report(last).as_dict()
    assert data["is_power_measurement"] is False
    assert data["hydraulic"] == HYDRAULIC_OK


@pytest.mark.parametrize(
    "delta,count",
    [(0.0, 0), (3.0, 5), (8.0, 5), (20.0, 5)],
)
def test_reason_codes_always_declare_not_a_power_measurement(delta: float, count: int) -> None:
    stats = engine(min_stable_seconds=120.0)
    last = feed_stable(stats, count=count, delta=delta) if count else T0
    assert REASON_NOT_A_POWER_MEASUREMENT in stats.report(last).reason_codes


def test_module_docstring_documents_the_power_limit() -> None:
    doc = dtd.__doc__ or ""
    assert "not heat power" in doc.lower() or "is not heat power" in doc.lower()
    assert "efficiency" in doc.lower()


# ---------------------------------------------------------------------------
# HARD CONSTRAINT: hydraulic wording (flow signal, not an efficiency grade)
# ---------------------------------------------------------------------------
def test_docstring_explains_low_and_high_delta_t_meanings() -> None:
    """The semantics the task pins: bypass/too much flow vs insufficient flow."""
    doc = (dtd.__doc__ or "").lower()
    assert "bypass" in doc
    assert "insufficient flow" in doc
    assert "not a judgement about efficiency" in doc or "not" in doc


def test_docstring_low_delta_t_semantics_are_the_documented_ones() -> None:
    doc = (dtd.__doc__ or "").lower()
    # Low delta-T -> too much flow or a short circuit through a bypass.
    assert "too much flow" in doc
    # High delta-T -> insufficient flow.
    assert "insufficient flow" in doc


# ---------------------------------------------------------------------------
# Missing readings are never 0
# ---------------------------------------------------------------------------
def test_none_supply_records_no_sample() -> None:
    stats = engine()
    stats.note(supply=None, return_=40.0, burning=True, active_zones=1, now=T0)
    report = stats.report(T0)
    assert report.samples == 0
    assert report.delta_t is None
    assert report.average is None


def test_none_return_records_no_sample() -> None:
    stats = engine()
    stats.note(supply=45.0, return_=None, burning=True, active_zones=1, now=T0)
    report = stats.report(T0)
    assert report.samples == 0
    assert report.delta_t is None


def test_missing_reading_is_not_treated_as_zero() -> None:
    """A None return must not become 0 C and manufacture a huge delta-T."""
    stats = engine()
    stats.note(supply=45.0, return_=None, burning=True, active_zones=1, now=T0)
    report = stats.report(T0)
    assert report.delta_t is None
    assert report.maximum is None
    # 45 - 0 would have been 45, far outside anything sane.
    assert report.maximum != 45.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_readings_record_no_sample(bad: float) -> None:
    stats = engine()
    stats.note(supply=bad, return_=40.0, burning=True, active_zones=1, now=T0)
    assert stats.report(T0).samples == 0


def test_missing_probe_yields_unknown_verdicts_not_normal_ones() -> None:
    """Absent evidence must never read as OK/LIKELY/NONE (V0.1 rule 4)."""
    stats = engine()
    stats.note(supply=45.0, return_=None, burning=True, active_zones=1, now=T0)
    report = stats.report(T0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN
    assert report.condensing == CONDENSING_UNKNOWN
    assert report.calibration == CALIBRATION_INSUFFICIENT_DATA
    assert report.quality == PROBE_UNAVAILABLE
    assert REASON_PROBE_MISSING in report.reason_codes


def test_recovery_after_a_missing_reading() -> None:
    stats = settled_engine()
    stats.note(supply=45.0, return_=None, burning=True, active_zones=1, now=T0)
    assert stats.report(T0).samples == 0
    last = feed_stable(stats, start=T0 + timedelta(seconds=60), count=5, delta=8.0)
    report = stats.report(last)
    assert report.quality == PROBE_VALID
    assert report.hydraulic == HYDRAULIC_OK


# ---------------------------------------------------------------------------
# Sampling throttle
# ---------------------------------------------------------------------------
def test_sampling_is_throttled_to_the_interval() -> None:
    stats = engine(sample_interval=60.0)
    stats.note(supply=45.0, return_=37.0, burning=True, active_zones=1, now=T0)
    # 30 s later: inside the throttle window, so nothing is recorded.
    stats.note(
        supply=46.0,
        return_=38.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=30),
    )
    assert stats.report(T0 + timedelta(seconds=30)).samples == 1


def test_sample_is_recorded_once_the_interval_elapses() -> None:
    stats = engine(sample_interval=60.0)
    stats.note(supply=45.0, return_=37.0, burning=True, active_zones=1, now=T0)
    stats.note(
        supply=45.0,
        return_=37.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=60),
    )
    assert stats.report(T0 + timedelta(seconds=60)).samples == 2


def test_zero_sample_interval_records_every_reading() -> None:
    stats = engine(sample_interval=0.0)
    for index in range(4):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index),
        )
    assert stats.report(T0 + timedelta(seconds=3)).samples == 4


def test_throttled_call_still_refreshes_the_current_value() -> None:
    """Throttling is about history, not about noticing a probe dropping out."""
    stats = engine(sample_interval=60.0)
    stats.note(supply=45.0, return_=37.0, burning=True, active_zones=1, now=T0)
    stats.note(
        supply=45.0,
        return_=None,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=10),
    )
    report = stats.report(T0 + timedelta(seconds=10))
    assert report.quality == PROBE_UNAVAILABLE
    assert report.delta_t is None


# ---------------------------------------------------------------------------
# Delta-T statistics
# ---------------------------------------------------------------------------
def test_delta_t_is_supply_minus_return() -> None:
    stats = engine()
    stats.note(supply=45.0, return_=37.5, burning=True, active_zones=1, now=T0)
    assert stats.report(T0).delta_t == pytest.approx(7.5)


def test_average_minimum_maximum_over_the_window() -> None:
    stats = engine(sample_interval=0.0)
    for index, delta in enumerate([6.0, 8.0, 10.0]):
        stats.note(
            supply=45.0,
            return_=45.0 - delta,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index),
        )
    report = stats.report(T0 + timedelta(seconds=2))
    assert report.minimum == pytest.approx(6.0)
    assert report.maximum == pytest.approx(10.0)
    assert report.average == pytest.approx(8.0)
    assert report.samples == 3


def test_trend_is_degrees_per_hour_and_positive_when_widening() -> None:
    """Points (0s, 6), (60s, 7), (120s, 8) give a slope of exactly +60 C/h."""
    stats = engine(sample_interval=0.0)
    for index, delta in enumerate([6.0, 7.0, 8.0]):
        stats.note(
            supply=45.0,
            return_=45.0 - delta,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    assert stats.report(T0 + timedelta(seconds=120)).trend_per_hour == pytest.approx(60.0)


def test_trend_is_negative_when_narrowing() -> None:
    stats = engine(sample_interval=0.0)
    for index, delta in enumerate([10.0, 9.0, 8.0]):
        stats.note(
            supply=45.0,
            return_=45.0 - delta,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    assert stats.report(T0 + timedelta(seconds=120)).trend_per_hour == pytest.approx(-60.0)


def test_trend_is_zero_for_a_flat_run() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0)
    assert stats.report(last).trend_per_hour == pytest.approx(0.0)


def test_trend_is_none_with_a_single_sample() -> None:
    stats = engine()
    stats.note(supply=45.0, return_=37.0, burning=True, active_zones=1, now=T0)
    assert stats.report(T0).trend_per_hour is None


# ---------------------------------------------------------------------------
# Window rolling
# ---------------------------------------------------------------------------
def test_samples_outside_the_window_are_dropped() -> None:
    stats = engine(window=3600.0)
    feed_stable(stats, count=3, delta=8.0)
    # Two hours later the whole window has rolled past the samples.
    report = stats.report(T0 + timedelta(hours=2))
    assert report.samples == 0


def test_samples_inside_the_window_are_kept() -> None:
    stats = engine(window=3600.0)
    feed_stable(stats, count=3, delta=8.0)
    report = stats.report(T0 + timedelta(seconds=120))
    assert report.samples == 3


def test_window_boundary_is_inclusive() -> None:
    stats = engine(window=3600.0, sample_interval=0.0)
    stats.note(supply=45.0, return_=37.0, burning=True, active_zones=1, now=T0)
    # Exactly one window later the sample is still inside.
    assert stats.report(T0 + timedelta(seconds=3600)).samples == 1
    assert stats.report(T0 + timedelta(seconds=3601)).samples == 0


def test_old_samples_never_accumulate_without_bound() -> None:
    stats = engine(window=600.0, sample_interval=0.0)
    for index in range(300):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 10),
        )
    assert stats.report(T0 + timedelta(seconds=2990)).samples <= 61
    assert len(stats.snapshot()["entries"]) <= 4096


# ---------------------------------------------------------------------------
# Stability gate
# ---------------------------------------------------------------------------
def test_unsettled_run_reports_unknown_hydraulic() -> None:
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=5, delta=8.0)  # spans only 240 s
    report = stats.report(last)
    assert report.stable_seconds == pytest.approx(240.0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN
    assert REASON_DELTA_T_UNSTABLE in report.reason_codes


def test_unsettled_run_reports_insufficient_calibration_data() -> None:
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=5, delta=8.0, underheating=1)
    # Even with underheating evidence, an unsettled loop gives no advice.
    assert stats.report(last).calibration == CALIBRATION_INSUFFICIENT_DATA


def test_settle_threshold_is_inclusive() -> None:
    """Exactly min_stable_seconds of settled evidence must be enough."""
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=11, delta=8.0)  # spans exactly 600 s
    report = stats.report(last)
    assert report.stable_seconds == pytest.approx(600.0)
    assert report.hydraulic == HYDRAULIC_OK


def test_one_second_short_of_the_settle_threshold_is_not_enough() -> None:
    stats = engine(min_stable_seconds=600.0, sample_interval=0.0)
    for index in range(11):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    # Shift the newest evidence 1 s earlier than the 600 s mark.
    stats.note(
        supply=45.0,
        return_=37.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=599),
    )
    report = stats.report(T0 + timedelta(seconds=599))
    assert report.stable_seconds == pytest.approx(599.0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN


def test_transient_jump_restarts_the_settled_run() -> None:
    """A step larger than the stability tolerance breaks the run."""
    stats = engine(min_stable_seconds=600.0, sample_interval=0.0)
    for index in range(11):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    # A large valve/pump transient right at the end.
    stats.note(
        supply=45.0,
        return_=25.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=600),
    )
    report = stats.report(T0 + timedelta(seconds=600))
    assert report.stable_seconds == pytest.approx(0.0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN


def test_probe_dropout_splits_the_settled_run() -> None:
    """Readings either side of a dropout must not be stitched into one run."""
    stats = engine(min_stable_seconds=600.0, sample_interval=0.0)
    for index in range(11):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    # Dropout, then readings resume with an identical value.
    stats.note(
        supply=None,
        return_=None,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=605),
    )
    stats.note(
        supply=45.0,
        return_=37.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=660),
    )
    report = stats.report(T0 + timedelta(seconds=660))
    # The post-dropout run is only one sample long, so nothing is settled.
    assert report.stable_seconds == pytest.approx(0.0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN


# ---------------------------------------------------------------------------
# Hydraulic verdicts: three bands
# ---------------------------------------------------------------------------
def test_low_delta_t_maps_to_low_flow_key_and_reason() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=3.0)  # below healthy_min 5.0
    report = stats.report(last)
    assert report.hydraulic == HYDRAULIC_LOW_FLOW
    assert REASON_DELTA_T_LOW in report.reason_codes


def test_normal_delta_t_maps_to_ok() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0)
    report = stats.report(last)
    assert report.hydraulic == HYDRAULIC_OK
    assert REASON_DELTA_T_OK in report.reason_codes


def test_high_delta_t_maps_to_high_flow_key_and_reason() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=20.0)  # above healthy_max 15.0
    report = stats.report(last)
    assert report.hydraulic == HYDRAULIC_HIGH_FLOW
    assert REASON_DELTA_T_HIGH in report.reason_codes


def test_healthy_band_edges_are_inclusive() -> None:
    """Exactly healthy_min and healthy_max are healthy (band is closed)."""
    low_edge = settled_engine()
    low_last = feed_stable(low_edge, count=5, delta=5.0)
    assert low_edge.report(low_last).hydraulic == HYDRAULIC_OK

    high_edge = settled_engine()
    high_last = feed_stable(high_edge, count=5, delta=15.0)
    assert high_edge.report(high_last).hydraulic == HYDRAULIC_OK


def test_custom_healthy_band_is_respected() -> None:
    stats = settled_engine(healthy_min=2.0, healthy_max=4.0)
    last = feed_stable(stats, count=5, delta=3.0)
    assert stats.report(last).hydraulic == HYDRAULIC_OK


def test_a_healthy_floor_heating_delta_t_reaches_the_report() -> None:
    """A textbook-healthy 7 C delta-T must produce a real report.

    Guards the boundary where probe-level plausibility and delta-T health
    interact: supply/return probes sit on the SAME hydraulic loop, so their
    difference *is* the quantity being measured. If probe-layer plausibility
    rejected a healthy difference (e.g. a "max believable divergence" at or below
    the healthy band), the coordinator would hand this engine two ``None`` values,
    no sample would ever be recorded, and every verdict would be permanently
    ``unknown`` - silently, with no error anywhere.

    Uses the default constructor deliberately: this must hold for a stock engine,
    not only for one tuned by the test.
    """
    stats = DeltaTDiagnostics()
    for minute in range(20):
        stats.note(
            supply=45.0,
            return_=38.0,
            burning=True,
            active_zones=2,
            now=T0 + timedelta(minutes=minute),
        )
    report = stats.report(T0 + timedelta(minutes=20))

    assert report.delta_t == pytest.approx(7.0, abs=0.01)
    assert report.samples > 0
    assert report.quality == PROBE_VALID
    # A 7 C difference is inside the healthy 5..15 C band.
    assert report.hydraulic == HYDRAULIC_OK
    assert report.condensing == CONDENSING_LIKELY  # return 38 C is below 55 C
    assert report.average == pytest.approx(7.0, abs=0.01)


def test_healthy_delta_t_survives_the_default_settle_threshold() -> None:
    """The default 600 s settle requirement must be reachable in real time.

    The 20 samples above span 1140 s, comfortably past the default threshold, so
    a stock engine reaches a settled verdict rather than staying ``unknown``.
    """
    stats = DeltaTDiagnostics()
    for minute in range(20):
        stats.note(
            supply=45.0,
            return_=38.0,
            burning=True,
            active_zones=2,
            now=T0 + timedelta(minutes=minute),
        )
    report = stats.report(T0 + timedelta(minutes=20))
    assert report.stable_seconds >= DEFAULT_DELTA_T_MIN_STABLE_SECONDS
    assert report.hydraulic != HYDRAULIC_UNKNOWN


def test_no_samples_gives_unknown_hydraulic() -> None:
    report = engine().report(T0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN
    assert report.samples == 0


# ---------------------------------------------------------------------------
# Quality: worse side wins
# ---------------------------------------------------------------------------
def test_quality_is_valid_when_both_probes_are_usable() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=3, delta=8.0)
    assert stats.report(last).quality == PROBE_VALID


@pytest.mark.parametrize(
    ("supply", "return_"),
    [(None, 40.0), (45.0, None), (None, None)],
)
def test_quality_takes_the_worse_side(supply: float | None, return_: float | None) -> None:
    stats = engine()
    stats.note(supply=supply, return_=return_, burning=True, active_zones=1, now=T0)
    assert stats.report(T0).quality == PROBE_UNAVAILABLE


def test_unavailable_ranks_worse_than_valid() -> None:
    """Pin the documented severity ordering from the frozen const block."""
    assert PROBE_QUALITIES.index(PROBE_UNAVAILABLE) > PROBE_QUALITIES.index(PROBE_VALID)
    assert dtd._worse_quality(PROBE_VALID, PROBE_UNAVAILABLE) == PROBE_UNAVAILABLE
    assert dtd._worse_quality(PROBE_UNAVAILABLE, PROBE_VALID) == PROBE_UNAVAILABLE
    assert dtd._worse_quality(PROBE_VALID, PROBE_VALID) == PROBE_VALID


# ---------------------------------------------------------------------------
# Condensing: three verdicts
# ---------------------------------------------------------------------------
def test_condensing_likely_when_return_is_cool_while_burning() -> None:
    stats = settled_engine()
    # supply 45 / return 40 -> delta_t 5 (healthy), return below 55 C.
    last = feed_stable(stats, count=5, delta=5.0, burning=True)
    report = stats.report(last)
    assert report.condensing == CONDENSING_LIKELY
    assert report.hydraulic == HYDRAULIC_OK


def test_condensing_unlikely_when_return_is_hot_while_burning() -> None:
    stats = settled_engine()
    # supply 70 / return 60 -> delta_t 10 (healthy), return above 55 C.
    last = feed_stable(stats, count=5, delta=10.0, supply=70.0, burning=True)
    assert stats.report(last).condensing == CONDENSING_UNLIKELY


def test_condensing_unknown_when_never_burning() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, burning=False)
    report = stats.report(last)
    assert report.condensing == CONDENSING_UNKNOWN


def test_condensing_unknown_when_not_settled() -> None:
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=3, delta=8.0, burning=True)
    assert stats.report(last).condensing == CONDENSING_UNKNOWN


def test_condensing_boundary_is_inclusive() -> None:
    """return_c <= condensing_return_c counts as likely."""
    stats = settled_engine(condensing_return_c=55.0)
    last = feed_stable(stats, count=5, delta=15.0, supply=70.0, burning=True)
    assert stats.report(last).condensing == CONDENSING_LIKELY


def test_condensing_uses_only_burning_samples() -> None:
    """An idle cool-down must not be read as condensing operation."""
    stats = settled_engine(sample_interval=0.0)
    # Five settled, burning samples with a hot return.
    for index in range(5):
        stats.note(
            supply=70.0,
            return_=60.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 30),
        )
    # Then one settled, non-burning sample with a cold return.
    stats.note(
        supply=40.0,
        return_=30.0,
        burning=False,
        active_zones=0,
        now=T0 + timedelta(seconds=150),
    )
    report = stats.report(T0 + timedelta(seconds=150))
    assert report.condensing == CONDENSING_UNLIKELY


# ---------------------------------------------------------------------------
# Calibration advice: four verdicts
# ---------------------------------------------------------------------------
def test_calibration_raise_curve_on_underheating_evidence() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, underheating=1)
    report = stats.report(last)
    assert report.calibration == CALIBRATION_RAISE_CURVE


def test_calibration_lower_curve_on_overheating_evidence() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, overheating=2)
    assert stats.report(last).calibration == CALIBRATION_LOWER_CURVE


def test_calibration_none_without_zone_evidence() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0)
    assert stats.report(last).calibration == CALIBRATION_NONE


def test_calibration_insufficient_data_when_unsettled() -> None:
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=2, delta=8.0, underheating=1)
    assert stats.report(last).calibration == CALIBRATION_INSUFFICIENT_DATA


def test_calibration_insufficient_data_without_samples() -> None:
    assert engine().report(T0).calibration == CALIBRATION_INSUFFICIENT_DATA


def test_conflicting_zone_evidence_withholds_advice() -> None:
    """A global curve change cannot fix a distribution problem."""
    stats = settled_engine(sample_interval=0.0)
    for index in range(5):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=2,
            now=T0 + timedelta(seconds=index * 30),
            underheating_zones=1,
            overheating_zones=1,
        )
    report = stats.report(T0 + timedelta(seconds=120))
    assert report.calibration == CALIBRATION_NONE
    assert dtd.REASON_CALIBRATION_CONFLICT in report.reason_codes


def test_calibration_evidence_inside_the_window_only() -> None:
    """Zone evidence that has rolled out of the window must not drive advice."""
    stats = engine(window=600.0, sample_interval=0.0, min_stable_seconds=60.0)
    for index in range(3):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 30),
            underheating_zones=1,
        )
    # 20 minutes later that evidence is outside the 10 minute window.
    last = T0 + timedelta(minutes=20)
    for index in range(3):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=last + timedelta(seconds=index * 30),
        )
    report = stats.report(last + timedelta(seconds=60))
    assert report.calibration == CALIBRATION_NONE


def test_calibration_never_mutates_any_curve() -> None:
    """Advice is advice: the engine exposes no curve-writing surface."""
    public = [name for name in dir(DeltaTDiagnostics) if not name.startswith("_")]
    assert not any("set" in name or "apply" in name or "write" in name for name in public)


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------
def test_report_is_idempotent_for_a_fixed_now() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, underheating=1)
    first = stats.report(last)
    second = stats.report(last)
    assert first == second
    assert first.as_dict() == second.as_dict()


def test_report_is_idempotent_across_many_calls() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0)
    reports = [stats.report(last) for _ in range(5)]
    assert all(report == reports[0] for report in reports)


def test_report_is_idempotent_without_any_samples() -> None:
    stats = engine()
    assert stats.report(T0) == stats.report(T0)


def test_report_is_idempotent_while_probes_are_missing() -> None:
    stats = settled_engine()
    stats.note(supply=None, return_=None, burning=True, active_zones=1, now=T0)
    assert stats.report(T0) == stats.report(T0)


def test_repeated_reports_at_a_later_now_are_idempotent() -> None:
    """The coordinator polls with an advancing ``now``; each poll is stable.

    Pruning for a later ``now`` legitimately discards rolled-out history, so this
    only asserts idempotence *per* ``now`` (the documented contract) rather than
    time-travel stability, which nothing promises and pruning cannot offer.
    """
    stats = engine(window=3600.0, sample_interval=60.0)
    feed_stable(stats, count=5, delta=8.0)

    later = T0 + timedelta(seconds=300)
    first = stats.report(later)
    assert first == stats.report(later)
    assert stats.report(later).as_dict() == first.as_dict()


def test_a_later_report_does_not_resurrect_old_samples() -> None:
    """Rolled-out history stays gone; the window is a real bound, not a cache."""
    stats = engine(window=3600.0, sample_interval=60.0)
    feed_stable(stats, count=5, delta=8.0)
    assert stats.report(T0 + timedelta(seconds=240)).samples == 5

    # Five hours on, every recorded sample is outside the window.
    assert stats.report(T0 + timedelta(hours=5)).samples == 0
    # ...and asking again for the old instant cannot bring them back.
    assert stats.report(T0 + timedelta(seconds=240)).samples == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, underheating=1)
    before = stats.report(last)

    restored = settled_engine()
    restored.restore(stats.snapshot())
    assert restored.report(last) == before


def test_restore_preserves_the_quality_and_current_value() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=7.0)
    restored = settled_engine()
    restored.restore(stats.snapshot())
    report = restored.report(last)
    assert report.quality == PROBE_VALID
    assert report.delta_t == pytest.approx(stats.report(last).delta_t)


def test_restore_keeps_the_settled_run_intact() -> None:
    """A restart must not silently reset the settle clock to zero."""
    stats = engine(min_stable_seconds=600.0)
    last = feed_stable(stats, count=11, delta=8.0)
    assert stats.report(last).hydraulic == HYDRAULIC_OK

    restored = engine(min_stable_seconds=600.0)
    restored.restore(stats.snapshot())
    assert restored.report(last).hydraulic == HYDRAULIC_OK


def test_restore_keeps_dropout_breaks_across_a_restart() -> None:
    stats = engine(min_stable_seconds=600.0, sample_interval=0.0)
    for index in range(11):
        stats.note(
            supply=45.0,
            return_=37.0,
            burning=True,
            active_zones=1,
            now=T0 + timedelta(seconds=index * 60),
        )
    stats.note(
        supply=None,
        return_=None,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=605),
    )
    stats.note(
        supply=45.0,
        return_=37.0,
        burning=True,
        active_zones=1,
        now=T0 + timedelta(seconds=660),
    )

    restored = engine(min_stable_seconds=600.0, sample_interval=0.0)
    restored.restore(stats.snapshot())
    report = restored.report(T0 + timedelta(seconds=660))
    assert report.stable_seconds == pytest.approx(0.0)
    assert report.hydraulic == HYDRAULIC_UNKNOWN


def test_restore_accepts_none_and_empties_state() -> None:
    stats = settled_engine()
    feed_stable(stats, count=3, delta=8.0)
    stats.restore(None)
    assert stats.report(T0 + timedelta(seconds=120)).samples == 0


@pytest.mark.parametrize(
    "garbage",
    [
        {},
        {"entries": "not-a-list"},
        {"entries": [[None, None, None, None]]},
        {"entries": [["not-a-date", 5.0, 45.0, 40.0]]},
        {"entries": [[T0.isoformat(), "nan", 45.0, 40.0]]},
        {"breaks": 5, "last_sample_at": 12345},
        {"last_note_at": "nonsense", "supply_quality": "banana"},
        {"entries": None, "current_delta_t": "inf"},
    ],
)
def test_restore_tolerates_dirty_data(garbage: dict) -> None:
    stats = settled_engine()
    stats.restore(garbage)
    report = stats.report(T0)
    assert report is not None
    assert report.samples == 0
    assert report.hydraulic == HYDRAULIC_UNKNOWN


def test_restore_repairs_an_inconsistent_quality_label() -> None:
    """A payload claiming 'valid' with samples must not outlive the samples."""
    stats = settled_engine()
    stats.restore(
        {
            "entries": [],
            "supply_quality": PROBE_VALID,
            "return_quality": PROBE_VALID,
            "current_delta_t": 8.0,
        }
    )
    report = stats.report(T0)
    assert report.samples == 0
    assert report.quality == PROBE_UNAVAILABLE
    assert report.delta_t is None


def test_restore_sorts_out_of_order_samples() -> None:
    stats = settled_engine()
    stats.restore(
        {
            "entries": [
                [(T0 + timedelta(seconds=120)).isoformat(), 8.0, 45.0, 37.0, True, 0, 0],
                [T0.isoformat(), 8.0, 45.0, 37.0, True, 0, 0],
            ]
        }
    )
    report = stats.report(T0 + timedelta(seconds=120))
    assert report.samples == 2
    assert report.minimum == pytest.approx(8.0)


def test_snapshot_is_json_friendly() -> None:
    import json

    stats = settled_engine()
    feed_stable(stats, count=3, delta=8.0)
    payload = stats.snapshot()
    assert json.loads(json.dumps(payload)) == payload


def test_report_as_dict_is_json_friendly() -> None:
    import json

    stats = settled_engine()
    last = feed_stable(stats, count=3, delta=8.0)
    data = stats.report(last).as_dict()
    assert json.loads(json.dumps(data)) == data


def test_restore_round_trip_is_stable_across_two_generations() -> None:
    stats = settled_engine()
    last = feed_stable(stats, count=5, delta=8.0, overheating=1)

    first = settled_engine()
    first.restore(stats.snapshot())
    second = settled_engine()
    second.restore(first.snapshot())

    assert second.report(last) == stats.report(last)
