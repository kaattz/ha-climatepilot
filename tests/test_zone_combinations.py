"""Unit tests for the zone-combination tracker (docs/INTERFACES_V0.2.md 3.1).

The behavioural rules under test:

* a combination is keyed by the *sorted set* of demanding zones, so ``{a, b}``
  and ``{b, a}`` are one combination and the key is stable;
* a change must persist for ``min_dwell`` before it is accepted, and the switch
  is back-dated to when the change was first seen;
* a completed interval shorter than ``min_duration`` is folded into the
  preceding interval instead of being counted as a combination of its own;
* the report window rolls: old intervals drop out and a straddling interval
  contributes only its in-window part;
* ``COMBINATION_NONE`` means exactly "nobody was demanding heat" - never
  "there was demand but no pattern won";
* the empty set is still reported as its own group with ``share == 0.0``;
* all state survives a restart, and a corrupt payload never raises.
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# Import isolation via the shared alias loader (see tests/_engine_loader.py).
# The engine is loaded under an alias package name so importing it never runs
# ``custom_components/climatepilot/__init__.py`` (which pulls in Home Assistant
# and the coordinator).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _engine_loader import load_engine

zc = load_engine("zone_combinations")
_const = load_engine("const")

COMBINATION_NONE = _const.COMBINATION_NONE
DEFAULT_COMBINATION_DOMINANT_SHARE = _const.DEFAULT_COMBINATION_DOMINANT_SHARE
DEFAULT_COMBINATION_MIN_DURATION = _const.DEFAULT_COMBINATION_MIN_DURATION
DEFAULT_COMBINATION_MIN_DWELL = _const.DEFAULT_COMBINATION_MIN_DWELL
DEFAULT_COMBINATION_WINDOW = _const.DEFAULT_COMBINATION_WINDOW

CombinationObservation = zc.CombinationObservation
CombinationStat = zc.CombinationStat
ZoneCombinationTracker = zc.ZoneCombinationTracker

T0 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
MODULE_PATH = Path(zc.__file__)

LIVING = "living_room"
BEDROOM = "master_bedroom"
OFFICE = "office"


def at(seconds: float) -> datetime:
    """Return ``T0`` shifted by ``seconds``."""
    return T0 + timedelta(seconds=seconds)


def tracker(**kwargs) -> ZoneCombinationTracker:
    """Build a tracker with test-friendly, explicit parameters."""
    params = {"min_dwell": 120.0, "min_duration": 60.0, "window": 86400.0}
    params.update(kwargs)
    return ZoneCombinationTracker(**params)


def settle_dwell(trk: ZoneCombinationTracker, zones, start: float) -> None:
    """Adopt ``zones`` at ``start`` and confirm it through the dwell filter.

    The confirming observation is issued exactly one dwell later, so the switch
    is back-dated to ``start`` (a change is real when it was *first* seen, not
    when the dwell confirmed it).
    """
    trk.note_demand(zones, at(start))
    trk.note_demand(zones, at(start + DEFAULT_COMBINATION_MIN_DWELL))


def keys(report) -> set[str]:
    return {stat.key for stat in report.stats}


def combo(*zones: str) -> str:
    """The expected key for ``zones`` - sorted, joined (the engine's rule)."""
    return COMBINATION_NONE if not zones else "+".join(sorted(zones))


def stat_for(report, key: str) -> CombinationStat:
    for stat in report.stats:
        if stat.key == key:
            return stat
    raise AssertionError(f"no stat for {key!r} in {keys(report)}")


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
    source = inspect.getsource(zc)
    assert f"datetime.{forbidden}(" not in source
    assert f"time.{forbidden}(" not in source


def test_defaults_come_from_const() -> None:
    """The frozen V0.2 constants are the defaults, not locally invented numbers."""
    signature = inspect.signature(ZoneCombinationTracker.__init__)
    assert signature.parameters["min_dwell"].default == DEFAULT_COMBINATION_MIN_DWELL
    assert signature.parameters["min_duration"].default == DEFAULT_COMBINATION_MIN_DURATION
    assert signature.parameters["window"].default == DEFAULT_COMBINATION_WINDOW
    assert signature.parameters["dominant_share"].default == DEFAULT_COMBINATION_DOMINANT_SHARE


# ---------------------------------------------------------------------------
# Combination keys are order-insensitive
# ---------------------------------------------------------------------------
def test_key_is_independent_of_zone_order() -> None:
    forward = tracker()
    settle_dwell(forward, [LIVING, BEDROOM], 0)
    backward = tracker()
    settle_dwell(backward, [BEDROOM, LIVING], 0)

    # Lexical sort on the real ids: "living_room" < "master_bedroom".
    assert forward.report(at(1000)).dominant_key == "living_room+master_bedroom"
    assert backward.report(at(1000)).dominant_key == "living_room+master_bedroom"


def test_order_swap_is_not_a_change() -> None:
    """Reordering the same set must not open a new interval."""
    trk = tracker()
    trk.note_demand([LIVING, BEDROOM], at(0))
    trk.note_demand([BEDROOM, LIVING], at(500))
    trk.note_demand([LIVING, BEDROOM], at(900))

    report = trk.report(at(1000))
    assert report.distinct_count == 1
    assert stat_for(report, combo(LIVING, BEDROOM)).count == 1
    assert report.current_seconds == pytest.approx(1000.0)


def test_observation_properties() -> None:
    open_obs = CombinationObservation(zones=frozenset({LIVING}), started_at=T0, ended_at=None)
    assert open_obs.duration == 0.0
    assert open_obs.key == LIVING
    assert open_obs.zone_ids == (LIVING,)

    empty = CombinationObservation(zones=frozenset(), started_at=T0, ended_at=at(300))
    assert empty.key == COMBINATION_NONE
    assert empty.zone_ids == ()
    assert empty.duration == pytest.approx(300.0)

    multi = CombinationObservation(
        zones=frozenset({OFFICE, LIVING}), started_at=T0, ended_at=at(60)
    )
    assert multi.zone_ids == (LIVING, OFFICE)
    assert multi.key == f"{LIVING}+{OFFICE}"


# ---------------------------------------------------------------------------
# Dwell filter
# ---------------------------------------------------------------------------
def test_first_reading_is_adopted_without_waiting_for_the_dwell() -> None:
    trk = tracker()
    trk.note_demand([LIVING], at(0))
    report = trk.report(at(10))
    assert report.current_key == LIVING
    assert report.current_seconds == pytest.approx(10.0)


def test_change_is_ignored_until_the_dwell_elapses() -> None:
    trk = tracker(min_dwell=120.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([LIVING, BEDROOM], at(1000))
    trk.note_demand([LIVING, BEDROOM], at(1100))  # only 100 s of dwell

    report = trk.report(at(1100))
    assert report.current_key == LIVING
    # The candidate combination never entered the tally.
    assert keys(report) == {LIVING}


def test_change_is_accepted_once_the_dwell_elapses() -> None:
    trk = tracker(min_dwell=120.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([LIVING, BEDROOM], at(1000))
    trk.note_demand([LIVING, BEDROOM], at(1120))

    report = trk.report(at(1500))
    assert report.current_key == combo(LIVING, BEDROOM)
    assert report.current_seconds == pytest.approx(500.0)


def test_flicker_that_reverts_within_the_dwell_is_never_recorded() -> None:
    """KNX chatter must not create combinations (design doc 8)."""
    trk = tracker(min_dwell=120.0)
    trk.note_demand([LIVING], at(0))
    # Blink to another set three times, each for 50 s - well under the dwell.
    for offset in (100, 300, 500):
        trk.note_demand([LIVING, BEDROOM], at(offset))
        trk.note_demand([LIVING], at(offset + 50))

    report = trk.report(at(1000))
    assert keys(report) == {LIVING}
    assert report.current_key == LIVING
    assert report.current_seconds == pytest.approx(1000.0)


def test_dwell_switch_is_back_dated_to_the_first_observation() -> None:
    """The interval starts when demand changed, not when the dwell confirmed it."""
    trk = tracker(min_dwell=120.0, min_duration=60.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(1000))
    trk.note_demand([BEDROOM], at(1120))  # confirmed 120 s after it was first seen

    report = trk.report(at(2000))
    assert stat_for(report, LIVING).total_seconds == pytest.approx(1000.0)
    assert stat_for(report, BEDROOM).total_seconds == pytest.approx(1000.0)


def test_zero_dwell_switches_immediately() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(100))
    assert trk.report(at(200)).current_key == BEDROOM


# ---------------------------------------------------------------------------
# Short intervals
# ---------------------------------------------------------------------------
def test_short_interval_is_folded_into_the_previous_one() -> None:
    """A 150 s blip between two long stretches must not split the pattern."""
    trk = tracker(min_dwell=120.0, min_duration=300.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(1000))
    trk.note_demand([], at(1120))  # closes [LIVING] and opens the 150 s blip
    trk.note_demand([LIVING], at(1150))
    trk.note_demand([LIVING], at(1270))  # closes the blip (< 300 s) and reopens

    report = trk.report(at(2000))
    assert report.distinct_count == 1
    assert keys(report) == {LIVING}
    # Folded into the predecessor and then merged back into one interval.
    assert stat_for(report, LIVING).count == 1
    assert stat_for(report, LIVING).total_seconds == pytest.approx(2000.0)


def test_short_interval_without_a_predecessor_is_dropped() -> None:
    """No predecessor: the noise span is not attributed to anything."""
    trk = tracker(min_dwell=120.0, min_duration=300.0)
    trk.note_demand([], at(0))
    trk.note_demand([LIVING], at(100))
    trk.note_demand([LIVING], at(220))  # closes a 100 s empty interval

    report = trk.report(at(1000))
    assert COMBINATION_NONE not in keys(report)
    assert report.total_demand_seconds == pytest.approx(900.0)


def test_interval_at_the_min_duration_is_kept() -> None:
    """The threshold is exclusive: exactly ``min_duration`` is a real interval."""
    trk = tracker(min_dwell=0.0, min_duration=300.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(300))

    report = trk.report(at(600))
    assert keys(report) == {LIVING, BEDROOM}
    assert stat_for(report, LIVING).total_seconds == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Window rolling
# ---------------------------------------------------------------------------
def test_old_observations_roll_out_of_the_window() -> None:
    trk = tracker(window=3600.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(1000))
    trk.note_demand([BEDROOM], at(1120))

    fresh = trk.report(at(2000))
    assert keys(fresh) == {LIVING, BEDROOM}

    # By now the LIVING interval (0-1000) is entirely older than now - 3600,
    # and the still-open BEDROOM interval counts only its in-window part.
    rolled = trk.report(at(5000))
    assert keys(rolled) == {BEDROOM}
    assert rolled.distinct_count == 1
    assert rolled.dominant_key == BEDROOM
    assert rolled.total_demand_seconds == pytest.approx(3600.0)


def test_straddling_interval_contributes_only_its_in_window_part() -> None:
    trk = tracker(min_dwell=0.0, window=3600.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(2000))  # closes [LIVING] at 2000 (duration 2000 s)

    report = trk.report(at(5000))  # cutoff at 1400
    assert stat_for(report, LIVING).total_seconds == pytest.approx(600.0)


def test_interval_ending_exactly_at_the_cutoff_contributes_nothing() -> None:
    trk = tracker(min_dwell=0.0, window=1000.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(500))  # the interval is [0, 500]

    # now = 1499 -> cutoff 499: one second of the interval is still in-window.
    assert LIVING in keys(trk.report(at(1499)))
    # now = 1500 -> cutoff 500: the interval ends exactly at the cutoff.
    assert LIVING not in keys(trk.report(at(1500)))


def test_closed_events_are_pruned_out_of_the_snapshot() -> None:
    trk = tracker(min_dwell=0.0, window=600.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(60))
    trk.note_demand([], at(120))

    report = trk.report(at(10000))
    # Only the (empty) interval that is still open survives; everything closed
    # long before the cutoff is gone rather than lingering in memory.
    assert keys(report) == {COMBINATION_NONE}
    assert report.distinct_count == 0
    assert trk.snapshot()["observations"] == []


# ---------------------------------------------------------------------------
# The empty set: its own group, share 0, never "dominant"
# ---------------------------------------------------------------------------
def test_empty_set_is_reported_as_its_own_group_with_zero_share() -> None:
    trk = tracker(window=3600.0)
    trk.note_demand([], at(0))

    report = trk.report(at(3600))
    assert keys(report) == {COMBINATION_NONE}
    assert report.dominant_key == COMBINATION_NONE
    assert report.dominant_share == 0.0
    assert report.total_demand_seconds == 0.0
    assert report.distinct_count == 0
    assert report.total_groups == 1
    assert stat_for(report, COMBINATION_NONE).share == 0.0
    assert stat_for(report, COMBINATION_NONE).zone_ids == ()
    assert stat_for(report, COMBINATION_NONE).total_seconds == pytest.approx(3600.0)


def test_no_demand_time_keeps_share_at_zero_even_with_demand_elsewhere() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(1000))
    trk.note_demand([LIVING], at(2000))

    report = trk.report(at(3000))
    empty = stat_for(report, COMBINATION_NONE)
    assert empty.share == 0.0
    # Demand time is the denominator, and the empty set is not part of it.
    assert report.total_demand_seconds == pytest.approx(2000.0)
    assert sum(stat.share for stat in report.stats) == pytest.approx(1.0)


def test_empty_set_does_not_inflate_distinct_count() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([], at(0))
    trk.note_demand([LIVING], at(1000))
    trk.note_demand([BEDROOM], at(2000))

    report = trk.report(at(3000))
    assert keys(report) == {COMBINATION_NONE, LIVING, BEDROOM}
    assert report.distinct_count == 2  # demand combinations only
    assert report.total_groups == 3  # stats entries, empty set included


# ---------------------------------------------------------------------------
# Dominant combination semantics (Lead ruling: COMBINATION_NONE == no demand)
# ---------------------------------------------------------------------------
def test_dominant_key_is_the_busiest_combination() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([LIVING, BEDROOM], at(1000))
    trk.note_demand([], at(4000))

    report = trk.report(at(5000))
    assert report.dominant_key == combo(LIVING, BEDROOM)
    assert report.dominant_share == pytest.approx(3000.0 / 4000.0)


def test_dominant_key_is_none_if_and_only_if_the_window_has_no_demand() -> None:
    """``COMBINATION_NONE`` has exactly one meaning: no demand in the window.

    A sentinel carrying two meanings ("no demand" *and* "no pattern won") could
    not be branched on by the UI - the same class of error as treating
    ``unknown`` as "no demand" in V0.1.
    """
    without_demand = tracker(min_dwell=0.0, window=3600.0)
    without_demand.note_demand([], at(0))
    report = without_demand.report(at(3600))
    assert report.dominant_key == COMBINATION_NONE
    assert report.total_demand_seconds == 0.0

    # Demand spread across four equal patterns, so no single one reaches the
    # 0.30 threshold... yet the key must still name the leader.
    spread = tracker(min_dwell=0.0, window=86400.0, dominant_share=0.30)
    spread.note_demand([LIVING], at(0))
    spread.note_demand([BEDROOM], at(1000))
    spread.note_demand([OFFICE], at(2000))
    spread.note_demand([LIVING, BEDROOM, OFFICE], at(3000))
    spread_report = spread.report(at(4000))

    assert spread_report.total_demand_seconds > 0.0
    assert spread_report.dominant_key != COMBINATION_NONE
    assert spread_report.dominant_key == LIVING  # tie at 1000 s -> lowest key wins
    assert spread_report.dominant_share == pytest.approx(0.25)
    # The "is this really a pattern?" question is answered separately.
    assert spread_report.has_dominant_mode is False


def test_dominant_tie_break_is_deterministic() -> None:
    """Equal totals must not swap places between refreshes."""
    first = tracker(min_dwell=0.0, window=86400.0)
    first.note_demand([LIVING], at(0))
    first.note_demand([BEDROOM], at(1000))

    second = tracker(min_dwell=0.0, window=86400.0)
    second.note_demand([BEDROOM], at(0))
    second.note_demand([LIVING], at(1000))

    a = first.report(at(2000))
    b = second.report(at(2000))
    assert a.dominant_key == b.dominant_key == LIVING  # equal totals, lowest key
    assert [s.key for s in a.stats] == [s.key for s in b.stats]


def test_has_dominant_mode_uses_the_configured_threshold() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0, dominant_share=0.30)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(8000))

    report = trk.report(at(10000))
    assert report.dominant_key == LIVING
    assert report.dominant_share == pytest.approx(0.8)
    assert report.dominant_threshold == pytest.approx(0.30)
    assert report.has_dominant_mode is True

    strict = tracker(min_dwell=0.0, window=86400.0, dominant_share=0.95)
    strict.note_demand([LIVING], at(0))
    strict.note_demand([BEDROOM], at(8000))
    assert strict.report(at(10000)).has_dominant_mode is False


