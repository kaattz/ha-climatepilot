"""Gas and degree-day accumulator: rollovers, missing samples, meter resets.

The engine is a pure module (no Home Assistant import), so these tests inject
``now`` and never touch the wall clock.

The recurring theme: every one of these tests exists because the naive
implementation produces a *plausible wrong number* rather than an error. Counting
a missing temperature as 0 C, or letting a replaced meter report a negative
delta, both yield figures that look like real measurements.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climatepilot.energy_monitor import EnergyMonitor

BASE = 15.5


def at(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Degree days
# ---------------------------------------------------------------------------


def test_degree_days_accumulate_over_a_full_day() -> None:
    """10.5 C below base for 24 h is 10.5 degree-days.

    The integral is over *time*, not over samples. An implementation that
    averaged the samples and multiplied by a fixed day length would pass a
    constant-temperature test by accident and fail the moment the weather moves,
    so the arithmetic has to be dt-weighted and this is the number that proves it.

    Sampling starts at 23:45 the previous day so that the final reading lands at
    23:45 on the day under test. The first ``note`` has no predecessor and so
    contributes no interval; starting a quarter-hour early is what makes all 96
    intervals fall inside the day being measured.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2025, 12, 31, 23, 45)
    for i in range(97):  # 23:45 previous day .. 23:45 on the day under test
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 23, 59))
    assert snap["degree_days_today"] == pytest.approx(10.5, abs=0.01)


def test_a_changing_temperature_is_weighted_by_time_not_by_sample() -> None:
    """Half a day at 5 C and half at 15 C is 5.25 + ~0.25 degree-days.

    Twelve hours 10.5 below base is 5.25; the remaining eleven and three-quarter
    hours half a degree below base adds ~0.24.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2026, 1, 1, 0)
    for i in range(49):  # 00:00 .. 12:00 at 5 C (48 intervals = 12 h)
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=100.0)
    noon = start + timedelta(hours=12)
    for i in range(1, 48):  # 12:15 .. 23:45 at 15 C (47 intervals)
        monitor.note(noon + timedelta(minutes=15 * i), outdoor_temp=15.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 23, 59))
    assert snap["degree_days_today"] == pytest.approx(5.49, abs=0.02)


def test_temperature_above_base_contributes_zero_not_negative() -> None:
    """A warm day must not subtract from the heating season's total.

    Summing ``base - temp`` unclamped lets a mild spell cancel out a cold one, so
    a month could report fewer degree-days than it actually had - and the milder
    the weather, the more the error grows.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2026, 1, 1, 0)
    for i in range(97):
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=25.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 2, 0))
    assert snap["degree_days_today"] == 0.0


def test_missing_temperature_is_skipped_not_counted_as_zero() -> None:
    """No reading means no contribution, and the coverage figure records it.

    Counting a missing reading as 0 C would inflate degree-days by the full base
    temperature - and it would do so silently, producing a day that looks much
    colder than it was. The coverage figure is what makes the gap visible.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2026, 1, 1, 0)
    monitor.note(start, outdoor_temp=5.0, gas_total=100.0)
    monitor.note(start + timedelta(minutes=15), outdoor_temp=None, gas_total=100.0)
    monitor.note(start + timedelta(minutes=30), outdoor_temp=5.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 1))
    # Only one interval had a usable reading: 10.5 * (900/86400).
    assert snap["degree_days_today"] == pytest.approx(0.109, abs=0.001)
    assert snap["coverage_today"] < 1.0


def test_coverage_is_one_when_every_interval_was_observed() -> None:
    """Coverage must not punish a well-behaved sensor."""
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2025, 12, 31, 23, 45)
    for i in range(97):
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 23, 59))
    assert snap["coverage_today"] == pytest.approx(1.0, abs=0.001)


def test_coverage_uses_elapsed_time_not_a_fixed_day() -> None:
    """At 06:00 a perfect record of six hours is full coverage, not 25%.

    Dividing by a fixed 24 hours would flag every morning as untrustworthy and
    train the user to ignore the flag.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2026, 1, 1, 0)
    for i in range(25):  # 00:00 .. 06:00
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 6, 1))
    assert snap["coverage_today"] == pytest.approx(1.0, abs=0.01)
    assert snap["degree_days_trusted"] is True


