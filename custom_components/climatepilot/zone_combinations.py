"""Zone-combination analysis for the heating system (design doc 8 / V0.2).

Pure engine: **no Home Assistant imports** (docs/INTERFACES.md section 0.1) so it
can be unit tested without booting HA. Every timestamp arrives through the
``now`` argument; this module never reads the wall clock.

What this answers
-----------------

"Which rooms were demanding heat *together*, how long did each pattern last, and
which one dominates the day?" That is the input the hydraulics discussion needs:
a single room cycling alone points at one emitter, while
``living_room+master_bedroom`` dominating the day points at the supply curve.

Semantics (docs/INTERFACES_V0.2.md 3.1)
---------------------------------------

* A combination is keyed by the **sorted set** of demanding zone ids, so
  ``{a, b}`` and ``{b, a}`` are the same combination and the key is stable
  across reconfigurations that reorder the zone list.
* The empty set is a real combination (``COMBINATION_NONE``): "nobody was
  demanding heat". It is reported as its own entry with
  ``share == 0.0`` because :attr:`CombinationStat.share` is expressed as a
  fraction of *demand* time, and a day with no demand has no demand time.
* **Dwell filter.** A change is only accepted once the new set has been
  observed for ``min_dwell`` seconds. The switch is then back-dated to the
  moment the change was *first* seen, because that is when the demand actually
  changed - the dwell is a confirmation delay, not a physical duration.
* **Short-interval policy (fixed by test).** A completed interval shorter than
  ``min_duration`` is *folded into the preceding interval*: the preceding
  observation is extended to cover the noise, so a thermostat blinking for 30
  seconds does not split one steady combination into two. Contiguous intervals
  with the same key are then merged at report time. When a short interval has
  no predecessor (it is the very first thing observed) it is dropped and its
  span is not attributed to anything.
* **Rolling window.** :meth:`ZoneCombinationTracker.report` counts only data
  inside ``[now - window, now]``; intervals straddling the boundary contribute
  only their in-window part, and older observations are pruned.

Sentinel discipline (``COMBINATION_NONE``)
------------------------------------------

``COMBINATION_NONE`` means **exactly one thing**: no zone was demanding heat
inside the window. It is never used to mean "there was demand but no pattern
dominated it". A sentinel with two mutually exclusive meanings cannot be
branched on correctly by the UI or by an alert rule - the same class of mistake
as reusing ``unknown`` for "no demand" in V0.1. So:

* ``dominant_key is COMBINATION_NONE``  ⟺  the window contains no demand time;
* when demand exists, ``dominant_key`` is the highest-share demand combination
  (ties broken by key so successive refreshes are reproducible);
* ``dominant_threshold`` / :attr:`CombinationReport.has_dominant_mode` express
  "is that leading pattern a real pattern or a coin flip?" as a *separate*
  question, so a dispersed mix stays visible instead of being flattened to
  ``none``.

``CombinationReport.distinct_count`` counts **demand** combinations only - the
empty set is reported in ``stats`` (with ``share == 0.0``) and in the
``total_groups`` export key, but it is not a "zone combination" a user asks
about.

This engine is observation-only: it never influences arbitration or what gets
written to a device.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from .const import (
    COMBINATION_NONE,
    DEFAULT_COMBINATION_DOMINANT_SHARE,
    DEFAULT_COMBINATION_MIN_DURATION,
    DEFAULT_COMBINATION_MIN_DWELL,
    DEFAULT_COMBINATION_WINDOW,
)


def _key_for(zones: frozenset[str]) -> str:
    """Return the stable, sortable identifier for ``zones``.

    The empty set is :data:`COMBINATION_NONE`, never an empty string: an empty
    entity state reads as "no data" in Home Assistant, which is exactly the
    wrong message for "the house was not asking for heat".
    """
    if not zones:
        return COMBINATION_NONE
    return "+".join(sorted(zones))


@dataclass(frozen=True, slots=True)
class CombinationObservation:
    """One interval during which a fixed set of zones demanded heat."""

    zones: frozenset[str]
    started_at: datetime
    ended_at: datetime | None

    @property
    def duration(self) -> float:
        """Seconds the interval lasted; ``0.0`` while it is still running."""
        if self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    @property
    def key(self) -> str:
        """Stable identifier such as ``"living_room+master_bedroom"``."""
        return _key_for(self.zones)

    @property
    def zone_ids(self) -> tuple[str, ...]:
        """Demanding zone ids, sorted."""
        return tuple(sorted(self.zones))


@dataclass(frozen=True, slots=True)
class CombinationStat:
    """Aggregated figures for one combination inside the report window."""

    key: str
    zone_ids: tuple[str, ...]
    count: int
    total_seconds: float
    share: float
    average_seconds: float
    longest_seconds: float


@dataclass(frozen=True, slots=True)
class CombinationReport:
    """What the combination tracker saw inside its window.

    ``dominant_threshold`` travels with the report (rather than being read from
    a module constant) so a consumer testing :attr:`has_dominant_mode` uses
    exactly the threshold this tracker was configured with, including after a
    :meth:`ZoneCombinationTracker.restore`.
    """

    dominant_key: str
    dominant_share: float
    distinct_count: int
    total_demand_seconds: float
    stats: tuple[CombinationStat, ...]
    current_key: str
    current_seconds: float
    dominant_threshold: float = DEFAULT_COMBINATION_DOMINANT_SHARE

    @property
    def has_dominant_mode(self) -> bool:
        """True when the leading combination is a genuine pattern.

        False means the window's demand time is spread across several patterns
        and no single one deserves to be called "the" combination - the caller
        should look at ``stats`` instead of trusting ``dominant_key`` alone.
        """
        return self.dominant_share >= self.dominant_threshold

    @property
    def total_groups(self) -> int:
        """Number of groups in ``stats``, *including* the empty (no-demand) set."""
        return len(self.stats)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly export for entity attributes/diagnostics."""
        return {
            "dominant_key": self.dominant_key,
            "dominant_share": round(self.dominant_share, 4),
            "dominant_threshold": round(self.dominant_threshold, 4),
            "has_dominant_mode": self.has_dominant_mode,
            #: Demand combinations only - the empty set is not a "combination"
            #: in the user-facing sense (see the module docstring).
            "distinct_count": self.distinct_count,
            #: Groups in ``stats`` including the empty (no-demand) set.
            "total_groups": self.total_groups,
            "total_demand_seconds": round(self.total_demand_seconds, 1),
            "current_key": self.current_key,
            "current_seconds": round(self.current_seconds, 1),
            "stats": [
                {
                    "key": stat.key,
                    "zone_ids": list(stat.zone_ids),
                    "count": stat.count,
                    "total_seconds": round(stat.total_seconds, 1),
                    "share": round(stat.share, 4),
                    "average_seconds": round(stat.average_seconds, 1),
                    "longest_seconds": round(stat.longest_seconds, 1),
                }
                for stat in self.stats
            ],
        }