def test_report_carries_the_tracker_threshold() -> None:
    trk = tracker(dominant_share=0.75)
    assert trk.dominant_threshold == pytest.approx(0.75)
    assert trk.report(at(0)).dominant_threshold == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def test_stats_are_sorted_by_total_seconds_descending() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(1000))
    trk.note_demand([LIVING, BEDROOM], at(5000))

    report = trk.report(at(6000))
    # BEDROOM 4 000 s, then the 1 000 s tie broken by key ("living_room" is a
    # prefix of "living_room+master_bedroom", so it sorts first).
    assert [s.key for s in report.stats] == [BEDROOM, LIVING, combo(LIVING, BEDROOM)]


def test_stat_average_and_longest_seconds() -> None:
    trk = tracker(min_dwell=0.0, min_duration=0.0, window=86400.0)
    # Two separate LIVING intervals: 1 000 s and 3 000 s.
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(1000))
    trk.note_demand([LIVING], at(2000))
    trk.note_demand([], at(5000))

    stat = stat_for(trk.report(at(6000)), LIVING)
    assert stat.count == 2
    assert stat.total_seconds == pytest.approx(4000.0)
    assert stat.average_seconds == pytest.approx(2000.0)
    assert stat.longest_seconds == pytest.approx(3000.0)