# ---------------------------------------------------------------------------
# Gas usage
# ---------------------------------------------------------------------------


def test_no_gas_reading_yields_none_not_zero() -> None:
    """``None`` and ``0.0`` mean opposite things and must not be conflated.

    A missing meter reading reported as ``0`` reads as "the boiler used no gas
    today", which is a claim about the heating system. ``None`` reads as "we do
    not know", which is the truth.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=None)

    snap = monitor.snapshot(at(2026, 1, 1, 1))
    assert snap["gas_today"] is None


def test_gas_today_is_the_delta_from_the_first_reading_of_the_day() -> None:
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=110.5)

    snap = monitor.snapshot(at(2026, 1, 1, 13))
    assert snap["gas_today"] == pytest.approx(10.5, abs=0.001)


def test_day_rollover_freezes_yesterday_and_restarts_today() -> None:
    """An interval spanning midnight is charged to the day it ended in.

    The reading at 00:30 belongs to an interval that started at noon the day
    before, and there is no way to know how much gas burned before midnight. The
    engine charges the whole interval to the day it ended in, which keeps the
    parts additive (see
    :func:`test_consumption_across_a_period_boundary_is_claimed_exactly_once`).
    The misattribution is bounded by one polling interval; dropping it instead
    would lose the gas from both days.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=110.0)
    monitor.note(at(2026, 1, 2, 0, 30), outdoor_temp=6.0, gas_total=112.0)

    snap = monitor.snapshot(at(2026, 1, 2, 1))
    assert snap["gas_yesterday"] == pytest.approx(10.0, abs=0.01)
    assert snap["gas_today"] == pytest.approx(2.0, abs=0.01)


def test_a_gap_of_several_days_leaves_yesterday_unknown() -> None:
    """Do not present the last observed day as "yesterday" after a gap.

    After an outage the stored day may be from last week. Freezing it as
    yesterday's usage would attribute a week of gas to a single day, which is a
    worse error than admitting the day was not observed.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=110.0)
    # Nothing observed on the 2nd or 3rd; the first sample after the gap is the 4th.
    monitor.note(at(2026, 1, 4, 0), outdoor_temp=5.0, gas_total=130.0)

    snap = monitor.snapshot(at(2026, 1, 4, 1))
    assert snap["gas_yesterday"] is None
    assert snap["gas_today"] == pytest.approx(0.0, abs=0.01)


def test_month_rollover_charges_the_boundary_interval_to_the_new_month() -> None:
    """The month total includes consumption up to the boundary interval.

    January's single reading at 23:00 closes no interval (it is the first, so
    there is nothing to integrate from), and the interval opened by it ends at
    00:30 on 1 February. By the "charged to the period it ends in" rule that gas
    belongs to February - and the crucial part is that it belongs to *somewhere*:
    without the carry-forward it would be claimed by neither month and silently
    disappear, an undercount that grows with the polling interval.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 31, 23), outdoor_temp=0.0, gas_total=100.0)
    monitor.note(at(2026, 2, 1, 0, 30), outdoor_temp=0.0, gas_total=105.0)

    snap = monitor.snapshot(at(2026, 2, 1, 1))
    assert snap["gas_previous_month"] == pytest.approx(0.0, abs=0.01)
    assert snap["gas_this_month"] == pytest.approx(5.0, abs=0.01)


