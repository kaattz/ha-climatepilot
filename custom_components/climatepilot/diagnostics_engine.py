"""Burner runtime statistics and short-cycle diagnostics (design doc 8).

Pure engine: **no Home Assistant imports** (docs/INTERFACES.md section 0.1).
Every timestamp arrives through a ``now`` argument.

Design notes
------------

* Space heating and domestic hot water (DHW) burns are separated whenever the
  reported state lets us tell them apart. A generic ``on``-style burner state
  cannot be attributed, so those burns are counted under the ``unknown`` kind
  and the snapshot raises ``includes_dhw`` - the UI must then present the
  numbers as "total burns, may include domestic hot water" instead of calling a
  shower ignition a floor-heating short cycle (design doc 8).
* **An error is not a burn.** A fault/lockout token (``Error`` is code 33 on the
  target device) closes any open burn but is never itself counted as an
  ignition, and an unrecognised token carries no information at all - it is
  treated like a feed loss rather than guessed as "burning". Fabricating an
  ignition would corrupt ``starts_24h`` and could raise a *short-cycle alarm*,
  which is the one signal a user is expected to act on.
* **The circuit disambiguates the vocabulary.** The primary target device
  (Rinnai REB-A2747FF-CH) reports ``Burning``/``Heating Water`` on both its
  space-heating and its DHW sensor with the same spelling, so the classification
  takes a ``circuit`` argument: without it those tokens stay ``unknown``.
* An ``unknown``/``unavailable`` burner reading never closes an ongoing burn.
  Treating a feed loss as "flame off" would fabricate a short cycle and raise a
  false alarm; the burn is held open instead and ``quality`` drops to
  ``uncertain``.
* Short cycles are **diagnostic only**: this module never commands the boiler
  (design doc 8, 11).

Snapshot contract (consumed by the platform layer)
--------------------------------------------------

``snapshot(now)`` returns display keys plus a nested ``"persist"`` payload so
the same call serves both the diagnostic entities and state persistence. All
durations are **seconds as float**; all timestamps are ISO-8601 strings.

V0.2 (docs/INTERFACES_V0.2.md 3.3) **adds** keys to this payload - it renames
and removes nothing, because the V0.1 platform layer reads the original 16:
``cycling_quality``, ``cycling_quality_reason``, ``unexplained_burns_24h``
(an explicitly named alias of ``burns_without_demand_24h``), ``grade_heating_24h``
and ``grade_dhw_24h``. ``short_cycle_alert`` keeps its V0.1 meaning: heating and
unattributable burns only, never DHW.

``cycling_quality`` grades the window's **space-heating** average burn duration
(``>= DEFAULT_CYCLING_GOOD_SECONDS`` -> good, ``>= DEFAULT_CYCLING_POOR_SECONDS``
-> fair, else poor). Domestic-hot-water burns are graded separately in
``grade_dhw_24h`` and can neither raise nor lower the heating grade. With no
heating burns in the window the answer is ``unknown`` rather than ``poor``:
"nothing was observed" must not read as a fault.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .const import (
    CIRCUIT_DHW,
    CIRCUIT_HEATING,
    DEFAULT_CYCLING_GOOD_SECONDS,
    DEFAULT_CYCLING_POOR_SECONDS,
    DEFAULT_SHORT_CYCLE_SECONDS,
    DEFAULT_SHORT_CYCLE_WINDOW,
    FEEDBACK_UNCERTAIN,
    FEEDBACK_VALID,
    QUALITY_FAIR,
    QUALITY_GOOD,
    QUALITY_POOR,
    QUALITY_UNKNOWN,
)

# Burn kinds.
KIND_HEATING = "heating"
KIND_DHW = "dhw"
KIND_UNKNOWN = "unknown"
#: The device reported a fault/lockout. Deliberately **not** a burn kind: an
#: error must never be counted as an ignition (see :func:`classify_burner_state`).
KIND_ERROR = "error"

# Burn cycle histogram buckets (upper bound exclusive, last bucket is open).
HISTOGRAM_BINS: tuple[tuple[str, float | None], ...] = (
    ("0-5m", 300.0),
    ("5-10m", 600.0),
    ("10-20m", 1200.0),
    ("20-40m", 2400.0),
    ("40-60m", 3600.0),
    ("60m+", None),
)

MAX_EVENTS = 512

_HEATING_STATES = frozenset({"heating", "heat", "space_heating", "burner_heating", "ch"})
_DHW_STATES = frozenset({"dhw", "hot_water", "domestic_hot_water", "water_heating", "hotwater"})
_OFF_STATES = frozenset(
    # "30" is the Rinnai code for Standby, exposed verbatim by raw entities.
    {"off", "idle", "standby", "30", "false", "0", "none", "no", "cool", "dry", "fan_only"}
)
_UNKNOWN_STATES = frozenset({"unknown", "unavailable", "none", ""})
#: Fault tokens. A device in one of these states is **not** burning: counting a
#: lockout as an ignition would corrupt ``starts_24h``, the duration histogram
#: and - worst of all - fabricate a short-cycle alarm, which is the one signal a
#: user is expected to act on.
_ERROR_STATES = frozenset(
    {
        "error",
        "fault",
        "lockout",
        "lock_out",
        "alarm",
        "failure",
        "failed",
        "malfunction",
        "blocked",
        # Rinnai REB-A2747FF-CH code 33. The integration maps codes to names, but
        # a raw entity may expose either form.
        "33",
    }
)
#: Tokens that mean "something is on/burning" without saying *what* is burning.
#: ``Heating Water`` (code 31) and ``Burning`` (code 32) are the Rinnai
#: REB-A2747FF-CH vocabulary, and that device's space-heating and DHW sensors
#: share it - so the token alone cannot be attributed without knowing which
#: circuit the sensor watches. ``on``/``true``/``1`` cover plain
#: ``binary_sensor`` burner entities.
_GENERIC_ON_STATES = frozenset(
    {
        "on",
        "true",
        "1",
        "yes",
        "running",
        "active",
        "burning",
        "flame",
        "heating water",
        "heating_water",
        "water heating",
        "31",
        "32",
    }
)


def classify_burner_state(
    state: str | None, *, circuit: str | None = None
) -> tuple[bool, str | None, bool]:
    """Classify a raw burner state.

    Returns ``(burning, kind, known)``. ``known`` is False when the reading
    carries no information (``unknown``/``unavailable``/``None``), in which case
    ``burning`` mirrors the previous state and the caller must hold the burn.

    ``circuit`` disambiguates tokens that depend on *which* sensor produced
    them. The Rinnai REB-A2747FF-CH reports ``Burning``/``Heating Water`` on both
    its space-heating and its DHW sensor with the same spelling, so
    ``CIRCUIT_HEATING`` / ``CIRCUIT_DHW`` resolve them; ``None`` stays honestly
    unattributed (:data:`KIND_UNKNOWN`).

    Classification order and its safety meaning:

    1. no reading (``None``/``unknown``/``unavailable``) -> *unknown*, hold;
    2. fault/lockout -> **known, not burning**. A device in ``Error`` is not
       producing heat, so counting it as an ignition would corrupt
       ``starts_24h`` and could manufacture a short-cycle alarm - the one signal
       a user is expected to act on;
    3. explicit heating / DHW / off vocabularies;
    4. generic "something is burning" tokens -> attributed through ``circuit``;
    5. **any other token on a pin-down circuit** -> a burn on that circuit. That
       sensor's vocabulary is closed and specific, so a string we have not seen
       is far more likely a burning state added by a firmware revision than a
       reading that means "off"; treating it as *unknown* would silently switch
       the whole quality-grading feature off on vocabulary drift.
    6. any other token with **no** circuit -> *unknown*, hold. There is nothing
       to attribute it to, and guessing "burning" would fabricate an ignition.
    """
    if state is None:
        return False, None, False
    text = str(state).strip().lower()
    if text in _UNKNOWN_STATES:
        return False, None, False
    if text in _ERROR_STATES:
        # Known state, definitely not burning: this closes an open burn, but it
        # is never itself counted as an ignition.
        return False, None, True
    if text in _HEATING_STATES:
        return True, KIND_HEATING, True
    if text in _DHW_STATES:
        return True, KIND_DHW, True
    if text in _OFF_STATES:
        return False, None, True
    if text in _GENERIC_ON_STATES:
        # Something is burning; only the circuit tells us what.
        return True, _circuit_kind(circuit) or KIND_UNKNOWN, True
    # Unrecognised token: only a known circuit gives it a meaning.
    kind = _circuit_kind(circuit)
    if kind is not None:
        return True, kind, True
    return False, None, False


def is_error_state(state: str | None) -> bool:
    """True when ``state`` is a fault/lockout token (never a burn)."""
    if state is None:
        return False
    return str(state).strip().lower() in _ERROR_STATES


def _circuit_kind(circuit: str | None) -> str | None:
    """Map a configured sensor circuit onto a burn kind."""
    if circuit == CIRCUIT_HEATING:
        return KIND_HEATING
    if circuit == CIRCUIT_DHW:
        return KIND_DHW
    return None


class BurnerStats:
    """Tracks ignition counts, burn durations and short cycling."""

    def __init__(
        self,
        *,
        short_cycle_seconds: float = DEFAULT_SHORT_CYCLE_SECONDS,
        short_cycle_window: float = DEFAULT_SHORT_CYCLE_WINDOW,
        short_cycle_min_count: int = 1,
        circuit: str | None = None,
        cycling_good_seconds: float = DEFAULT_CYCLING_GOOD_SECONDS,
        cycling_poor_seconds: float = DEFAULT_CYCLING_POOR_SECONDS,
    ) -> None:
        self._short_cycle_seconds = float(short_cycle_seconds)
        self._short_cycle_window = float(short_cycle_window)
        self._short_cycle_min_count = int(short_cycle_min_count)
        #: Which sensor this instance watches. The target device (Rinnai
        #: REB-A2747FF-CH) spells its space-heating and DHW burner states
        #: identically, so the token alone is not enough to attribute a burn.
        self._circuit = circuit
        #: V0.2 combustion-quality bands: average space-heating burn duration
        #: at or above ``good`` grades good, at or above ``poor`` grades fair,
        #: anything shorter is poor.
        self._cycling_good_seconds = float(cycling_good_seconds)
        self._cycling_poor_seconds = float(cycling_poor_seconds)
        self._retention = max(self._short_cycle_window, 86400.0)

        self._state: str | None = None
        self._burning = False
        self._kind: str | None = None
        self._unknown = False
        self._error = False
        self._burn_start: datetime | None = None

        self._last_duration: float | None = None
        self._last_duration_kind: str | None = None
        self._last_started_at: datetime | None = None
        self._last_ended_at: datetime | None = None

        self._starts: list[tuple[datetime, str]] = []
        self._cycles: list[tuple[datetime, float, str]] = []

        self._starts_total = 0
        self._demand_active = False
        self._burns_without_demand: list[datetime] = []
        self._last_now: datetime | None = None

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def note_state(self, state: str | None, now: datetime) -> None:
        """Feed one burner state observation.

        ``now`` is the observation time and is never read from the wall clock.

        Feeding a state never touches the demand flag set by :meth:`note_demand`,
        so the caller may (and should) call ``note_demand`` first: that is what
        lets a demand-less ignition be recognised.
        """
        self._last_now = now
        burning, kind, known = classify_burner_state(state, circuit=self._circuit)
        self._state = state
        self._error = is_error_state(state)

        if not known:
            # Feed loss - or an unrecognised token, which carries no information
            # either: hold the burn rather than inventing a flame-off (or, worse,
            # an ignition).
            self._unknown = True
            return

        self._unknown = False
        if burning and not self._burning:
            self._open_burn(kind or KIND_UNKNOWN, now)
        elif burning and self._burning:
            self._upgrade_kind(kind or KIND_UNKNOWN)
        elif not burning and self._burning:
            self._close_burn(now)

        self._prune(now)

    def note_demand(self, now: datetime, has_demand: bool) -> None:
        """Feed whether any zone really demands heat (diagnostic correlation)."""
        self._demand_active = bool(has_demand)
        self._last_now = now

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def snapshot(self, now: datetime) -> dict[str, Any]:
        """Return the diagnostic snapshot for ``now``."""
        self._last_now = now
        self._prune(now)

        win = timedelta(seconds=self._short_cycle_window)
        hour = timedelta(hours=1)
        day = timedelta(hours=24)

        starts_1h = sum(1 for ts, _ in self._starts if now - ts <= hour)
        starts_24h = sum(1 for ts, _ in self._starts if now - ts <= day)
        heating_24h = sum(
            1 for ts, kind in self._starts if kind == KIND_HEATING and now - ts <= day
        )
        dhw_24h = sum(1 for ts, kind in self._starts if kind == KIND_DHW and now - ts <= day)
        unknown_24h = sum(
            1 for ts, kind in self._starts if kind == KIND_UNKNOWN and now - ts <= day
        )

        cycles = [(ts, dur, kind) for ts, dur, kind in self._cycles if now - ts <= day]
        # Design doc 8 / test checklist item 10: a domestic-hot-water burn must
        # not be reported as a floor-heating short cycle, so only burns we can
        # attribute to space heating (heating + unattributable) feed the verdict.
        heating_cycles = [c for c in cycles if c[2] != KIND_DHW]
        dhw_cycles = [c for c in cycles if c[2] == KIND_DHW]
        short_cycles = sum(
            1
            for ts, dur, _ in heating_cycles
            if dur < self._short_cycle_seconds and now - ts <= win
        )
        histogram = {label: 0 for label, _ in HISTOGRAM_BINS}
        for _, dur, _ in cycles:
            histogram[self._bin_for(dur)] += 1
        histogram_heating = {label: 0 for label, _ in HISTOGRAM_BINS}
        for _, dur, _ in heating_cycles:
            histogram_heating[self._bin_for(dur)] += 1

        runtime = sum(dur for _, dur, _ in cycles)
        if self._burning and self._burn_start is not None:
            runtime += max(0.0, (now - self._burn_start).total_seconds())

        # Design doc 8: whenever a counted burn cannot be attributed to space
        # heating, the totals must be presented as "total burns, may include
        # domestic hot water" instead of a floor-heating short-cycle verdict.
        includes_dhw = self._includes_dhw(now, day)

        # --- V0.2 quality grading -----------------------------------------
        # ``heating_cycles`` is heating + unattributable, which is exactly the
        # set the interface specifies for the heating grade: a DHW burn must
        # never drag the space-heating grade down, so it is graded separately.
        grade_heating = self._grade_counts(heating_cycles)
        grade_dhw = self._grade_counts(dhw_cycles)
        cycling_quality, cycling_reason = self._cycling_quality(heating_cycles)

        return {
            "state": self._state,
            "state_known": not self._unknown,
            "quality": FEEDBACK_UNCERTAIN if self._unknown else FEEDBACK_VALID,
            "burning": self._burning,
            "kind": self._kind,
            "includes_dhw": includes_dhw,
            "last_duration": self._last_duration,
            "last_duration_kind": self._last_duration_kind,
            "last_started_at": _dump_dt(self._last_started_at),
            "last_ended_at": _dump_dt(self._last_ended_at),
            "current_duration": self._current_duration(now),
            "starts_1h": starts_1h,
            "starts_24h": starts_24h,
            "starts_total": self._starts_total,
            "heating_starts_24h": heating_24h,
            "dhw_starts_24h": dhw_24h,
            "unknown_kind_starts_24h": unknown_24h,
            "burns_24h": len(cycles),
            "runtime_24h": runtime,
            "heating_runtime_24h": sum(dur for _, dur, _ in heating_cycles),
            "dhw_runtime_24h": sum(dur for _, dur, _ in dhw_cycles),
            "short_cycles_24h": short_cycles,
            "short_cycle_alert": short_cycles >= self._short_cycle_min_count,
            "short_cycle_seconds": self._short_cycle_seconds,
            "histogram": histogram,
            "histogram_heating": histogram_heating,
            "histogram_window_seconds": 86400.0,
            "demand_active": self._demand_active,
            "burns_without_demand_24h": sum(
                1 for ts in self._burns_without_demand if now - ts <= day
            ),
            # --- V0.2 additions (docs/INTERFACES_V0.2.md 3.3) --------------
            # Additive only: every V0.1 key above keeps its name and meaning.
            "error_state": self._error,
            "cycling_quality": cycling_quality,
            "cycling_quality_reason": cycling_reason,
            #: Explicitly named alias of ``burns_without_demand_24h``; the old
            #: key is kept because the V0.1 platform layer reads it.
            "unexplained_burns_24h": sum(1 for ts in self._burns_without_demand if now - ts <= day),
            "grade_heating_24h": grade_heating,
            "grade_dhw_24h": grade_dhw,
            "persist": self._persist(),
        }

    def _grade_for(self, duration: float) -> str:
        """Grade one burn by its duration (V0.2 quality bands)."""
        if duration >= self._cycling_good_seconds:
            return QUALITY_GOOD
        if duration >= self._cycling_poor_seconds:
            return QUALITY_FAIR
        return QUALITY_POOR

    def _grade_counts(self, cycles: list[tuple[datetime, float, str]]) -> dict[str, int]:
        """Count ``cycles`` per quality band, always exposing all three keys."""
        counts = {QUALITY_GOOD: 0, QUALITY_FAIR: 0, QUALITY_POOR: 0}
        for _, duration, _ in cycles:
            counts[self._grade_for(duration)] += 1
        return counts

    def _cycling_quality(
        self, heating_cycles: list[tuple[datetime, float, str]]
    ) -> tuple[str, str]:
        """Grade the window's **space-heating** combustion quality.

        Returns ``(quality, reason)``. Domestic-hot-water burns are excluded by
        the caller, so a long shower can never be mistaken for good space-heating
        behaviour - nor drag a good space-heating day down to "poor".

        With no heating burns in the window the answer is ``unknown``, not
        ``poor``: nothing was observed, and "we saw nothing" must not read as a
        fault (the same discipline as ``COMBINATION_NONE`` meaning only "no
        demand").
        """
        if not heating_cycles:
            return QUALITY_UNKNOWN, "no heating burns in the last 24h"
        average = sum(dur for _, dur, _ in heating_cycles) / len(heating_cycles)
        quality = self._grade_for(average)
        count = len(heating_cycles)
        return quality, f"average {average:.0f}s over {count} heating burns"

    @property
    def short_cycle_alert(self) -> bool:
        """Short-cycle alarm state as of the last observed ``now``.

        Mirrors ``snapshot(now)["short_cycle_alert"]``; the snapshot value is
        authoritative whenever a fresh ``now`` is available.
        """
        now = self._last_now
        if now is None:
            return False
        if not self._cycles:
            return False
        win = timedelta(seconds=self._short_cycle_window)
        count = sum(
            1
            for ts, dur, kind in self._cycles
            if kind != KIND_DHW and dur < self._short_cycle_seconds and now - ts <= win
        )
        return count >= self._short_cycle_min_count

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot_persist(self, now: datetime) -> dict[str, Any]:
        """Return only the persistable payload (convenience for the store)."""
        return self.snapshot(now)["persist"]

    def restore(self, data: dict[str, Any]) -> None:
        """Restore a :meth:`snapshot` result or a bare persist payload."""
        if not data:
            return
        payload = data.get("persist")
        if not isinstance(payload, dict):
            payload = data

        self._state = payload.get("state")
        self._burning = bool(payload.get("burning", False))
        self._kind = payload.get("kind")
        self._unknown = bool(payload.get("unknown", False))
        # V0.1 payloads have no "error" key: re-derive it from the last state so
        # an upgrade does not forget that the boiler was faulted.
        self._error = (
            bool(payload.get("error"))
            if "error" in payload
            else is_error_state(payload.get("state"))
        )
        self._burn_start = _opt_dt(payload.get("burn_start"))
        if self._burning and self._burn_start is None:
            # Inconsistent payload: never keep an unbounded open burn.
            self._burning = False
            self._kind = None
        if self._burning and self._error:
            # A fault state is never a burn; a hand-edited or torn payload must
            # not resurrect a phantom ignition that never ended.
            self._burning = False
            self._kind = None
            self._burn_start = None

        self._last_duration = _opt_float(payload.get("last_duration"))
        self._last_duration_kind = payload.get("last_duration_kind")
        self._last_started_at = _opt_dt(payload.get("last_started_at"))
        self._last_ended_at = _opt_dt(payload.get("last_ended_at"))
        self._starts_total = _opt_int(payload.get("starts_total")) or 0
        self._demand_active = bool(payload.get("demand_active", False))
        self._last_now = _opt_dt(payload.get("last_now"))

        self._starts = _events(payload.get("starts"))
        self._cycles = _cycles(payload.get("cycles"))
        self._burns_without_demand = [
            ts for ts in _timestamps(payload.get("burns_without_demand")) if ts
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _open_burn(self, kind: str, now: datetime) -> None:
        self._burning = True
        self._kind = kind
        self._burn_start = now
        self._last_started_at = now
        self._starts.append((now, kind))
        self._starts_total += 1
        if not self._demand_active:
            self._burns_without_demand.append(now)

    def _upgrade_kind(self, kind: str) -> None:
        """Refine an unattributed burn once a specific state arrives."""
        if self._kind == KIND_UNKNOWN and kind in (KIND_HEATING, KIND_DHW):
            self._kind = kind
            if self._starts:
                ts, _ = self._starts[-1]
                self._starts[-1] = (ts, kind)

    def _close_burn(self, now: datetime) -> None:
        start = self._burn_start
        duration = 0.0 if start is None else max(0.0, (now - start).total_seconds())
        kind = self._kind or KIND_UNKNOWN
        self._burning = False
        self._kind = None
        self._burn_start = None
        self._last_duration = duration
        self._last_duration_kind = kind
        self._last_ended_at = now
        self._cycles.append((now, duration, kind))

    def _current_duration(self, now: datetime) -> float | None:
        if not self._burning or self._burn_start is None:
            return None
        return max(0.0, (now - self._burn_start).total_seconds())

    def _includes_dhw(self, now: datetime, day: timedelta) -> bool:
        """True when some counted burn is not attributable to space heating."""
        if any(kind != KIND_HEATING for ts, kind in self._starts if now - ts <= day):
            return True
        return any(kind != KIND_HEATING for ts, _, kind in self._cycles if now - ts <= day)

    def _bin_for(self, duration: float) -> str:
        for label, upper in HISTOGRAM_BINS:
            if upper is None or duration < upper:
                return label
        return HISTOGRAM_BINS[-1][0]

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._retention)
        self._starts = [e for e in self._starts if e[0] >= cutoff][-MAX_EVENTS:]
        self._cycles = [e for e in self._cycles if e[0] >= cutoff][-MAX_EVENTS:]
        self._burns_without_demand = [ts for ts in self._burns_without_demand if ts >= cutoff][
            -MAX_EVENTS:
        ]

    def _persist(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "burning": self._burning,
            "kind": self._kind,
            "unknown": self._unknown,
            "error": self._error,
            "burn_start": _dump_dt(self._burn_start),
            "last_duration": self._last_duration,
            "last_duration_kind": self._last_duration_kind,
            "last_started_at": _dump_dt(self._last_started_at),
            "last_ended_at": _dump_dt(self._last_ended_at),
            "starts_total": self._starts_total,
            "demand_active": self._demand_active,
            "last_now": _dump_dt(self._last_now),
            "starts": [[_dump_dt(ts), kind] for ts, kind in self._starts],
            "cycles": [[_dump_dt(ts), dur, kind] for ts, dur, kind in self._cycles],
            "burns_without_demand": [_dump_dt(ts) for ts in self._burns_without_demand],
        }


# ---------------------------------------------------------------------------
# Coercion helpers (persisted payloads are untrusted)
# ---------------------------------------------------------------------------
def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _opt_int(value: Any) -> int | None:
    """Coerce a persisted counter, rejecting junk instead of raising."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        try:
            as_float = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(as_float):
            return None
        result = int(as_float)
    return max(0, result)


def _timestamps(raw: Any) -> list[datetime | None]:
    if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, dict)):
        return []
    return [_opt_dt(item) for item in raw]


def _events(raw: Any) -> list[tuple[datetime, str]]:
    out: list[tuple[datetime, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        ts = _opt_dt(item[0])
        kind = item[1]
        if ts is None or kind not in (KIND_HEATING, KIND_DHW, KIND_UNKNOWN):
            continue
        out.append((ts, str(kind)))
    return out


def _cycles(raw: Any) -> list[tuple[datetime, float, str]]:
    out: list[tuple[datetime, float, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        ts = _opt_dt(item[0])
        duration = _opt_float(item[1])
        kind = item[2]
        if ts is None or duration is None:
            continue
        if kind not in (KIND_HEATING, KIND_DHW, KIND_UNKNOWN):
            continue
        out.append((ts, max(0.0, duration), str(kind)))
    return out