class ZoneCombinationTracker:
    """Debounced observation of which zones demand heat together.

    ``min_dwell`` filters KNX flicker, ``min_duration`` folds isolated short
    intervals away, ``window`` bounds retention and ``dominant_share``
    describes the share at which a combination deserves to be called dominant
    (see :attr:`dominant_share_threshold`).
    """

    def __init__(
        self,
        *,
        min_dwell: float = DEFAULT_COMBINATION_MIN_DWELL,
        min_duration: float = DEFAULT_COMBINATION_MIN_DURATION,
        window: float = DEFAULT_COMBINATION_WINDOW,
        dominant_share: float = DEFAULT_COMBINATION_DOMINANT_SHARE,
    ) -> None:
        self._min_dwell = max(0.0, float(min_dwell))
        self._min_duration = max(0.0, float(min_duration))
        self._window = max(0.0, float(window))
        self._dominant_share = float(dominant_share)

        self._current_zones: frozenset[str] = frozenset()
        self._current_started_at: datetime | None = None
        self._pending_zones: frozenset[str] | None = None
        self._pending_since: datetime | None = None

        self._observations: list[CombinationObservation] = []

    # ------------------------------------------------------------------
    # Configuration introspection
    # ------------------------------------------------------------------
    @property
    def dominant_threshold(self) -> float:
        """Share at which a combination would be considered "dominant".

        Informational only: :attr:`CombinationReport.dominant_key` always names
        the highest-share *demand* combination, and falls back to
        ``COMBINATION_NONE`` only when nothing was demanding heat in the
        window. Reporting ``none`` merely because no single pattern reached
        this share would tell the user "nobody is heating" while the house is
        very much being heated. Callers wanting the stricter question should
        read :attr:`CombinationReport.has_dominant_mode`.
        """
        return self._dominant_share

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def note_demand(self, demanding: Iterable[str], now: datetime) -> None:
        """Record the set of zones demanding heat at ``now``.

        ``demanding`` is order-insensitive. A change is held back until it has
        been observed for ``min_dwell`` seconds; if the set flickers back to the
        current one before that, the candidate is discarded and nothing is
        recorded.

        An empty iterable is a real reading ("nobody is demanding heat"). A
        ``None`` or non-iterable argument carries **no** information and is
        ignored outright: reading it as "no demand" would be guessing zero
        demand, which V0.1 forbids.
        """
        desired = _normalise_zones(demanding)
        if desired is None:
            return

        if self._current_started_at is None:
            # Nothing observed yet: adopt the first reading immediately. Waiting
            # a dwell here would throw away the state the system starts in.
            self._current_zones = desired
            self._current_started_at = now
            self._pending_zones = None
            self._pending_since = None
            return

        if desired == self._current_zones:
            self._pending_zones = None
            self._pending_since = None
            return

        if desired == self._pending_zones and self._pending_since is not None:
            if (now - self._pending_since).total_seconds() >= self._min_dwell:
                self._switch(desired, self._pending_since)
            return

        self._pending_zones = desired
        self._pending_since = now
        if self._min_dwell <= 0.0:
            self._switch(desired, now)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def report(self, now: datetime) -> CombinationReport:
        """Aggregate the window ending at ``now``."""
        cutoff = now - timedelta(seconds=self._window)
        self._prune(cutoff)

        merged = self._merged_intervals(now)

        tally: dict[str, list[Any]] = {}
        for zones, start, end in merged:
            duration = (min(end, now) - max(start, cutoff)).total_seconds()
            if duration <= 0.0:
                continue
            key = _key_for(zones)
            entry = tally.get(key)
            if entry is None:
                tally[key] = [zones, 1, duration, duration]
            else:
                entry[1] += 1
                entry[2] += duration
                entry[3] = max(entry[3], duration)

        total_demand = sum(entry[2] for entry in tally.values() if entry[0])

        stats: list[CombinationStat] = []
        for key, (zones, count, total, longest) in tally.items():
            share = 0.0 if not zones or total_demand <= 0.0 else total / total_demand
            stats.append(
                CombinationStat(
                    key=key,
                    zone_ids=tuple(sorted(zones)),
                    count=count,
                    total_seconds=total,
                    share=share,
                    average_seconds=total / count if count else 0.0,
                    longest_seconds=longest,
                )
            )
        # Deterministic ordering: busiest first, then by key so two equally
        # long patterns never swap places between refreshes.
        stats.sort(key=lambda stat: (-stat.total_seconds, stat.key))

        dominant = next((stat for stat in stats if stat.zone_ids), None)

        if self._current_started_at is None:
            current_key = COMBINATION_NONE
            current_seconds = 0.0
        else:
            current_key = _key_for(self._current_zones)
            current_seconds = max(0.0, (now - self._current_started_at).total_seconds())

        return CombinationReport(
            dominant_key=dominant.key if dominant is not None else COMBINATION_NONE,
            dominant_share=dominant.share if dominant is not None else 0.0,
            distinct_count=sum(1 for stat in stats if stat.zone_ids),
            total_demand_seconds=total_demand,
            stats=tuple(stats),
            current_key=current_key,
            current_seconds=current_seconds,
            dominant_threshold=self._dominant_share,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable state (round-trips through :meth:`restore`)."""
        return {
            "current_zones": sorted(self._current_zones),
            "current_started_at": _dump_dt(self._current_started_at),
            "pending_zones": (None if self._pending_zones is None else sorted(self._pending_zones)),
            "pending_since": _dump_dt(self._pending_since),
            "observations": [
                [sorted(obs.zones), _dump_dt(obs.started_at), _dump_dt(obs.ended_at)]
                for obs in self._observations
            ],
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore a :meth:`snapshot`; malformed input falls back to a clean state."""
        if not data or not isinstance(data, dict):
            return

        started_at = _opt_dt(data.get("current_started_at"))
        if started_at is None:
            # An interval without a start time would be unbounded: drop it.
            self._current_zones = frozenset()
            self._current_started_at = None
            self._pending_zones = None
            self._pending_since = None
        else:
            self._current_zones = _opt_zones(data.get("current_zones"))
            self._current_started_at = started_at
            pending_since = _opt_dt(data.get("pending_since"))
            pending_zones = data.get("pending_zones")
            if pending_since is not None and isinstance(pending_zones, list):
                self._pending_zones = _opt_zones(pending_zones)
                self._pending_since = pending_since
            else:
                self._pending_zones = None
                self._pending_since = None

        self._observations = _opt_observations(data.get("observations"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _switch(self, zones: frozenset[str], at: datetime) -> None:
        """Close the current interval at ``at`` and start the new one there."""
        self._record(
            CombinationObservation(
                zones=self._current_zones,
                started_at=self._current_started_at,  # type: ignore[arg-type]
                ended_at=at,
            )
        )
        self._current_zones = zones
        self._current_started_at = at
        self._pending_zones = None
        self._pending_since = None

    def _record(self, observation: CombinationObservation) -> None:
        """Store a completed interval, folding sub-``min_duration`` noise away."""
        if observation.duration < self._min_duration:
            if self._observations:
                previous = self._observations[-1]
                self._observations[-1] = replace(previous, ended_at=observation.ended_at)
            # No predecessor: the noise span is simply not attributed to
            # anything rather than being invented as demand.
            return
        self._observations.append(observation)

    def _merged_intervals(self, now: datetime) -> list[list[Any]]:
        """Return contiguous ``[zones, start, end]`` runs, open one included."""
        raw: list[list[Any]] = [
            [obs.zones, obs.started_at, obs.ended_at]
            for obs in self._observations
            if obs.ended_at is not None
        ]
        if self._current_started_at is not None:
            raw.append([self._current_zones, self._current_started_at, now])

        merged: list[list[Any]] = []
        for zones, start, end in raw:
            if end <= start:
                continue
            if merged and merged[-1][0] == zones and merged[-1][2] == start:
                merged[-1][2] = end
                continue
            merged.append([zones, start, end])
        return merged

    def _prune(self, cutoff: datetime) -> None:
        self._observations = [
            obs for obs in self._observations if obs.ended_at is None or obs.ended_at >= cutoff
        ]


# ---------------------------------------------------------------------------
# Coercion helpers (persisted payloads and caller input are untrusted)
# ---------------------------------------------------------------------------
def _normalise_zones(demanding: Iterable[str] | None) -> frozenset[str] | None:
    """Coerce caller input into a frozenset of non-empty zone ids.

    Returns ``None`` when the argument carries no information at all (``None``
    or a non-iterable). ``frozenset()`` is a *reading* - it means "no zone was
    demanding heat" - and must never be confused with "we do not know".
    """
    if demanding is None or isinstance(demanding, (str, bytes)):
        return None
    try:
        items = list(demanding)
    except TypeError:
        return None
    zones: set[str] = set()
    for item in items:
        if item is None:
            continue
        text = item.strip() if isinstance(item, str) else str(item)
        if text:
            zones.add(text)
    return frozenset(zones)


def _opt_zones(raw: Any) -> frozenset[str]:
    zones = _normalise_zones(raw)
    return frozenset() if zones is None else zones


def _opt_observations(raw: Any) -> list[CombinationObservation]:
    out: list[CombinationObservation] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        start = _opt_dt(item[1])
        end = _opt_dt(item[2])
        if start is None or end is None or end <= start:
            continue
        out.append(
            CombinationObservation(
                zones=_opt_zones(item[0]),
                started_at=start,
                ended_at=end,
            )
        )
    out.sort(key=lambda obs: obs.started_at)
    return out


def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