def test_the_previous_month_keeps_its_own_consumption() -> None:
    """A month that ran normally keeps every m3 it actually used."""
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=0.0, gas_total=100.0)
    monitor.note(at(2026, 1, 31, 23), outdoor_temp=0.0, gas_total=180.0)
    monitor.note(at(2026, 2, 1, 12), outdoor_temp=0.0, gas_total=190.0)

    snap = monitor.snapshot(at(2026, 2, 1, 13))
    assert snap["gas_previous_month"] == pytest.approx(80.0, abs=0.01)
    assert snap["gas_this_month"] == pytest.approx(10.0, abs=0.01)


def test_consumption_across_a_period_boundary_is_claimed_exactly_once() -> None:
    """No gas may vanish - and none may be double counted - at a boundary.

    This is the invariant behind the two carry-forward rules. A period split at
    midnight must still account for the whole meter delta when the parts are
    added back together.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 31, 22), outdoor_temp=0.0, gas_total=1000.0)
    monitor.note(at(2026, 1, 31, 23, 30), outdoor_temp=0.0, gas_total=1004.0)
    monitor.note(at(2026, 2, 1, 0, 30), outdoor_temp=0.0, gas_total=1009.0)
    monitor.note(at(2026, 2, 1, 12), outdoor_temp=0.0, gas_total=1015.0)

    snap = monitor.snapshot(at(2026, 2, 1, 13))
    observed_delta = 1015.0 - 1000.0  # 15 m3 across both months
    claimed = (snap["gas_previous_month"] or 0.0) + (snap["gas_this_month"] or 0.0)
    assert claimed == pytest.approx(observed_delta, abs=0.01), (
        f"boundary gas was lost: meter moved {observed_delta} but the periods account for {claimed}"
    )


def test_a_day_gap_leaves_yesterday_unknown_and_restarts_cleanly() -> None:
    """After an outage the day total starts fresh instead of absorbing the gap."""
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=110.0)
    monitor.note(at(2026, 1, 4, 0), outdoor_temp=5.0, gas_total=130.0)
    monitor.note(at(2026, 1, 4, 6), outdoor_temp=5.0, gas_total=132.0)

    snap = monitor.snapshot(at(2026, 1, 4, 7))
    # The three unobserved days are not attributed to yesterday...
    assert snap["gas_yesterday"] is None
    # ...and only the observed part of today counts.
    assert snap["gas_today"] == pytest.approx(2.0, abs=0.01)


def test_a_month_gap_leaves_the_previous_month_unknown() -> None:
    """Skipping a whole month must not report it as a partial total."""
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 31, 23), outdoor_temp=0.0, gas_total=100.0)
    monitor.note(at(2026, 3, 1, 0), outdoor_temp=0.0, gas_total=140.0)

    snap = monitor.snapshot(at(2026, 3, 1, 1))
    assert snap["gas_previous_month"] is None


def test_meter_reset_does_not_produce_negative_usage() -> None:
    """A replaced or rolled-over meter must not report negative consumption.

    ``total_increasing`` semantics allow the value to drop, and a negative daily
    usage would then flow into every downstream average.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=1000.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=5.0)

    snap = monitor.snapshot(at(2026, 1, 1, 13))
    assert snap["gas_today"] is not None
    assert snap["gas_today"] >= 0.0
    assert snap["meter_reset_detected"] is True


def test_a_meter_reset_does_not_corrupt_the_month_total() -> None:
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=1000.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=1002.0)
    monitor.note(at(2026, 1, 2, 0), outdoor_temp=5.0, gas_total=3.0)  # meter swapped

    snap = monitor.snapshot(at(2026, 1, 2, 1))
    assert snap["gas_this_month"] is not None
    assert snap["gas_this_month"] >= 0.0


