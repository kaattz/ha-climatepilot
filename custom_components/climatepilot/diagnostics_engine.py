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
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .const import (
    DEFAULT_SHORT_CYCLE_SECONDS,
    DEFAULT_SHORT_CYCLE_WINDOW,
    FEEDBACK_UNCERTAIN,
    FEEDBACK_VALID,
)

# Burn kinds.
KIND_HEATING = "heating"
KIND_DHW = "dhw"
KIND_UNKNOWN = "unknown"

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
    {"off", "idle", "standby", "false", "0", "none", "no", "cool", "dry", "fan_only"}
)
_UNKNOWN_STATES = frozenset({"unknown", "unavailable", "none", ""})


def classify_burner_state(state: str | None) -> tuple[bool, str | None, bool]:
    """Classify a raw burner state.

    Returns ``(burning, kind, known)``. ``known`` is False when the reading
    carries no information (``unknown``/``unavailable``/``None``), in which case
    ``burning`` mirrors the previous state and the caller must hold the burn.
    """
    if state is None:
        return False, None, False
    text = str(state).strip().lower()
    if text in _UNKNOWN_STATES:
        return False, None, False
    if text in _HEATING_STATES:
        return True, KIND_HEATING, True
    if text in _DHW_STATES:
        return True, KIND_DHW, True
    if text in _OFF_STATES:
        return False, None, True
    # Anything else is a real "on" signal we cannot attribute to heating.
    return True, KIND_UNKNOWN, True


class BurnerStats:
    """Tracks ignition counts, burn durations and short cycling."""

    def __init__(
        self,
        *,
        short_cycle_seconds: float = DEFAULT_SHORT_CYCLE_SECONDS,
        short_cycle_window: float = DEFAULT_SHORT_CYCLE_WINDOW,
        short_cycle_min_count: int = 1,
    ) -> None:
        self._short_cycle_seconds = float(short_cycle_seconds)
        self._short_cycle_window = float(short_cycle_window)
        self._short_cycle_min_count = int(short_cycle_min_count)
        self._retention = max(self._short_cycle_window, 86400.0)

        self._state: str | None = None
        self._burning = False
        self._kind: str | None = None
        self._unknown = False
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
        """
        self._last_now = now
        burning, kind, known = classify_burner_state(state)
        self._state = state

        if not known:
            # Feed loss: hold the burn open rather than inventing a flame-off.
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
            "persist": self._persist(),
        }

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
        self._burn_start = _opt_dt(payload.get("burn_start"))
        if self._burning and self._burn_start is None:
            # Inconsistent payload: never keep an unbounded open burn.
            self._burning = False
            self._kind = None

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