def test_fresh_tracker_reports_no_data_safely() -> None:
    report = tracker().report(T0)
    assert report.dominant_key == COMBINATION_NONE
    assert report.dominant_share == 0.0
    assert report.distinct_count == 0
    assert report.total_demand_seconds == 0.0
    assert report.stats == ()
    assert report.total_groups == 0
    assert report.current_key == COMBINATION_NONE
    assert report.current_seconds == 0.0
    assert report.has_dominant_mode is False


def test_report_is_repeatable_for_the_same_instant() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING], at(0))
    assert trk.report(at(1000)) == trk.report(at(1000))


def test_current_seconds_grows_without_adding_history() -> None:
    """Repeated reports must not append the same open interval over and over."""
    trk = tracker(min_dwell=0.0, min_duration=0.0)
    trk.note_demand([LIVING], at(0))
    assert trk.report(at(500)).current_seconds == pytest.approx(500.0)
    assert trk.report(at(900)).current_seconds == pytest.approx(900.0)
    # One interval either way - the report is a read, not a mutation.
    assert trk.report(at(900)).distinct_count == 1
    assert trk.report(at(900)).stats == trk.report(at(900)).stats


# ---------------------------------------------------------------------------
# as_dict export
# ---------------------------------------------------------------------------
def test_as_dict_is_json_serializable_and_complete() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING, BEDROOM], at(0))
    trk.note_demand([], at(2000))

    payload = trk.report(at(3000)).as_dict()
    json.dumps(payload)  # must not raise

    assert payload["dominant_key"] == combo(LIVING, BEDROOM)
    assert payload["current_key"] == COMBINATION_NONE
    assert payload["distinct_count"] == 1
    assert payload["total_groups"] == 2
    assert payload["has_dominant_mode"] == (
        payload["dominant_share"] >= payload["dominant_threshold"]
    )
    entry = payload["stats"][0]
    assert entry["key"] == combo(LIVING, BEDROOM)
    assert entry["zone_ids"] == sorted([BEDROOM, LIVING])
    assert entry["count"] == 1