def test_a_stale_start_reading_cannot_produce_negative_usage() -> None:
    """The clamp in ``gas_used`` is defence against restored state, not decoration.

    Live observations handle a reset by re-basing the start reading, so the delta
    is already zero. But the start and last readings come back from the store
    independently, and a payload written by an older build (or one interrupted
    mid-write) can pair a pre-reset start with a post-reset last. Reading the raw
    difference there yields negative consumption, which would then flow into every
    average downstream.

    Constructed by restoring exactly that inconsistent payload, which the engine
    cannot prevent and therefore must survive.
    """
    payload = {
        "persist": {
            "base_c": BASE,
            "day": {
                "key": "2026-01-02",
                "degree_days": 0.0,
                "covered_seconds": 0.0,
                "samples": 0,
                "gas_start": 1000.0,  # from before the meter swap
                "gas_last": 3.0,  # after it
            },
            "month": {
                "key": [2026, 1],
                "degree_days": 0.0,
                "gas_start": 1000.0,
                "gas_last": 3.0,
                "covered_seconds": 0.0,
            },
        }
    }
    monitor = EnergyMonitor(base_c=BASE)
    monitor.restore(payload)

    snap = monitor.snapshot(at(2026, 1, 2, 1))
    assert snap["gas_today"] == 0.0, "a stale start reading must not read as negative usage"
    assert snap["gas_this_month"] == 0.0


def test_nan_and_infinity_are_rejected_as_temperatures() -> None:
    """``NaN`` compares False against every bound, so a range check lets it through.

    Left unchecked it poisons every total it touches, and ``inf`` would make the
    degree-day sum unrecoverable. Both must be treated as "no reading".

    Only the final interval is credited, because a reading is taken to describe
    the time *since it was taken* - not the gaps before it. The 00:45 value says
    nothing about 00:00-00:15, so that span is not invented. Coverage reports the
    quarter of the window that was genuinely observed.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 0, 15), outdoor_temp=float("nan"), gas_total=100.0)
    monitor.note(at(2026, 1, 1, 0, 30), outdoor_temp=float("inf"), gas_total=100.0)
    monitor.note(at(2026, 1, 1, 0, 45), outdoor_temp=5.0, gas_total=100.0)

    snap = monitor.snapshot(at(2026, 1, 1, 1))
    # One 15-minute interval at a 10.5 C deficit: 10.5 * 900 / 86400.
    assert snap["degree_days_today"] == pytest.approx(0.109, abs=0.001)
    assert snap["coverage_today"] == pytest.approx(0.25, abs=0.01)


def test_nan_and_infinity_are_rejected_as_meter_readings() -> None:
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 0, 15), outdoor_temp=5.0, gas_total=float("nan"))
    monitor.note(at(2026, 1, 1, 0, 30), outdoor_temp=5.0, gas_total=103.0)

    snap = monitor.snapshot(at(2026, 1, 1, 1))
    assert snap["gas_today"] == pytest.approx(3.0, abs=0.01)


def test_booleans_are_not_accepted_as_numbers() -> None:
    """``True`` is an ``int`` in Python, so ``float(True)`` is ``1.0``.

    Reached directly rather than through Home Assistant: a state read from
    ``hass.states`` is always a string, so ``float("true")`` already raises and
    the engine never sees a bool from that path. The guard exists for callers that
    pass a value straight through - tests, future attribute reads, other
    integrations - and the cost of being wrong is a phantom 1.0 degree baked into
    the degree-day total, where nothing downstream could tell it apart from real
    weather.

    Testing ``_finite`` directly is deliberate: routing through ``note()`` would
    only prove the guard is unreachable from that entry point, which is not the
    property being asserted.
    """
    from custom_components.climatepilot.energy_monitor import _finite

    assert _finite(True) is None
    assert _finite(False) is None
    # The contrast that makes the guard necessary rather than decorative.
    assert float(True) == 1.0


# ---------------------------------------------------------------------------
# Derived ratio
# ---------------------------------------------------------------------------


def test_gas_per_degree_day_is_none_when_there_are_no_degree_days() -> None:
    """Dividing by zero degree-days must yield None, not 0 and not infinity.

    Both alternatives are worse: ``0`` claims perfect efficiency and ``inf``
    breaks anything that consumes it.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=25.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 12), outdoor_temp=25.0, gas_total=105.0)

    snap = monitor.snapshot(at(2026, 1, 1, 13))
    assert snap["gas_per_degree_day"] is None


