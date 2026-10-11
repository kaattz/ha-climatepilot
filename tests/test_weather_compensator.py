"""Unit tests for the pure weather compensation engine (design doc 7).

These tests must run without Home Assistant: ``weather_compensator`` is a pure
engine, and several tests assert that property directly.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
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

from custom_components.climatepilot import weather_compensator as wc
from custom_components.climatepilot.const import (
    FEEDBACK_STALE,
    FEEDBACK_UNAVAILABLE,
    FEEDBACK_VALID,
    REASON_RANGE_CLAMP,
    REASON_THROTTLED,
    REASON_TOLERANCE,
    REASON_WEATHER_STALE,
    WEATHER_MODE_APPLY,
    WEATHER_MODE_SUGGEST,
)
from custom_components.climatepilot.models import WeatherCurve
from custom_components.climatepilot.weather_compensator import (
    CompensationResult,
    WeatherCompensator,
    WeatherReading,
)

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
CURVE = WeatherCurve()
MODULE_PATH = Path(wc.__file__)


def reading(
    value: float | None, at: datetime | None = T0, quality: str = FEEDBACK_VALID
) -> WeatherReading:
    """Build a reading whose age relative to ``T0`` is zero."""
    return WeatherReading(value=value, updated_at=at, quality=quality)


def compensator(**kwargs) -> WeatherCompensator:
    kwargs.setdefault("mode", WEATHER_MODE_APPLY)
    return WeatherCompensator(CURVE, **kwargs)


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


@pytest.mark.parametrize("forbidden", ["now", "utcnow", "today", "time"])
def test_engine_never_reads_the_wall_clock(forbidden: str) -> None:
    """All time must arrive through ``now``; no ambient clock access."""
    source = inspect.getsource(wc)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


# ---------------------------------------------------------------------------
# Curve / clamping
# ---------------------------------------------------------------------------
def test_supply_follows_the_linear_curve() -> None:
    """Outdoor 0C -> 45C and 15C -> 40C per the design doc curve."""
    comp = compensator()
    assert comp.evaluate(reading(0.0), T0).suggested == pytest.approx(45.0)
    assert comp.evaluate(reading(15.0), T0).suggested == pytest.approx(40.0)
    mid = comp.evaluate(reading(7.5), T0).suggested
    assert mid == pytest.approx(42.5)


def test_clamped_at_both_ends_with_reason_code() -> None:
    """Extrapolating past the range clamps and flags REASON_RANGE_CLAMP.

    The curve slopes downwards with rising outdoor temperature, so a very cold
    reading saturates the *upper* supply limit and a mild one the lower limit.
    """
    comp = compensator()
    # -45C extrapolates to 60C before clamping down to supply_max.
    low = comp.evaluate(reading(-45.0), T0)
    assert low.suggested == pytest.approx(CURVE.supply_max)
    assert REASON_RANGE_CLAMP in low.reason_codes

    high = comp.evaluate(reading(40.0), T0)
    assert high.suggested == pytest.approx(CURVE.supply_min)
    assert REASON_RANGE_CLAMP in high.reason_codes


def test_in_range_value_is_not_flagged_as_clamped() -> None:
    comp = compensator()
    result = comp.evaluate(reading(5.0), T0)
    assert REASON_RANGE_CLAMP not in result.reason_codes


def test_night_offset_is_applied() -> None:
    curve = WeatherCurve(night_offset=-2.0)
    comp = WeatherCompensator(curve, mode=WEATHER_MODE_APPLY)
    day = comp.evaluate(reading(7.5), T0, night=False)
    night = comp.evaluate(reading(7.5), T0, night=True)
    assert night.suggested == pytest.approx(day.suggested - 2.0)


# ---------------------------------------------------------------------------
# Safety: stale / unavailable data must never be guessed as 0 C
# ---------------------------------------------------------------------------
def test_stale_value_never_uses_zero_degrees() -> None:
    """A stale reading must not silently become a 0C computation."""
    comp = compensator(stale_after=90.0)
    stale_at = T0 - timedelta(minutes=120)
    result = comp.evaluate(reading(3.0, stale_at), T0)

    assert result.quality == FEEDBACK_STALE
    assert result.outdoor is None
    assert result.should_apply is False
    assert REASON_WEATHER_STALE in result.reason_codes
    # 0C would have produced 45.0 - the engine must not invent that.
    assert result.suggested != pytest.approx(45.0)


def test_stale_data_keeps_last_confirmed_value() -> None:
    """The last confirmed supply target survives a sensor dropout."""
    comp = compensator(stale_after=90.0)
    good = comp.evaluate(reading(10.0), T0)
    assert good.suggested == pytest.approx(41.6667, abs=0.01)

    later = T0 + timedelta(hours=5)
    stale = comp.evaluate(reading(10.0, T0), later)
    assert stale.quality == FEEDBACK_STALE
    assert stale.suggested == pytest.approx(good.suggested)
    assert stale.should_apply is False


def test_missing_timestamp_is_treated_as_stale() -> None:
    """Without a timestamp freshness cannot be proven, so we fail safe."""
    comp = compensator()
    result = comp.evaluate(reading(5.0, None), T0)
    assert result.quality == FEEDBACK_STALE
    assert REASON_WEATHER_STALE in result.reason_codes
    assert result.should_apply is False


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), float("inf"), float("-inf")],
)
def test_unusable_values_are_rejected(value: float | None) -> None:
    comp = compensator()
    result = comp.evaluate(reading(value), T0)
    assert result.should_apply is False
    assert result.quality in (FEEDBACK_STALE, FEEDBACK_UNAVAILABLE)
    assert REASON_WEATHER_STALE in result.reason_codes


def test_unavailable_quality_is_reported_as_unavailable() -> None:
    comp = compensator()
    result = comp.evaluate(reading(None, None, FEEDBACK_UNAVAILABLE), T0)
    assert result.quality == FEEDBACK_UNAVAILABLE
    assert result.suggested is None
    assert result.should_apply is False


def test_first_ever_call_with_stale_data_returns_no_value() -> None:
    """No confirmed value exists yet, so ``suggested`` stays ``None``."""
    comp = compensator()
    result = comp.evaluate(reading(4.0, T0 - timedelta(days=1)), T0)
    assert result.suggested is None
    assert result.outdoor is None
    assert result.quality == FEEDBACK_STALE


def test_age_exactly_at_the_limit_is_still_valid() -> None:
    """The staleness boundary itself is inclusive of valid data."""
    comp = compensator(stale_after=90.0)
    result = comp.evaluate(reading(5.0, T0 - timedelta(minutes=90)), T0)
    assert result.quality == FEEDBACK_VALID
    assert result.outdoor == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Suggest mode
# ---------------------------------------------------------------------------
def test_suggest_mode_never_applies() -> None:
    """Suggest mode computes a value but must never authorise a write."""
    comp = WeatherCompensator(CURVE, mode=WEATHER_MODE_SUGGEST)
    result = comp.evaluate(reading(0.0), T0)
    assert result.suggested == pytest.approx(45.0)
    assert result.should_apply is False
    assert result.quality == FEEDBACK_VALID


def test_suggest_mode_stays_off_even_with_huge_change() -> None:
    comp = WeatherCompensator(CURVE, mode=WEATHER_MODE_SUGGEST)
    comp.note_applied(45.0, T0)
    result = comp.evaluate(reading(15.0), T0 + timedelta(hours=10))
    assert result.should_apply is False


def test_unknown_mode_defaults_to_not_applying() -> None:
    comp = WeatherCompensator(CURVE, mode="bogus")
    assert comp.evaluate(reading(0.0), T0).should_apply is False


# ---------------------------------------------------------------------------
# Apply mode: hysteresis + min interval
# ---------------------------------------------------------------------------
def test_apply_mode_writes_on_first_evaluation() -> None:
    comp = compensator()
    result = comp.evaluate(reading(0.0), T0)
    assert result.should_apply is True
    assert result.suggested == pytest.approx(45.0)


def test_hysteresis_suppresses_small_changes() -> None:
    """A change smaller than the hysteresis is not worth a boiler write."""
    comp = compensator(hysteresis=1.0, min_interval=0.0)
    comp.note_applied(45.0, T0)

    # 0.2C outdoors -> well under 1.0C of supply movement.
    result = comp.evaluate(reading(0.2), T0 + timedelta(minutes=5))
    assert result.should_apply is False
    assert REASON_TOLERANCE in result.reason_codes


def test_change_at_or_above_hysteresis_applies() -> None:
    comp = compensator(hysteresis=1.0, min_interval=0.0)
    comp.note_applied(45.0, T0)
    # 4C outdoors -> 4/15*5 = 1.333C of supply movement, above hysteresis.
    result = comp.evaluate(reading(4.0), T0 + timedelta(minutes=5))
    assert result.should_apply is True
    assert REASON_TOLERANCE not in result.reason_codes


def test_min_interval_throttles_even_large_changes() -> None:
    comp = compensator(hysteresis=0.0, min_interval=30.0)
    comp.note_applied(45.0, T0)

    result = comp.evaluate(reading(15.0), T0 + timedelta(minutes=29))
    assert result.should_apply is False
    assert REASON_THROTTLED in result.reason_codes

    later = comp.evaluate(reading(15.0), T0 + timedelta(minutes=30))
    assert later.should_apply is True
    assert REASON_THROTTLED not in later.reason_codes


def test_last_applied_is_none_until_noted() -> None:
    comp = compensator()
    assert comp.last_applied is None
    comp.note_applied(44.0, T0)
    assert comp.last_applied == pytest.approx(44.0)


def test_note_applied_coerces_to_float() -> None:
    comp = compensator()
    comp.note_applied(44, T0)  # type: ignore[arg-type]
    assert isinstance(comp.last_applied, float)


def test_tolerance_and_throttle_can_both_be_reported() -> None:
    comp = compensator(hysteresis=5.0, min_interval=30.0)
    comp.note_applied(45.0, T0)
    result = comp.evaluate(reading(0.5), T0 + timedelta(minutes=1))
    assert REASON_TOLERANCE in result.reason_codes
    assert REASON_THROTTLED in result.reason_codes
    assert result.should_apply is False


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip() -> None:
    comp = compensator()
    comp.evaluate(reading(4.0), T0)
    comp.note_applied(43.0, T0)

    data = comp.snapshot()
    restored = compensator()
    restored.restore(data)

    assert restored.last_applied == pytest.approx(43.0)
    assert restored.snapshot()["last_suggested"] == pytest.approx(comp.snapshot()["last_suggested"])


def test_restore_keeps_stale_fallback_across_restart() -> None:
    """After a restart the last confirmed value still backs a stale reading."""
    comp = compensator(stale_after=90.0)
    good = comp.evaluate(reading(10.0), T0)

    restored = compensator(stale_after=90.0)
    restored.restore(comp.snapshot())

    stale = restored.evaluate(reading(10.0, T0), T0 + timedelta(hours=9))
    assert stale.quality == FEEDBACK_STALE
    assert stale.suggested == pytest.approx(good.suggested)


def test_restore_is_tolerant_of_garbage() -> None:
    comp = compensator()
    comp.restore({})  # empty
    comp.restore({"last_applied": "oops", "last_applied_at": 12345})
    assert comp.last_applied is None
    assert comp.snapshot()["last_applied_at"] is None


def test_restore_rehydrates_the_timestamp_for_throttling() -> None:
    """The throttle clock must survive persistence, not reset to 'never'."""
    comp = compensator(hysteresis=0.0, min_interval=30.0)
    comp.note_applied(45.0, T0)

    restored = compensator(hysteresis=0.0, min_interval=30.0)
    restored.restore(comp.snapshot())

    throttled = restored.evaluate(reading(15.0), T0 + timedelta(minutes=10))
    assert throttled.should_apply is False
    assert REASON_THROTTLED in throttled.reason_codes


def test_result_is_a_frozen_dataclass() -> None:
    result = compensator().evaluate(reading(0.0), T0)
    assert isinstance(result, CompensationResult)
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        result.suggested = 1.0  # type: ignore[misc]
