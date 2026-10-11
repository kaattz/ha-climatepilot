"""Unit tests for the burner diagnostics engine (design doc 8).

The behavioural rules under test:

* space-heating and domestic-hot-water burns are separated whenever the device
  reports enough detail, and a generic burner state is flagged as
  ``includes_dhw`` so a shower is never mistaken for a floor-heating short
  cycle (design doc 8, test checklist item 10);
* a feed loss (``unknown``/``unavailable``) never fabricates a flame-off, which
  would otherwise invent a short cycle and raise a false alarm;
* 1h/24h ignition counters and the duration histogram respect their windows;
* all state survives a restart through :meth:`BurnerStats.restore`.
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

# Defensive isolation: see the note in tests/test_heat_source.py. The engine
# itself must not import HA (asserted below); this guard only keeps the *test
# module* importable when the package ``__init__.py`` regresses.
try:  # pragma: no cover - import guard
    import custom_components.climatepilot as _real_package  # noqa: F401
except Exception as _import_error:  # pragma: no cover - defensive fallback
    # Loud, not silent on purpose: the engines are fine, but the integration
    # package just failed to import. Printing it here means the real cause is
    # visible instead of surfacing later as a confusing error in a module
    # that merely happened to import the package first.
    import sys as _sys
    import types

    print(
        f"\n[engine-test isolation] custom_components.climatepilot failed to "
        f"import: {type(_import_error).__name__}: {_import_error}\n"
        f"[engine-test isolation] falling back to a stub so the pure-engine "
        f"tests still run. THE INTEGRATION ITSELF IS BROKEN - see "
        f"tests/test_integration_setup.py for the real failure.\n",
        file=_sys.stderr,
    )

    _parent = sys.modules.get("custom_components")
    if _parent is None:
        _parent = types.ModuleType("custom_components")
        sys.modules["custom_components"] = _parent
    _parent.__path__ = [str(_REPO_ROOT / "custom_components")]  # type: ignore[attr-defined]

    _stub = types.ModuleType("custom_components.climatepilot")
    _stub.__path__ = [str(_REPO_ROOT / "custom_components" / "climatepilot")]  # type: ignore[attr-defined]
    sys.modules["custom_components.climatepilot"] = _stub
    _parent.climatepilot = _stub  # type: ignore[attr-defined]

from custom_components.climatepilot import diagnostics_engine as de
from custom_components.climatepilot.const import (
    CIRCUIT_DHW,
    CIRCUIT_HEATING,
    DEFAULT_SHORT_CYCLE_SECONDS,
    FEEDBACK_UNCERTAIN,
    FEEDBACK_VALID,
    QUALITY_FAIR,
    QUALITY_GOOD,
    QUALITY_POOR,
    QUALITY_UNKNOWN,
)
from custom_components.climatepilot.diagnostics_engine import (
    HISTOGRAM_BINS,
    KIND_DHW,
    KIND_HEATING,
    KIND_UNKNOWN,
    BurnerStats,
    classify_burner_state,
)

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
MODULE_PATH = Path(de.__file__)


def burn(
    stats: BurnerStats,
    start: datetime,
    duration: float,
    *,
    kind_state: str = "heating",
) -> datetime:
    """Record one complete burn and return the time it ended."""
    stats.note_state(kind_state, start)
    end = start + timedelta(seconds=duration)
    stats.note_state("off", end)
    return end


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
    source = inspect.getsource(de)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


# ---------------------------------------------------------------------------
# snapshot() contract (regression: the includes_dhw NameError)
# ---------------------------------------------------------------------------
def test_snapshot_exposes_includes_dhw_as_bool() -> None:
    """Regression test: ``snapshot()`` must never raise and must carry the flag."""
    stats = BurnerStats()
    burn(stats, T0, 600.0)
    snap = stats.snapshot(T0 + timedelta(seconds=700))

    assert "includes_dhw" in snap
    assert isinstance(snap["includes_dhw"], bool)
    assert snap["includes_dhw"] is False


def test_snapshot_on_a_fresh_instance_is_safe() -> None:
    snap = BurnerStats().snapshot(T0)
    assert snap["includes_dhw"] is False
    assert snap["burning"] is False
    assert snap["starts_24h"] == 0
    assert snap["last_duration"] is None


def test_snapshot_keys_used_by_the_platform_layer_exist() -> None:
    """Pin the key names the sensor platform reads."""
    snap = BurnerStats().snapshot(T0)
    for key in (
        "starts_24h",
        "last_duration",
        "short_cycle_alert",
        "includes_dhw",
        "short_cycles_24h",
        "heating_starts_24h",
        "dhw_starts_24h",
        "histogram",
        "histogram_heating",
        "runtime_24h",
    ):
        assert key in snap, f"missing snapshot key: {key}"


def test_last_duration_is_seconds_as_float() -> None:
    """The sensor platform expects numeric seconds, not a timedelta."""
    stats = BurnerStats()
    burn(stats, T0, 450.0)
    snap = stats.snapshot(T0 + timedelta(seconds=600))
    assert isinstance(snap["last_duration"], float)
    assert snap["last_duration"] == pytest.approx(450.0)


# ---------------------------------------------------------------------------
# State classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("heating", (True, KIND_HEATING, True)),
        ("heat", (True, KIND_HEATING, True)),
        ("dhw", (True, KIND_DHW, True)),
        ("hot_water", (True, KIND_DHW, True)),
        ("off", (False, None, True)),
        ("idle", (False, None, True)),
        ("unknown", (False, None, False)),
        ("unavailable", (False, None, False)),
        (None, (False, None, False)),
    ],
)
def test_classify_burner_state(state, expected) -> None:
    assert classify_burner_state(state) == expected


def test_generic_on_state_is_unattributed() -> None:
    """An unrecognised 'on' cannot be claimed as space heating."""
    assert classify_burner_state("on") == (True, KIND_UNKNOWN, True)


# ---------------------------------------------------------------------------
# Basic counting
# ---------------------------------------------------------------------------
def test_single_burn_updates_last_duration_and_count() -> None:
    stats = BurnerStats()
    burn(stats, T0, 900.0)
    snap = stats.snapshot(T0 + timedelta(seconds=1000))

    assert snap["starts_24h"] == 1
    assert snap["starts_total"] == 1
    assert snap["last_duration"] == pytest.approx(900.0)
    assert snap["last_duration_kind"] == KIND_HEATING
    assert snap["burns_24h"] == 1
    assert snap["burning"] is False


def test_duplicate_on_states_count_only_one_ignition() -> None:
    stats = BurnerStats()
    for offset in range(0, 50, 10):
        stats.note_state("heating", T0 + timedelta(seconds=offset))
    snap = stats.snapshot(T0 + timedelta(seconds=60))
    assert snap["starts_24h"] == 1
    assert snap["burning"] is True


def test_current_duration_grows_while_burning() -> None:
    stats = BurnerStats()
    stats.note_state("heating", T0)
    snap = stats.snapshot(T0 + timedelta(seconds=240))
    assert snap["current_duration"] == pytest.approx(240.0)
    assert snap["burning"] is True


def test_current_duration_is_none_when_not_burning() -> None:
    stats = BurnerStats()
    burn(stats, T0, 300.0)
    assert stats.snapshot(T0 + timedelta(seconds=400))["current_duration"] is None


# ---------------------------------------------------------------------------
# 1h / 24h windows
# ---------------------------------------------------------------------------
def test_starts_1h_and_24h_windows_differ() -> None:
    stats = BurnerStats()
    now = T0 + timedelta(hours=4)
    # Two ignitions well over an hour ago, one 30 minutes ago.
    burn(stats, T0, 60.0)
    burn(stats, T0 + timedelta(hours=2), 60.0)
    burn(stats, now - timedelta(minutes=30), 60.0)

    snap = stats.snapshot(now)
    assert snap["starts_24h"] == 3
    # The +2h ignition is 2h old; only the most recent one is inside 1h.
    assert snap["starts_1h"] == 1


def test_starts_1h_window_counts_each_ignition() -> None:
    stats = BurnerStats()
    now = T0 + timedelta(hours=2)
    for minutes in (10, 20, 30):
        burn(stats, now - timedelta(minutes=minutes), 30.0)
    assert stats.snapshot(now)["starts_1h"] == 3


def test_starts_beyond_24h_are_not_counted() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0)
    snap = stats.snapshot(T0 + timedelta(hours=48))
    assert snap["starts_24h"] == 0
    assert snap["starts_1h"] == 0
    # The lifetime counter keeps them.
    assert snap["starts_total"] == 1


def test_window_boundary_is_inclusive() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0)
    assert stats.snapshot(T0 + timedelta(hours=24))["starts_24h"] == 1
    assert stats.snapshot(T0 + timedelta(hours=24, seconds=1))["starts_24h"] == 0


# ---------------------------------------------------------------------------
# Short cycle detection
# ---------------------------------------------------------------------------
def test_short_cycle_raises_the_alert() -> None:
    stats = BurnerStats(short_cycle_seconds=DEFAULT_SHORT_CYCLE_SECONDS)
    burn(stats, T0, 60.0)  # 1 minute, well under the 5 minute threshold
    snap = stats.snapshot(T0 + timedelta(seconds=120))

    assert snap["short_cycles_24h"] == 1
    assert snap["short_cycle_alert"] is True
    assert snap["short_cycle_seconds"] == pytest.approx(DEFAULT_SHORT_CYCLE_SECONDS)


def test_long_burn_does_not_raise_the_alert() -> None:
    stats = BurnerStats()
    burn(stats, T0, 1800.0)  # 30 minutes
    snap = stats.snapshot(T0 + timedelta(seconds=1900))
    assert snap["short_cycles_24h"] == 0
    assert snap["short_cycle_alert"] is False


def test_burn_exactly_at_the_threshold_is_not_short() -> None:
    stats = BurnerStats(short_cycle_seconds=300.0)
    burn(stats, T0, 300.0)
    assert stats.snapshot(T0 + timedelta(seconds=400))["short_cycles_24h"] == 0


def test_short_cycle_threshold_is_configurable() -> None:
    """A 120s burn is normal at a 60s threshold and short at a 300s one."""
    lenient = BurnerStats(short_cycle_seconds=60.0)
    burn(lenient, T0, 120.0)
    assert lenient.snapshot(T0 + timedelta(seconds=200))["short_cycle_alert"] is False

    strict = BurnerStats(short_cycle_seconds=300.0)
    burn(strict, T0, 120.0)
    assert strict.snapshot(T0 + timedelta(seconds=200))["short_cycle_alert"] is True


def test_short_cycle_alert_property_matches_snapshot() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0)
    now = T0 + timedelta(seconds=120)
    assert stats.short_cycle_alert == stats.snapshot(now)["short_cycle_alert"]


def test_short_cycle_alert_property_is_false_before_any_observation() -> None:
    assert BurnerStats().short_cycle_alert is False


# ---------------------------------------------------------------------------
# SAFETY: DHW must not be counted as a heating short cycle (design doc 8)
# ---------------------------------------------------------------------------
def test_dhw_burn_is_not_a_heating_short_cycle() -> None:
    """Test checklist item 10: a shower must not look like floor-heating cycling."""
    stats = BurnerStats(short_cycle_seconds=300.0)
    burn(stats, T0, 90.0, kind_state="dhw")
    snap = stats.snapshot(T0 + timedelta(seconds=150))

    assert snap["dhw_starts_24h"] == 1
    assert snap["heating_starts_24h"] == 0
    # Counted in the overall totals...
    assert snap["starts_24h"] == 1
    assert snap["burns_24h"] == 1
    assert snap["includes_dhw"] is True
    # ...but never as a space-heating short cycle.
    assert snap["short_cycles_24h"] == 0
    assert snap["short_cycle_alert"] is False


def test_dhw_excluded_from_the_heating_histogram() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0, kind_state="dhw")
    burn(stats, T0 + timedelta(hours=1), 1800.0, kind_state="heating")
    snap = stats.snapshot(T0 + timedelta(hours=2))

    assert snap["histogram"]["0-5m"] == 1  # the dhw burn
    assert snap["histogram"]["20-40m"] == 1  # the heating burn
    assert snap["histogram_heating"]["0-5m"] == 0
    assert snap["histogram_heating"]["20-40m"] == 1


def test_generic_burner_state_sets_includes_dhw() -> None:
    """A device without separate states must be flagged as possibly-DHW."""
    stats = BurnerStats()
    burn(stats, T0, 120.0, kind_state="on")
    snap = stats.snapshot(T0 + timedelta(seconds=200))

    assert snap["unknown_kind_starts_24h"] == 1
    assert snap["includes_dhw"] is True


def test_heating_only_system_does_not_set_includes_dhw() -> None:
    stats = BurnerStats()
    burn(stats, T0, 600.0, kind_state="heating")
    assert stats.snapshot(T0 + timedelta(seconds=700))["includes_dhw"] is False


def test_kind_is_refined_when_a_specific_state_arrives() -> None:
    """A burn first seen as generic is upgraded once the detail arrives."""
    stats = BurnerStats()
    stats.note_state("on", T0)
    stats.note_state("heating", T0 + timedelta(seconds=30))
    snap = stats.snapshot(T0 + timedelta(seconds=60))
    assert snap["kind"] == KIND_HEATING


def test_dhw_runtime_is_reported_separately() -> None:
    stats = BurnerStats()
    burn(stats, T0, 100.0, kind_state="dhw")
    burn(stats, T0 + timedelta(hours=1), 900.0, kind_state="heating")
    snap = stats.snapshot(T0 + timedelta(hours=2))

    assert snap["dhw_runtime_24h"] == pytest.approx(100.0)
    assert snap["heating_runtime_24h"] == pytest.approx(900.0)
    assert snap["runtime_24h"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# SAFETY: a feed loss must not fabricate a flame-off
# ---------------------------------------------------------------------------
def test_unknown_state_does_not_close_an_ongoing_burn() -> None:
    """Treating a dropout as 'flame off' would invent a short cycle."""
    stats = BurnerStats(short_cycle_seconds=300.0)
    stats.note_state("heating", T0)
    stats.note_state("unavailable", T0 + timedelta(seconds=60))

    snap = stats.snapshot(T0 + timedelta(seconds=90))
    assert snap["burning"] is True
    assert snap["last_duration"] is None
    assert snap["short_cycles_24h"] == 0
    assert snap["short_cycle_alert"] is False


def test_unknown_state_is_reported_as_uncertain_quality() -> None:
    stats = BurnerStats()
    stats.note_state("heating", T0)
    stats.note_state("unknown", T0 + timedelta(seconds=30))
    snap = stats.snapshot(T0 + timedelta(seconds=60))

    assert snap["quality"] == FEEDBACK_UNCERTAIN
    assert snap["state_known"] is False


def test_quality_recovers_when_the_feed_returns() -> None:
    stats = BurnerStats()
    stats.note_state("heating", T0)
    stats.note_state("unavailable", T0 + timedelta(seconds=30))
    stats.note_state("heating", T0 + timedelta(seconds=60))
    snap = stats.snapshot(T0 + timedelta(seconds=90))

    assert snap["quality"] == FEEDBACK_VALID
    assert snap["state_known"] is True
    assert snap["burning"] is True


def test_burn_closed_after_a_dropout_uses_the_full_duration() -> None:
    """A dropout must not shorten the measured burn."""
    stats = BurnerStats()
    stats.note_state("heating", T0)
    stats.note_state("unavailable", T0 + timedelta(seconds=100))
    stats.note_state("off", T0 + timedelta(seconds=900))
    snap = stats.snapshot(T0 + timedelta(seconds=1000))

    assert snap["last_duration"] == pytest.approx(900.0)
    assert snap["starts_24h"] == 1


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------
def test_histogram_buckets_cover_every_duration() -> None:
    stats = BurnerStats()
    durations = [60.0, 400.0, 800.0, 1500.0, 3000.0, 7200.0]
    for index, duration in enumerate(durations):
        burn(stats, T0 + timedelta(hours=index), duration)
    snap = stats.snapshot(T0 + timedelta(hours=7))

    assert snap["histogram"] == {
        "0-5m": 1,
        "5-10m": 1,
        "10-20m": 1,
        "20-40m": 1,
        "40-60m": 1,
        "60m+": 1,
    }


def test_histogram_totals_match_the_burn_count() -> None:
    stats = BurnerStats()
    for index in range(5):
        burn(stats, T0 + timedelta(hours=index), 60.0 * (index + 1))
    snap = stats.snapshot(T0 + timedelta(hours=6))
    assert sum(snap["histogram"].values()) == snap["burns_24h"]


def test_histogram_starts_empty_with_all_buckets() -> None:
    snap = BurnerStats().snapshot(T0)
    assert set(snap["histogram"]) == {label for label, _ in HISTOGRAM_BINS}
    assert all(count == 0 for count in snap["histogram"].values())


# ---------------------------------------------------------------------------
# Demand correlation (design doc 8)
# ---------------------------------------------------------------------------
def test_burn_without_demand_is_recorded() -> None:
    """Diagnostic correlation: firing with no zone demand is worth flagging."""
    stats = BurnerStats()
    stats.note_demand(T0, False)
    burn(stats, T0 + timedelta(seconds=10), 300.0)
    snap = stats.snapshot(T0 + timedelta(seconds=400))
    assert snap["burns_without_demand_24h"] == 1


def test_burn_with_demand_is_not_flagged() -> None:
    stats = BurnerStats()
    stats.note_demand(T0, True)
    burn(stats, T0 + timedelta(seconds=10), 300.0)
    snap = stats.snapshot(T0 + timedelta(seconds=400))
    assert snap["burns_without_demand_24h"] == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip_keeps_counters() -> None:
    stats = BurnerStats()
    burn(stats, T0, 120.0)
    burn(stats, T0 + timedelta(hours=1), 600.0)
    now = T0 + timedelta(hours=2)

    restored = BurnerStats()
    restored.restore(stats.snapshot(now))

    before = stats.snapshot(now)
    after = restored.snapshot(now)
    for key in ("starts_24h", "starts_total", "burns_24h", "last_duration"):
        assert after[key] == before[key], key


def test_restore_accepts_a_bare_persist_payload() -> None:
    stats = BurnerStats()
    burn(stats, T0, 120.0)
    payload = stats.snapshot(T0 + timedelta(hours=1))["persist"]

    restored = BurnerStats()
    restored.restore(payload)
    assert restored.snapshot(T0 + timedelta(hours=1))["starts_24h"] == 1


def test_restore_keeps_an_open_burn_across_a_restart() -> None:
    """A restart during a burn must not lose the start time."""
    stats = BurnerStats()
    stats.note_state("heating", T0)

    restored = BurnerStats()
    restored.restore(stats.snapshot(T0 + timedelta(seconds=60)))

    snap = restored.snapshot(T0 + timedelta(seconds=600))
    assert snap["burning"] is True
    assert snap["current_duration"] == pytest.approx(600.0)


def test_restore_of_a_burn_without_start_time_is_dropped() -> None:
    """An inconsistent payload must not create an unbounded burn."""
    stats = BurnerStats()
    stats.restore({"burning": True, "burn_start": None, "state": "heating"})
    assert stats.snapshot(T0)["burning"] is False


def test_restore_preserves_short_cycle_history() -> None:
    stats = BurnerStats(short_cycle_seconds=300.0)
    burn(stats, T0, 60.0)
    now = T0 + timedelta(seconds=120)

    restored = BurnerStats(short_cycle_seconds=300.0)
    restored.restore(stats.snapshot(now))
    assert restored.snapshot(now)["short_cycle_alert"] is True


def test_restore_is_tolerant_of_garbage() -> None:
    stats = BurnerStats()
    stats.restore({})
    stats.restore(
        {
            "starts": [["not-a-date", "heating"], ["2026-01-01T00:00:00+00:00", "bogus"]],
            "cycles": [["2026-01-01T00:00:00+00:00", "nan", "heating"]],
            "starts_total": "nonsense",
            "last_duration": "nan",
        }
    )
    snap = stats.snapshot(T0)
    assert snap["starts_24h"] == 0
    assert snap["last_duration"] is None


def test_restore_of_a_bare_timestamp_list_is_ignored_safely() -> None:
    stats = BurnerStats()
    stats.restore({"starts": "not-a-list", "cycles": 5})
    assert stats.snapshot(T0)["starts_total"] == 0


# ---------------------------------------------------------------------------
# Pruning / bounds
# ---------------------------------------------------------------------------
def test_old_events_are_pruned() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0)
    # Evaluate far in the future: retention is 24h.
    stats.snapshot(T0 + timedelta(days=30))
    snap = stats.snapshot(T0 + timedelta(days=30, hours=1))
    assert snap["starts_24h"] == 0
    # The lifetime counter is deliberately not pruned.
    assert snap["starts_total"] == 1


def test_many_burns_do_not_grow_state_without_bound() -> None:
    stats = BurnerStats()
    now = T0
    for index in range(400):
        now = T0 + timedelta(minutes=index * 2)
        burn(stats, now, 30.0)
    stats.snapshot(now)
    persisted = stats.snapshot(now)["persist"]
    assert len(persisted["starts"]) <= 512
    assert len(persisted["cycles"]) <= 512


# ===========================================================================
# V0.2 - docs/INTERFACES_V0.2.md 3.3
# ===========================================================================
#
# Two hard requirements drive this block:
#
# 1. **An error is not a burn.** ``Error`` (code 33 on the target Rinnai
#    REB-A2747FF-CH) falling through to the old generic-burning fallback would
#    count a lockout as an ignition and could fabricate a short-cycle alarm.
# 2. **The circuit disambiguates the vocabulary.** That device spells its
#    space-heating and DHW burner states identically (``Burning`` = 32,
#    ``Heating Water`` = 31), so the token alone cannot be attributed.


# ---------------------------------------------------------------------------
# SAFETY: an error must never be counted as a burn
# ---------------------------------------------------------------------------
def test_error_state_is_not_burning() -> None:
    """Regression: ``Error`` used to fall through to the generic-burning branch."""
    assert classify_burner_state("Error") == (False, None, True)
    assert classify_burner_state("error") == (False, None, True)


@pytest.mark.parametrize("state", ["Error", "fault", "lockout", "alarm", "FAILED", "blocked"])
def test_fault_tokens_are_known_and_not_burning(state: str) -> None:
    burning, _, known = classify_burner_state(state)
    assert known is True
    assert burning is False


def test_error_state_does_not_create_an_ignition() -> None:
    stats = BurnerStats()
    stats.note_state("Error", T0)
    snap = stats.snapshot(T0 + timedelta(seconds=60))

    assert snap["starts_24h"] == 0
    assert snap["burns_24h"] == 0
    assert snap["burning"] is False
    assert snap["last_duration"] is None


def test_error_state_cannot_fabricate_a_short_cycle_alarm() -> None:
    """The alarm is the one signal a user acts on, so it must not be faked."""
    stats = BurnerStats(short_cycle_seconds=300.0)
    for offset in range(0, 600, 60):
        stats.note_state("Error", T0 + timedelta(seconds=offset))
    snap = stats.snapshot(T0 + timedelta(seconds=700))

    assert snap["short_cycles_24h"] == 0
    assert snap["short_cycle_alert"] is False


def test_error_state_closes_an_open_burn_without_adding_an_ignition() -> None:
    stats = BurnerStats()
    stats.note_state("heating", T0)
    stats.note_state("Error", T0 + timedelta(seconds=400))
    snap = stats.snapshot(T0 + timedelta(seconds=500))

    assert snap["starts_24h"] == 1  # only the real burn
    assert snap["burning"] is False
    assert snap["last_duration"] == pytest.approx(400.0)
    assert snap["error_state"] is True


def test_error_state_is_exposed_on_the_snapshot() -> None:
    stats = BurnerStats()
    stats.note_state("Error", T0)
    assert stats.snapshot(T0)["error_state"] is True

    stats.note_state("heating", T0 + timedelta(seconds=60))
    assert stats.snapshot(T0 + timedelta(seconds=90))["error_state"] is False


# ---------------------------------------------------------------------------
# SAFETY: an unrecognised token is not a burn
# ---------------------------------------------------------------------------
def test_unrecognised_token_does_not_invent_an_ignition() -> None:
    """No information must not be guessed as "burning"."""
    stats = BurnerStats()
    stats.note_state("0x51f", T0)
    snap = stats.snapshot(T0 + timedelta(seconds=600))

    assert snap["starts_24h"] == 0
    assert snap["burning"] is False
    assert snap["state_known"] is False
    assert snap["quality"] == FEEDBACK_UNCERTAIN


def test_unrecognised_token_holds_an_ongoing_burn() -> None:
    """Treated like a feed loss: hold, never fabricate flame-off or flame-on."""
    stats = BurnerStats()
    stats.note_state("heating", T0)
    stats.note_state("0x51f", T0 + timedelta(seconds=60))
    snap = stats.snapshot(T0 + timedelta(seconds=600))

    assert snap["burning"] is True
    assert snap["starts_24h"] == 1
    assert snap["current_duration"] == pytest.approx(600.0)


def test_unrecognised_token_in_a_known_circuit_is_attributed_to_that_circuit() -> None:
    """On the space-heating sensor, "something is burning" IS space heating."""
    heating = BurnerStats(circuit=CIRCUIT_HEATING)
    heating.note_state("0x51f", T0)
    assert heating.snapshot(T0 + timedelta(seconds=600))["kind"] == KIND_HEATING

    dhw = BurnerStats(circuit=CIRCUIT_DHW)
    dhw.note_state("0x51f", T0)
    assert dhw.snapshot(T0 + timedelta(seconds=600))["kind"] == KIND_DHW


# ---------------------------------------------------------------------------
# SAFETY: the circuit disambiguates the shared Rinnai vocabulary
# ---------------------------------------------------------------------------
def test_heating_water_is_recognised_on_the_heating_circuit() -> None:
    """Rinnai code 31 on ``sensor.heating_burning_state`` means space heating."""
    assert classify_burner_state("Heating Water", circuit=CIRCUIT_HEATING) == (
        True,
        KIND_HEATING,
        True,
    )
    assert classify_burner_state("Burning", circuit=CIRCUIT_HEATING) == (
        True,
        KIND_HEATING,
        True,
    )


def test_same_token_on_the_dhw_circuit_is_dhw() -> None:
    """The identical spelling on ``sensor.hot_water_burning_state`` is DHW."""
    assert classify_burner_state("Heating Water", circuit=CIRCUIT_DHW) == (
        True,
        KIND_DHW,
        True,
    )
    assert classify_burner_state("Burning", circuit=CIRCUIT_DHW) == (
        True,
        KIND_DHW,
        True,
    )


def test_shared_token_without_a_circuit_stays_unattributed() -> None:
    assert classify_burner_state("Heating Water") == (True, KIND_UNKNOWN, True)
    assert classify_burner_state("Burning") == (True, KIND_UNKNOWN, True)


def test_rinnai_heating_sensor_makes_cycling_quality_work_end_to_end() -> None:
    """The V0.2 headline feature on the real device's vocabulary.

    Without circuit awareness these burns were all ``unknown``, so
    ``grade_heating_24h`` stayed empty and ``cycling_quality`` was permanently
    ``unknown`` - the feature was inert on the target hardware.
    """
    stats = BurnerStats(circuit=CIRCUIT_HEATING)
    burn(stats, T0, 900.0, kind_state="Heating Water")
    burn(stats, T0 + timedelta(hours=1), 900.0, kind_state="Burning")
    snap = stats.snapshot(T0 + timedelta(hours=2))

    assert snap["heating_starts_24h"] == 2
    assert snap["unknown_kind_starts_24h"] == 0
    assert snap["grade_heating_24h"] == {"good": 2, "fair": 0, "poor": 0}
    assert snap["cycling_quality"] == "good"


def test_rinnai_standby_is_off() -> None:
    assert classify_burner_state("Standby", circuit=CIRCUIT_HEATING) == (False, None, True)
    assert classify_burner_state("Standby", circuit=CIRCUIT_DHW) == (False, None, True)


def test_rinnai_error_is_not_a_burn_on_either_circuit() -> None:
    assert classify_burner_state("Error", circuit=CIRCUIT_HEATING) == (False, None, True)
    assert classify_burner_state("Error", circuit=CIRCUIT_DHW) == (False, None, True)


@pytest.mark.parametrize(
    ("raw", "circuit", "expected"),
    [
        # The Rinnai REB-A2747FF-CH vocabulary, by name and by raw code.
        # "Heating Water" (31) is this device's *space heating* state.
        ("Standby", CIRCUIT_HEATING, (False, None, True)),
        ("30", CIRCUIT_HEATING, (False, None, True)),
        ("Standby", CIRCUIT_DHW, (False, None, True)),
        ("30", CIRCUIT_DHW, (False, None, True)),
        ("Heating Water", CIRCUIT_HEATING, (True, KIND_HEATING, True)),
        ("31", CIRCUIT_HEATING, (True, KIND_HEATING, True)),
        ("Burning", CIRCUIT_HEATING, (True, KIND_HEATING, True)),
        ("32", CIRCUIT_HEATING, (True, KIND_HEATING, True)),
        ("Heating Water", CIRCUIT_DHW, (True, KIND_DHW, True)),
        ("31", CIRCUIT_DHW, (True, KIND_DHW, True)),
        ("Burning", CIRCUIT_DHW, (True, KIND_DHW, True)),
        ("32", CIRCUIT_DHW, (True, KIND_DHW, True)),
        # Error (33) is never a burn, on either circuit.
        ("Error", CIRCUIT_HEATING, (False, None, True)),
        ("33", CIRCUIT_HEATING, (False, None, True)),
        ("Error", CIRCUIT_DHW, (False, None, True)),
        ("33", CIRCUIT_DHW, (False, None, True)),
        # Shared spelling with no circuit: burning, but honestly unattributed.
        ("Heating Water", None, (True, KIND_UNKNOWN, True)),
        ("Burning", None, (True, KIND_UNKNOWN, True)),
        # A closed, specific vocabulary: an unseen token on a known circuit is a
        # burn on that circuit rather than a silent "no data" on every refresh.
        ("some-future-token", CIRCUIT_HEATING, (True, KIND_HEATING, True)),
        ("some-future-token", CIRCUIT_DHW, (True, KIND_DHW, True)),
        ("some-future-token", None, (False, None, False)),
    ],
)
def test_rinnai_vocabulary_matrix(raw, circuit, expected) -> None:
    assert classify_burner_state(raw, circuit=circuit) == expected


def test_no_reading_holds_on_every_circuit() -> None:
    for circuit in (CIRCUIT_HEATING, CIRCUIT_DHW, None):
        for token in (None, "unknown", "unavailable", ""):
            assert classify_burner_state(token, circuit=circuit) == (False, None, False)


def test_dhw_circuit_burns_are_classified_as_dhw() -> None:
    stats = BurnerStats(circuit=CIRCUIT_DHW)
    burn(stats, T0, 900.0, kind_state="Heating Water")
    snap = stats.snapshot(T0 + timedelta(seconds=1000))

    assert snap["dhw_starts_24h"] == 1
    assert snap["heating_starts_24h"] == 0
    assert snap["includes_dhw"] is True


# ---------------------------------------------------------------------------
# V0.2 snapshot keys
# ---------------------------------------------------------------------------
def test_v02_snapshot_keys_exist() -> None:
    snap = BurnerStats().snapshot(T0)
    for key in (
        "cycling_quality",
        "cycling_quality_reason",
        "unexplained_burns_24h",
        "grade_heating_24h",
        "grade_dhw_24h",
    ):
        assert key in snap, f"missing V0.2 snapshot key: {key}"


def test_v01_snapshot_keys_are_not_renamed_or_removed() -> None:
    """Regression: the V0.1 platform layer reads all of these."""
    snap = BurnerStats().snapshot(T0)
    for key in (
        "state",
        "state_known",
        "quality",
        "burning",
        "kind",
        "includes_dhw",
        "last_duration",
        "last_duration_kind",
        "last_started_at",
        "last_ended_at",
        "current_duration",
        "starts_1h",
        "starts_24h",
        "starts_total",
        "heating_starts_24h",
        "dhw_starts_24h",
        "unknown_kind_starts_24h",
        "burns_24h",
        "runtime_24h",
        "heating_runtime_24h",
        "dhw_runtime_24h",
        "short_cycles_24h",
        "short_cycle_alert",
        "short_cycle_seconds",
        "histogram",
        "histogram_heating",
        "histogram_window_seconds",
        "demand_active",
        "burns_without_demand_24h",
        "persist",
    ):
        assert key in snap, f"V0.1 snapshot key disappeared: {key}"


def test_cycling_quality_is_unknown_without_heating_burns() -> None:
    """Nothing observed must not read as a fault."""
    snap = BurnerStats().snapshot(T0)
    assert snap["cycling_quality"] == QUALITY_UNKNOWN
    assert "no heating burns" in snap["cycling_quality_reason"]


def test_cycling_quality_grades_on_the_average_heating_burn() -> None:
    stats = BurnerStats()
    burn(stats, T0, 900.0)  # 15 min -> good (>= 600 s)
    burn(stats, T0 + timedelta(hours=1), 900.0)
    snap = stats.snapshot(T0 + timedelta(hours=2))

    assert snap["cycling_quality"] == QUALITY_GOOD
    assert snap["cycling_quality_reason"] == "average 900s over 2 heating burns"


def test_cycling_quality_fair_band() -> None:
    stats = BurnerStats()
    burn(stats, T0, 400.0)  # between poor (300) and good (600)
    snap = stats.snapshot(T0 + timedelta(seconds=500))
    assert snap["cycling_quality"] == QUALITY_FAIR


def test_cycling_quality_poor_band() -> None:
    stats = BurnerStats()
    burn(stats, T0, 120.0)  # under 300 s
    snap = stats.snapshot(T0 + timedelta(seconds=200))
    assert snap["cycling_quality"] == QUALITY_POOR


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (299.9, QUALITY_POOR),
        (300.0, QUALITY_FAIR),  # inclusive lower bound
        (599.9, QUALITY_FAIR),
        (600.0, QUALITY_GOOD),  # inclusive lower bound
        (3600.0, QUALITY_GOOD),
    ],
)
def test_cycling_quality_band_boundaries(duration: float, expected: str) -> None:
    stats = BurnerStats()
    burn(stats, T0, duration)
    assert stats.snapshot(T0 + timedelta(seconds=duration + 60))["cycling_quality"] == expected


def test_cycling_quality_thresholds_are_configurable() -> None:
    stats = BurnerStats(cycling_good_seconds=1200.0, cycling_poor_seconds=600.0)
    burn(stats, T0, 900.0)
    snap = stats.snapshot(T0 + timedelta(seconds=1000))
    assert snap["cycling_quality"] == QUALITY_FAIR


# ---------------------------------------------------------------------------
# SAFETY: DHW must not pull the heating grade down
# ---------------------------------------------------------------------------
def test_dhw_burn_does_not_lower_the_heating_quality() -> None:
    """A shower must not be able to downgrade the space-heating verdict."""
    stats = BurnerStats()
    burn(stats, T0, 900.0, kind_state="heating")  # good
    burn(stats, T0 + timedelta(hours=1), 60.0, kind_state="dhw")  # very short
    snap = stats.snapshot(T0 + timedelta(hours=2))

    assert snap["cycling_quality"] == QUALITY_GOOD
    assert "over 1 heating burns" in snap["cycling_quality_reason"]
    # The DHW burn is graded separately instead.
    assert snap["grade_heating_24h"] == {"good": 1, "fair": 0, "poor": 0}
    assert snap["grade_dhw_24h"] == {"good": 0, "fair": 0, "poor": 1}


def test_dhw_only_window_leaves_heating_quality_unknown() -> None:
    stats = BurnerStats()
    burn(stats, T0, 60.0, kind_state="dhw")
    snap = stats.snapshot(T0 + timedelta(seconds=200))

    assert snap["cycling_quality"] == QUALITY_UNKNOWN
    assert snap["grade_heating_24h"] == {"good": 0, "fair": 0, "poor": 0}
    assert snap["grade_dhw_24h"] == {"good": 0, "fair": 0, "poor": 1}


def test_unattributable_burns_still_feed_the_heating_grade() -> None:
    """A generic ``on`` burner cannot be proven DHW, so it counts as heating."""
    stats = BurnerStats()
    burn(stats, T0, 900.0, kind_state="on")
    snap = stats.snapshot(T0 + timedelta(seconds=1000))

    assert snap["cycling_quality"] == QUALITY_GOOD
    assert snap["grade_heating_24h"] == {"good": 1, "fair": 0, "poor": 0}
    assert snap["includes_dhw"] is True


def test_grade_histograms_always_expose_all_three_bands() -> None:
    stats = BurnerStats()
    snap = stats.snapshot(T0)
    for key in ("grade_heating_24h", "grade_dhw_24h"):
        assert set(snap[key]) == {QUALITY_GOOD, QUALITY_FAIR, QUALITY_POOR}


def test_grades_mix_bands_correctly() -> None:
    stats = BurnerStats()
    burn(stats, T0, 900.0)  # good
    burn(stats, T0 + timedelta(hours=1), 400.0)  # fair
    burn(stats, T0 + timedelta(hours=2), 120.0)  # poor
    snap = stats.snapshot(T0 + timedelta(hours=3))

    assert snap["grade_heating_24h"] == {"good": 1, "fair": 1, "poor": 1}
    # Average = 473 s -> fair.
    assert snap["cycling_quality"] == QUALITY_FAIR


def test_grades_respect_the_24h_window() -> None:
    stats = BurnerStats()
    burn(stats, T0, 900.0)
    snap = stats.snapshot(T0 + timedelta(hours=48))

    assert snap["grade_heating_24h"] == {"good": 0, "fair": 0, "poor": 0}
    assert snap["cycling_quality"] == QUALITY_UNKNOWN


# ---------------------------------------------------------------------------
# unexplained_burns_24h
# ---------------------------------------------------------------------------
def test_unexplained_burns_24h_aliases_burns_without_demand() -> None:
    stats = BurnerStats()
    stats.note_demand(T0, False)
    burn(stats, T0 + timedelta(seconds=10), 300.0)
    snap = stats.snapshot(T0 + timedelta(seconds=400))

    assert snap["unexplained_burns_24h"] == 1
    assert snap["unexplained_burns_24h"] == snap["burns_without_demand_24h"]


def test_unexplained_burns_is_zero_when_demand_existed() -> None:
    stats = BurnerStats()
    stats.note_demand(T0, True)
    burn(stats, T0 + timedelta(seconds=10), 300.0)
    assert stats.snapshot(T0 + timedelta(seconds=400))["unexplained_burns_24h"] == 0


def test_note_demand_flag_survives_note_state() -> None:
    """The coordinator calls note_demand(now, ...) then note_state(source, now)."""
    stats = BurnerStats()
    stats.note_demand(T0, False)
    stats.note_state("heating", T0 + timedelta(seconds=10))
    snap = stats.snapshot(T0 + timedelta(seconds=400))

    assert snap["demand_active"] is False
    assert snap["unexplained_burns_24h"] == 1


# ---------------------------------------------------------------------------
# short_cycle_alert semantics are unchanged
# ---------------------------------------------------------------------------
def test_short_cycle_alert_semantics_unchanged_by_v02() -> None:
    """Heating + unattributable only, never DHW; alarm at the same threshold."""
    stats = BurnerStats(short_cycle_seconds=300.0, circuit=CIRCUIT_HEATING)
    burn(stats, T0, 60.0, kind_state="Burning")
    snap = stats.snapshot(T0 + timedelta(seconds=120))

    assert snap["short_cycles_24h"] == 1
    assert snap["short_cycle_alert"] is True


def test_dhw_short_burn_still_raises_no_alert_under_v02() -> None:
    stats = BurnerStats(short_cycle_seconds=300.0, circuit=CIRCUIT_DHW)
    burn(stats, T0, 60.0, kind_state="Heating Water")
    snap = stats.snapshot(T0 + timedelta(seconds=120))

    assert snap["short_cycles_24h"] == 0
    assert snap["short_cycle_alert"] is False
    # ...while still being graded as DHW quality.
    assert snap["grade_dhw_24h"] == {"good": 0, "fair": 0, "poor": 1}


# ---------------------------------------------------------------------------
# Persistence: V0.1 payloads and the new error flag
# ---------------------------------------------------------------------------
def test_restore_of_a_v01_payload_without_new_fields() -> None:
    """An upgrade must not break on state written by V0.1."""
    stats = BurnerStats()
    stats.restore(
        {
            "state": "off",
            "burning": False,
            "kind": None,
            "unknown": False,
            "burn_start": None,
            "last_duration": 900.0,
            "last_duration_kind": KIND_HEATING,
            "starts_total": 2,
            "demand_active": True,
            "starts": [[T0.isoformat(), KIND_HEATING]],
            "cycles": [[(T0 + timedelta(seconds=900)).isoformat(), 900.0, KIND_HEATING]],
            "burns_without_demand": [],
        }
    )
    snap = stats.snapshot(T0 + timedelta(hours=1))
    assert snap["starts_total"] == 2
    assert snap["last_duration"] == pytest.approx(900.0)
    assert snap["grade_heating_24h"] == {"good": 1, "fair": 0, "poor": 0}


def test_v02_snapshot_restore_round_trip_keeps_quality() -> None:
    stats = BurnerStats(circuit=CIRCUIT_HEATING)
    burn(stats, T0, 900.0, kind_state="Burning")
    burn(stats, T0 + timedelta(hours=1), 120.0, kind_state="Burning")
    now = T0 + timedelta(hours=2)

    restored = BurnerStats(circuit=CIRCUIT_HEATING)
    restored.restore(stats.snapshot(now))
    before = stats.snapshot(now)
    after = restored.snapshot(now)

    for key in (
        "cycling_quality",
        "cycling_quality_reason",
        "unexplained_burns_24h",
        "grade_heating_24h",
        "grade_dhw_24h",
        "short_cycle_alert",
    ):
        assert after[key] == before[key], key


def test_restore_round_trips_the_error_flag() -> None:
    stats = BurnerStats()
    stats.note_state("Error", T0)

    restored = BurnerStats()
    restored.restore(stats.snapshot(T0 + timedelta(seconds=10)))
    assert restored.snapshot(T0 + timedelta(seconds=20))["error_state"] is True


def test_restore_rederives_the_error_flag_for_a_v01_payload() -> None:
    """V0.1 had no ``error`` key; the last state still tells us."""
    stats = BurnerStats()
    stats.restore({"state": "Error", "burning": False})
    assert stats.snapshot(T0)["error_state"] is True


def test_restore_drops_a_phantom_burn_left_open_by_an_error_state() -> None:
    """A torn payload must not resurrect a burn that an error already ended."""
    stats = BurnerStats()
    stats.restore(
        {
            "state": "Error",
            "error": True,
            "burning": True,
            "kind": KIND_HEATING,
            "burn_start": T0.isoformat(),
        }
    )
    snap = stats.snapshot(T0 + timedelta(seconds=60))
    assert snap["burning"] is False
    assert snap["current_duration"] is None


def test_v02_restore_is_tolerant_of_garbage() -> None:
    stats = BurnerStats(circuit=CIRCUIT_HEATING)
    stats.restore({})
    stats.restore({"error": "yes", "cycles": "nonsense", "starts": 5})
    snap = stats.snapshot(T0)
    assert snap["cycling_quality"] == QUALITY_UNKNOWN
    assert snap["grade_dhw_24h"] == {"good": 0, "fair": 0, "poor": 0}
    assert snap["unexplained_burns_24h"] == 0