def test_gas_per_degree_day_divides_month_gas_by_month_degree_days() -> None:
    """The ratio is exactly the month's gas over the month's degree-days.

    Asserted as the identity rather than against a hand-computed constant: the
    point is that the two figures come from the same period, which is the property
    that makes the series comparable month to month. A hand-computed value would
    only re-state the sampling schedule.
    """
    monitor = EnergyMonitor(base_c=BASE)
    start = at(2025, 12, 31, 23, 45)
    for i in range(97):
        monitor.note(start + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=1000.0)
    monitor.note(at(2026, 1, 1, 23, 50), outdoor_temp=5.0, gas_total=1105.0)

    snap = monitor.snapshot(at(2026, 1, 1, 23, 55))
    assert snap["gas_this_month"] == pytest.approx(105.0, abs=0.01)
    # A full day below a 10.5 C deficit, plus the 5-minute tail of the last sample.
    assert snap["degree_days_this_month"] == pytest.approx(10.5, abs=0.1)
    assert snap["gas_per_degree_day"] == pytest.approx(
        snap["gas_this_month"] / snap["degree_days_this_month"], abs=0.001
    )
    assert snap["gas_per_degree_day"] == pytest.approx(10.0, abs=0.1)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_restore_preserves_progress_within_the_day() -> None:
    """Restarting mid-day must not reset the accumulator to zero.

    The store is written on every audit, so a restart a few times a day is
    normal; losing the day's progress each time would make the daily figure
    depend on uptime.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 6), outdoor_temp=5.0, gas_total=103.0)
    saved = monitor.snapshot(at(2026, 1, 1, 6))

    restored = EnergyMonitor(base_c=BASE)
    restored.restore(saved)
    restored.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=106.0)

    snap = restored.snapshot(at(2026, 1, 1, 12))
    assert snap["gas_today"] == pytest.approx(6.0, abs=0.01)
    assert snap["degree_days_today"] == pytest.approx(5.25, abs=0.01)


def test_restore_accepts_the_bare_persist_payload() -> None:
    """``restore`` takes either a full snapshot or just its persist section.

    The store keeps only the persist payload, while tests and the coordinator
    may hand over a whole snapshot; accepting both avoids a silent cold start
    when the wrong one is passed.
    """
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 6), outdoor_temp=5.0, gas_total=103.0)

    restored = EnergyMonitor(base_c=BASE)
    restored.restore(monitor.snapshot_persist())
    restored.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=106.0)

    assert restored.snapshot(at(2026, 1, 1, 12))["gas_today"] == pytest.approx(6.0, abs=0.01)


def test_restore_tolerates_garbage() -> None:
    """A corrupt or older payload must start clean rather than raise."""
    for payload in (None, {}, {"persist": "nonsense"}, {"day_key": 5}, [1, 2, 3]):
        monitor = EnergyMonitor(base_c=BASE)
        monitor.restore(payload)  # type: ignore[arg-type]
        assert monitor.snapshot(at(2026, 1, 1, 0))["gas_today"] is None


def test_snapshot_is_json_serialisable() -> None:
    """It has to survive a round trip through the Home Assistant store."""
    monitor = EnergyMonitor(base_c=BASE)
    monitor.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    monitor.note(at(2026, 1, 1, 6), outdoor_temp=5.0, gas_total=103.0)

    encoded = json.dumps(monitor.snapshot(at(2026, 1, 1, 7)))
    assert json.loads(encoded)["gas_today"] == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_the_engine_does_not_import_home_assistant() -> None:
    """Rule 1: a pure engine must stay testable without booting Home Assistant."""
    import inspect

    from custom_components.climatepilot import energy_monitor

    source = inspect.getsource(energy_monitor)
    assert "from homeassistant" not in source
    assert "import homeassistant" not in source
    assert "datetime.now()" not in source, "time must arrive through the `now` argument"
