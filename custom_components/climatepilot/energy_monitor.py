"""Gas usage and heating degree-days: an instrument, not a proof of savings.

Pure engine: this module **must not import Home Assistant** (docs/INTERFACES.md
section 0.1) so it can be unit tested without booting HA, and it may only depend
on the standard library plus ``.const``. Every timestamp arrives through the
``now`` argument; the wall clock is never read here.

Why this module exists, and what it deliberately does not do
------------------------------------------------------------

The original request was "how do I know the integration saved gas?". That
question cannot be answered here, and the honest response is to build a meter
rather than a verdict:

* the gas meter is **whole-house** - it also feeds domestic hot water and the
  hob, and those are not heating;
* there is **no flow-rate sensor**, so apportioning consumption by burner time
  would be guesswork on a modulating boiler, where a low flame and a high flame
  burn very different amounts over the same ten minutes;
* **weather does not repeat**, so the only credible comparison is a same-period
  A/B alternation, which needs dozens of cycles and a real heating season.

So this engine reports *what happened*: gas used per day and per month, the
degree-days those periods contained, and their ratio. ``gas_per_degree_day`` is a
**comparable series**, not an efficiency figure - it is the number a user can
watch over months to see whether the house is using more or less gas for the same
weather. Nothing here claims a saving.

Three rules that exist because the naive implementation lies
------------------------------------------------------------

1. **A missing reading is skipped, never treated as zero.** Counting an absent
   outdoor temperature as 0 degrees C would inflate degree-days by the full base
   temperature, producing a day that looks far colder than it was. Counting a
   missing meter reading as 0 m3 would claim the boiler burned nothing. Both are
   plausible-looking wrong numbers, so both are ``None`` instead, and
   ``coverage`` records how much of the day was actually observed.
2. **Temperatures above the base contribute nothing, not a negative amount.**
   Summing the raw difference would let a mild spell cancel out a cold one, so a
   month could report fewer degree-days than it really had - and the warmer the
   weather, the larger the error.
3. **A meter reset cannot produce negative consumption.** ``total_increasing``
   meters are allowed to drop when they are replaced or roll over; a negative
   daily figure would then flow into every average built on top of it.

State is O(1) - the current day, the current month, and the last *complete* day
and month. Nothing grows with uptime, which matters because this payload is
written to the Home Assistant store on every audit.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from .const import (
    DEFAULT_DEGREE_DAY_BASE_C,
    ENERGY_COVERAGE_TRUSTED,
)

#: Seconds in an hour, used to turn a sum of (degrees x seconds) into degree-hours.
_SECONDS_PER_HOUR = 3600.0

#: Seconds in a day, the denominator for the coverage figure.
_SECONDS_PER_DAY = 86400.0


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    ``NaN`` and the infinities are rejected deliberately: ``NaN`` compares
    ``False`` against every bound, so a naive range check would let it through as
    a plausible temperature and poison every total it touches.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _round_opt(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


class _DayAccumulator:
    """One calendar day's running totals."""

    __slots__ = ("covered_seconds", "degree_days", "gas_last", "gas_start", "key", "samples")

    def __init__(self, key: date) -> None:
        self.key = key
        self.degree_days = 0.0
        self.covered_seconds = 0.0
        self.samples = 0
        self.gas_start: float | None = None
        self.gas_last: float | None = None

    def gas_used(self) -> float | None:
        """Consumption so far, or ``None`` when no usable pair of readings exists.

        A reset is clamped to zero rather than reported as negative; the caller
        records that a reset happened, which is the honest description of what
        was observed.
        """
        if self.gas_start is None or self.gas_last is None:
            return None
        return max(0.0, self.gas_last - self.gas_start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.isoformat(),
            "degree_days": self.degree_days,
            "covered_seconds": self.covered_seconds,
            "samples": self.samples,
            "gas_start": self.gas_start,
            "gas_last": self.gas_last,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _DayAccumulator | None:
        if not isinstance(data, dict):
            return None
        raw_key = data.get("key")
        if not isinstance(raw_key, str):
            return None
        try:
            key = date.fromisoformat(raw_key)
        except ValueError:
            return None
        acc = cls(key)
        acc.degree_days = _finite(data.get("degree_days")) or 0.0
        acc.covered_seconds = _finite(data.get("covered_seconds")) or 0.0
        acc.samples = int(_finite(data.get("samples")) or 0)
        acc.gas_start = _finite(data.get("gas_start"))
        acc.gas_last = _finite(data.get("gas_last"))
        return acc


class _MonthAccumulator:
    """One calendar month's running totals, tracked separately from the day.

    The month cannot be derived by summing days, because days that were never
    observed would silently count as zero. Keeping the month boundary's meter
    reading means a month with a gap reports the gas that actually passed
    through the meter.
    """

    __slots__ = ("covered_seconds", "degree_days", "gas_last", "gas_start", "key")

    def __init__(self, key: tuple[int, int]) -> None:
        self.key = key
        self.degree_days = 0.0
        self.gas_start: float | None = None
        self.gas_last: float | None = None
        self.covered_seconds = 0.0

    def gas_used(self) -> float | None:
        if self.gas_start is None or self.gas_last is None:
            return None
        return max(0.0, self.gas_last - self.gas_start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": [self.key[0], self.key[1]],
            "degree_days": self.degree_days,
            "gas_start": self.gas_start,
            "gas_last": self.gas_last,
            "covered_seconds": self.covered_seconds,
        }

    @classmethod
    def from_dict(cls, data: Any) -> _MonthAccumulator | None:
        if not isinstance(data, dict):
            return None
        raw_key = data.get("key")
        if not isinstance(raw_key, (list, tuple)) or len(raw_key) != 2:
            return None
        try:
            key = (int(raw_key[0]), int(raw_key[1]))
        except (TypeError, ValueError):
            return None
        acc = cls(key)
        acc.degree_days = _finite(data.get("degree_days")) or 0.0
        acc.gas_start = _finite(data.get("gas_start"))
        acc.gas_last = _finite(data.get("gas_last"))
        acc.covered_seconds = _finite(data.get("covered_seconds")) or 0.0
        return acc


class EnergyMonitor:
    """Accumulate gas usage and degree-days across days and months.

    Call :meth:`note` once per audit cycle with whatever is currently known, then
    read :meth:`snapshot`. Both ``outdoor_temp`` and ``gas_total`` may be ``None``
    at any time - the engine simply records less.
    """

    def __init__(self, base_c: float = DEFAULT_DEGREE_DAY_BASE_C) -> None:
        base = _finite(base_c)
        self.base_c = DEFAULT_DEGREE_DAY_BASE_C if base is None else base

        self._day: _DayAccumulator | None = None
        self._month: _MonthAccumulator | None = None
        self._prev_day: _DayAccumulator | None = None
        self._prev_month: _MonthAccumulator | None = None
        self._last_gas_total: float | None = None
        self._last_note: datetime | None = None
        self._meter_reset: bool = False

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def note(
        self,
        now: datetime,
        outdoor_temp: float | None = None,
        gas_total: float | None = None,
    ) -> None:
        """Record one observation.

        The temperature contributes to degree-days **weighted by the time since
        the previous call**, so the integral is over real elapsed time rather
        than over sample count. That is what makes a 15-minute poll and a
        60-minute poll agree.
        """
        temp = _finite(outdoor_temp)
        total = _finite(gas_total)

        day_key = now.date()
        month_key = (now.year, now.month)

        # --- rollovers, before anything is added to the new period ----------
        if self._day is None or self._day.key != day_key:
            self._roll_day(day_key)
        if self._month is None or self._month.key != month_key:
            self._roll_month(month_key)

        assert self._day is not None  # set by _roll_day
        assert self._month is not None  # set by _roll_month

        elapsed = self._elapsed_seconds(now)

        # --- degree-days ---------------------------------------------------
        if temp is not None:
            # Only ever positive: a mild spell must not cancel a cold one.
            #
            # Divided by seconds in a *day*, not an hour: accumulating
            # (deficit x seconds) and dividing by 3600 yields degree-*hours*,
            # which is 24x too large. A 10.5 C deficit held for one full day is
            # 10.5 degree-days.
            deficit = max(0.0, self.base_c - temp)
            contribution = deficit * elapsed / _SECONDS_PER_DAY
            self._day.degree_days += contribution
            self._month.degree_days += contribution
            self._day.covered_seconds += elapsed
            self._month.covered_seconds += elapsed
            self._day.samples += 1

        # --- gas -----------------------------------------------------------
        if total is not None:
            if self._last_gas_total is not None and total < self._last_gas_total:
                # The meter was replaced or rolled over. Everything measured
                # against the old reading is no longer comparable, so both
                # periods restart from the new value.
                self._meter_reset = True
                self._day.gas_start = total
                self._month.gas_start = total
            if self._day.gas_start is None:
                self._day.gas_start = total
            if self._month.gas_start is None:
                self._month.gas_start = total
            self._day.gas_last = total
            self._month.gas_last = total
            self._last_gas_total = total

        self._last_note = now

    def _elapsed_seconds(self, now: datetime) -> float:
        """Seconds since the previous observation, clamped to a sane window.

        The first call has no predecessor, so nothing is accumulated - there is
        no interval to integrate over.

        A very long gap (an outage, a suspended laptop) is clamped rather than
        integrated across, because spreading one reading over several days would
        invent weather that was never measured. The clamp is one day; the
        coverage figure and the "yesterday" guard are what expose the hole.
        """
        if self._last_note is None:
            return 0.0
        delta = (now - self._last_note).total_seconds()
        if delta <= 0:
            return 0.0
        return min(delta, _SECONDS_PER_DAY)

    def _roll_day(self, new_key: date) -> None:
        """Freeze the finished day and start a new one.

        The finished day is only kept as "yesterday" when it is genuinely the
        calendar day before the new one. After a gap the stored day may be a week
        old, and presenting it as yesterday would attribute a week of gas to a
        single day - a worse error than admitting the day was not observed.

        The last meter reading of the finished day is **carried forward as the new
        day's starting point**. Without that carry the interval spanning midnight
        belongs to neither day: the finished day has already stopped accumulating
        and the new one starts from the reading *after* the gas was burned, so the
        consumption vanishes from both. Carrying it forward charges the interval
        to the day it ended in, which keeps the totals additive.

        The carry is skipped across a gap. After several unobserved days the
        interval may contain days' worth of gas, and charging all of it to today
        would be worse than the small undercount of starting fresh.
        """
        carry: float | None = None
        if self._day is not None:
            if (new_key - self._day.key).days == 1:
                self._prev_day = self._day
                carry = self._day.gas_last
            else:
                self._prev_day = None
        self._day = _DayAccumulator(new_key)
        self._day.gas_start = carry

    def _roll_month(self, new_key: tuple[int, int]) -> None:
        """Freeze the finished month, keeping it only if it is the month before.

        Carries the boundary reading forward for the same reason as
        :meth:`_roll_day`: otherwise the consumption across midnight on the last
        day of the month is claimed by neither month.
        """
        carry: float | None = None
        if self._month is not None:
            prev = self._month.key
            consecutive = (new_key[0] == prev[0] and new_key[1] == prev[1] + 1) or (
                new_key[1] == 1 and prev[1] == 12 and new_key[0] == prev[0] + 1
            )
            self._prev_month = self._month if consecutive else None
            if consecutive:
                carry = self._month.gas_last
        self._month = _MonthAccumulator(new_key)
        self._month.gas_start = carry

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Return the current readings plus the persistable state.

        Every value is either a real number or ``None``; ``None`` always means
        "not observed", never "zero". Consumers must render the two differently.
        """
        today = self._day
        month = self._month

        degree_days_today = 0.0 if today is None else today.degree_days
        degree_days_month = 0.0 if month is None else month.degree_days
        gas_today = None if today is None else today.gas_used()
        gas_month = None if month is None else month.gas_used()

        coverage_today = self._coverage(today, now)

        # The ratio is deliberately undefined when there is nothing to divide by:
        # returning 0 would claim perfect efficiency, and infinity would break
        # every consumer.
        gas_per_degree_day: float | None = None
        if gas_month is not None and degree_days_month > 0.0:
            gas_per_degree_day = gas_month / degree_days_month

        readings = {
            "gas_today": _round_opt(gas_today),
            "gas_yesterday": _round_opt(
                None if self._prev_day is None else self._prev_day.gas_used()
            ),
            "gas_this_month": _round_opt(gas_month),
            "gas_previous_month": _round_opt(
                None if self._prev_month is None else self._prev_month.gas_used()
            ),
            "degree_days_today": _round_opt(degree_days_today),
            "degree_days_this_month": _round_opt(degree_days_month),
            "gas_per_degree_day": _round_opt(gas_per_degree_day),
            "coverage_today": _round_opt(coverage_today),
            "degree_days_trusted": coverage_today >= ENERGY_COVERAGE_TRUSTED,
            "meter_reset_detected": self._meter_reset,
            "base_c": self.base_c,
        }

        return {**readings, "persist": self._persist_payload()}

    def snapshot_persist(self) -> dict[str, Any]:
        """Return only the persistable payload (convenience for the store)."""
        return self._persist_payload()

    def _coverage(self, day: _DayAccumulator | None, now: datetime | None) -> float:
        """Fraction of the elapsed day that had a usable temperature reading.

        The denominator is time elapsed *today*, not a fixed 24 hours: at 06:00 a
        perfect record of the six hours so far is full coverage, not 25%.
        """
        if day is None or now is None:
            return 0.0
        elapsed = (
            now - datetime.combine(day.key, datetime.min.time(), tzinfo=now.tzinfo)
        ).total_seconds()
        if elapsed <= 0:
            return 0.0
        return min(1.0, day.covered_seconds / elapsed)

    def _persist_payload(self) -> dict[str, Any]:
        return {
            "base_c": self.base_c,
            "day": None if self._day is None else self._day.to_dict(),
            "month": None if self._month is None else self._month.to_dict(),
            "prev_day": None if self._prev_day is None else self._prev_day.to_dict(),
            "prev_month": None if self._prev_month is None else self._prev_month.to_dict(),
            "last_gas_total": self._last_gas_total,
            "last_note": None if self._last_note is None else self._last_note.isoformat(),
            "meter_reset": self._meter_reset,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore a :meth:`snapshot` or a bare persist payload.

        Tolerant on purpose: this runs while persisted state is loaded, and a
        payload written by an older build (or hand-edited) must not break setup.
        Anything unparseable is dropped rather than guessed at, leaving a clean
        cold start - which is visible as ``None`` readings rather than wrong ones.
        """
        self._day = None
        self._month = None
        self._prev_day = None
        self._prev_month = None
        self._last_gas_total = None
        self._last_note = None
        self._meter_reset = False

        if not isinstance(data, dict):
            return

        payload = data.get("persist")
        if not isinstance(payload, dict):
            payload = data

        base = _finite(payload.get("base_c"))
        if base is not None:
            self.base_c = base

        self._day = _DayAccumulator.from_dict(payload.get("day"))
        self._month = _MonthAccumulator.from_dict(payload.get("month"))
        self._prev_day = _DayAccumulator.from_dict(payload.get("prev_day"))
        self._prev_month = _MonthAccumulator.from_dict(payload.get("prev_month"))
        self._last_gas_total = _finite(payload.get("last_gas_total"))
        self._meter_reset = bool(payload.get("meter_reset"))

        raw_note = payload.get("last_note")
        if isinstance(raw_note, str):
            try:
                self._last_note = datetime.fromisoformat(raw_note)
            except ValueError:
                self._last_note = None