def test_as_dict_of_an_empty_report_is_safe() -> None:
    payload = tracker().report(T0).as_dict()
    json.dumps(payload)
    assert payload["stats"] == []
    assert payload["total_groups"] == 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def test_snapshot_restore_round_trip_preserves_observations() -> None:
    trk = tracker(min_dwell=0.0, window=86400.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([LIVING, BEDROOM], at(1000))
    now = at(2000)

    restored = tracker(min_dwell=0.0, window=86400.0)
    restored.restore(trk.snapshot())

    assert restored.report(now) == trk.report(now)
    assert restored.snapshot() == trk.snapshot()


def test_restore_keeps_an_open_interval_and_its_zone_set() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand([LIVING, BEDROOM], at(0))

    restored = tracker()
    restored.restore(trk.snapshot())

    report = restored.report(at(600))
    assert report.current_key == combo(LIVING, BEDROOM)
    assert report.current_seconds == pytest.approx(600.0)
    assert restored.snapshot()["current_zones"] == sorted([LIVING, BEDROOM])


def test_restore_keeps_a_pending_candidate() -> None:
    """A dwell in progress must survive a restart, or the dwell restarts."""
    trk = tracker(min_dwell=120.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(1000))  # pending, not yet confirmed

    restored = tracker(min_dwell=120.0)
    restored.restore(trk.snapshot())
    assert restored.report(at(1050)).current_key == LIVING

    # The dwell clock kept running: 120 s after 1000 the switch lands.
    restored.note_demand([BEDROOM], at(1120))
    assert restored.report(at(1200)).current_key == BEDROOM
    # Back-dated to 1000, not to 1120.
    assert stat_for(restored.report(at(2000)), LIVING).total_seconds == pytest.approx(1000.0)


def test_restore_of_an_open_interval_without_a_start_time_is_dropped() -> None:
    trk = tracker()
    trk.restore(
        {
            "current_zones": [LIVING],
            "current_started_at": None,
            "pending_zones": None,
            "pending_since": None,
            "observations": [],
        }
    )
    report = trk.report(T0)
    assert report.current_key == COMBINATION_NONE
    assert report.current_seconds == 0.0


def test_restore_drops_a_pending_candidate_without_a_timestamp() -> None:
    trk = tracker(min_dwell=120.0)
    trk.restore(
        {
            "current_zones": [LIVING],
            "current_started_at": "2026-01-15T08:00:00+00:00",
            "pending_zones": [BEDROOM],
            "pending_since": "junk",
            "observations": [],
        }
    )
    # The candidate lost its clock, so it starts over instead of switching on
    # an unproven dwell.
    trk.note_demand([BEDROOM], at(100))
    assert trk.report(at(150)).current_key == LIVING


def test_restore_is_tolerant_of_garbage() -> None:
    trk = tracker()
    trk.restore(None)
    trk.restore({})
    trk.restore(
        {
            "current_zones": 5,
            "current_started_at": "not-a-date",
            "pending_zones": "not-a-list",
            "pending_since": 12345,
            "observations": "definitely not a list",
        }
    )
    assert trk.report(T0).dominant_key == COMBINATION_NONE


def test_restore_skips_malformed_observations_but_keeps_valid_ones() -> None:
    trk = tracker()
    trk.restore(
        {
            "current_started_at": "2026-01-15T08:00:00+00:00",
            "current_zones": ["", None, "  ", LIVING],
            "observations": [
                "junk",
                [],
                ["not-a-list", "start", "end"],
                [[LIVING], "bad-start", "2026-01-15T09:00:00+00:00"],
                [[LIVING], "2026-01-15T09:00:00+00:00", "2026-01-15T08:00:00+00:00"],
                [[BEDROOM], "2026-01-15T08:00:00+00:00", "2026-01-15T09:00:00+00:00"],
            ],
        }
    )
    report = trk.report(at(7200))
    # Blank/garbage zone names are dropped; the one well-formed interval
    # survives alongside the restored open interval.
    assert report.current_key == LIVING
    assert keys(report) == {BEDROOM, LIVING}
    assert stat_for(report, BEDROOM).total_seconds == pytest.approx(3600.0)


def test_restore_never_raises_on_hostile_types() -> None:
    trk = tracker()
    for payload in (
        {"current_started_at": ["a", "b"]},
        {"current_zones": {"a": 1}},
        {"pending_since": ["x"]},
        {"observations": [42, None, {"a": 1}]},
        {"observations": [["a", "b", "c", "d", "e"]]},
        {"current_started_at": "2026-01-15T08:00:00+00:00", "observations": 7},
    ):
        trk.restore(payload)  # must not raise
    assert trk.report(T0).dominant_key == COMBINATION_NONE


def test_restored_observations_stay_bounded_by_the_window() -> None:
    trk = tracker(min_dwell=0.0, window=600.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([BEDROOM], at(100))
    trk.note_demand([LIVING], at(200))
    trk.note_demand([], at(300))

    restored = tracker(min_dwell=0.0, window=600.0)
    restored.restore(trk.snapshot())
    restored.report(at(100000))  # far future prunes everything
    assert restored.snapshot()["observations"] == []


# ---------------------------------------------------------------------------
# Malformed caller input
# ---------------------------------------------------------------------------
def test_note_demand_accepts_any_iterable() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand((zone for zone in [LIVING, BEDROOM]), at(0))
    assert trk.report(at(10)).current_key == combo(LIVING, BEDROOM)


def test_note_demand_drops_blank_and_none_zone_ids() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand([None, "", "   ", LIVING], at(0))  # type: ignore[list-item]
    report = trk.report(at(10))
    assert report.current_key == LIVING
    assert report.stats[0].zone_ids == (LIVING,)


def test_note_demand_with_none_carries_no_information() -> None:
    """``None`` is not a reading: it must never be read as "no demand"."""
    trk = tracker(min_dwell=0.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand(None, at(100))  # type: ignore[arg-type]
    assert trk.report(at(200)).current_key == LIVING


def test_note_demand_with_a_non_iterable_is_ignored() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand(42, at(100))  # type: ignore[arg-type]
    assert trk.report(at(200)).current_key == LIVING


def test_empty_iterable_is_a_real_no_demand_reading() -> None:
    trk = tracker(min_dwell=0.0)
    trk.note_demand([LIVING], at(0))
    trk.note_demand([], at(100))
    assert trk.report(at(200)).current_key == COMBINATION_NONE
