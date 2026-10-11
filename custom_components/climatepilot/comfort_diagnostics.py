"""Over/under temperature diagnosis per zone (design doc 8 / V0.2).

Pure engine: **no Home Assistant imports** (docs/INTERFACES.md section 0.1). Every
timestamp arrives through the ``now`` argument; this module never reads the wall
clock.

What this answers
-----------------

"Which room is persistently too warm, which one never reaches target, and what
should a human look at first?" The analyser is deliberately *not* a controller:
it never influences arbitration or a device write. It only turns a stream of
``(current, target, demanding)`` observations into a verdict plus an actionable
advice code.

Safety contract
---------------

1. **A missing reading is never a temperature.** ``current`` or ``target`` being
   ``None`` (or non-finite, which includes the ``NaN``/``85.0``-style probe
   failures of design doc test checklist item 11) records **no sample** and the
   verdict is :data:`COMFORT_UNKNOWN` with :data:`ADVICE_NONE`. Treating it as
   ``0`` would invent a room that is freezing - the same mistake V0.1 forbids for
   the outdoor sensor.
2. **A dropout freezes the evidence instead of extending it.** When the feed is
   lost, the last valid sample stops being extrapolated at the moment the loss
   was seen. A single ``unavailable`` reading therefore cannot retroactively turn
   the whole 24 h window into "overheating": being offline for a minute must not
   pollute the whole day's statistics.
3. **Underheating requires real demand.** A room below its target is only
   *underheating* while its thermostat is genuinely calling for heat
   (``demanding=True``). That is a capacity/hydraulics symptom. Below target with
   **no** demand is a different situation - see :data:`ADVICE_CHECK_HYDRAULICS`
   below - and must not be reported as underheating, or the binary sensor
   ``underheating`` ("持续低于目标**且确实在要热**") and this verdict would
   contradict each other about the same room.
4. **Sentinel discipline.** :data:`COMFORT_UNKNOWN` means only "we have no
   trustworthy reading", never "the reading is fine but we do not like it".
   Reusing it for the latter would repeat the ``unknown`` versus "no demand"
   conflation that V0.1 exists to prevent.
5. **Overheating wins ties.** The two conditions are mutually exclusive for a
   numeric deviation, but the evaluation order is pinned so the result is
   deterministic.

Current state versus window evidence
------------------------------------

The verdict and the advice describe **now**: the dwell must still be running at
``now`` (that is what "持续高于目标" means). The 24 h window supplies the
*supporting evidence* - ``average_deviation``, ``max_over``, ``max_under``,
``overheated_seconds``, ``underheated_seconds`` - so a dashboard can show "fine
right now, but 2 h too warm this morning". A past excursion deliberately does not
keep the current verdict alarming forever.

Advice codes
------------

* overheating while another room is genuinely demanding heat ->
  :data:`ADVICE_LOWER_SUPPLY` (the supply curve is too hot for this room);
* overheating with nobody else demanding ->
  :data:`ADVICE_CHECK_SCHEDULE` (why is this room being heated at all?);
* underheating while demanding -> :data:`ADVICE_RAISE_SUPPLY`;
* persistently below target while **not** demanding ->
  :data:`ADVICE_CHECK_HYDRAULICS`. Within a thermostat's own deadband this is
  normal, which is exactly why the advice only appears after the full dwell has
  elapsed: an hour below target with the valve shut is no longer deadband, it is
  a stuck valve, a closed zone valve or no water flow. The verdict stays
  :data:`COMFORT_OK` because the integration is not failing to control anything -
  the actuator simply is not moving - and an ``underheating`` verdict here would
  contradict the ``underheating`` binary sensor's documented meaning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
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

#: Reason codes carried on :class:`ComfortVerdict`. The frozen V0.2 ``const``
#: block only ships :data:`REASON_BELOW_TARGET_WITHOUT_DEMAND`; the rest are
#: scoped here because this engine must not modify ``const.py``. Values are
#: plain lowercase tokens, matching the ``REASON_*`` convention in ``const``.
REASON_NO_DATA = "no_comfort_data"
REASON_OVERHEAT = "overheat_dwell_met"
REASON_UNDERHEAT = "underheat_dwell_met"
REASON_DEMAND_PRESENT = "zone_demanding_heat"
REASON_OTHER_ZONE_DEMANDING = "other_zone_demanding_heat"

#: Hard cap on retained samples per zone, so a hostile or resumed payload cannot
#: unbound memory. With the default 5 min interval a 24 h window holds ~288.
MAX_SAMPLES_PER_ZONE = 4096
MAX_GAPS_PER_ZONE = 4096


@dataclass(frozen=True, slots=True)
class ComfortSample:
    """One trustworthy ``(current, target)`` observation."""

    at: datetime
    current: float
    target: float
    demanding: bool


@dataclass(frozen=True, slots=True)
class ComfortVerdict:
    """What the analyser concluded for one zone.

    ``average_deviation`` is time-weighted over the window: the deviation the
    room actually spent its time at, not the mean of the recorded samples (which
    would over-weight whichever moments happened to be sampled).
    """

    zone_id: str
    verdict: str
    advice: str
    average_deviation: float
    max_over: float
    max_under: float
    overheated_seconds: float
    underheated_seconds: float
    samples: int
    reason_codes: tuple[str, ...]
    below_target_no_demand_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly export for entity attributes/diagnostics."""
        return {
            "zone_id": self.zone_id,
            "verdict": self.verdict,
            "advice": self.advice,
            "average_deviation": round(self.average_deviation, 3),
            "max_over": round(self.max_over, 3),
            "max_under": round(self.max_under, 3),
            "overheated_seconds": round(self.overheated_seconds, 1),
            "underheated_seconds": round(self.underheated_seconds, 1),
            "samples": self.samples,
            "reason_codes": list(self.reason_codes),
            #: Why "check the hydraulics" was advised: time spent below target
            #: while nothing was calling for heat (design doc 9.1).
            "below_target_no_demand_seconds": round(self.below_target_no_demand_seconds, 1),
        }


@dataclass(slots=True)
class _ZoneHistory:
    """Mutable per-zone bookkeeping (never exposed)."""

    samples: list[ComfortSample]
    gaps: list[datetime]
    last_sample_at: datetime | None = None
    last_note_at: datetime | None = None
    gap_open: bool = False


class ComfortAnalyser:
    """Accumulates per-zone comfort observations and grades them on demand."""

    def __init__(
        self,
        *,
        overheat_delta: float = DEFAULT_OVERHEAT_DELTA,
        overheat_dwell: float = DEFAULT_OVERHEAT_DWELL,
        underheat_delta: float = DEFAULT_UNDERHEAT_DELTA,
        underheat_dwell: float = DEFAULT_UNDERHEAT_DWELL,
        window: float = DEFAULT_COMFORT_WINDOW,
        sample_interval: float = DEFAULT_COMFORT_SAMPLE_INTERVAL,
    ) -> None:
        self._overheat_delta = float(overheat_delta)
        self._overheat_dwell = max(0.0, float(overheat_dwell))
        self._underheat_delta = float(underheat_delta)
        self._underheat_dwell = max(0.0, float(underheat_dwell))
        self._window = max(0.0, float(window))
        self._sample_interval = max(0.0, float(sample_interval))

        self._zones: dict[str, _ZoneHistory] = {}

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def note(
        self,
        zone_id: str,
        *,
        current: float | None,
        target: float | None,
        demanding: bool,
        now: datetime,
    ) -> None:
        """Feed one observation for ``zone_id``.

        ``current``/``target`` of ``None`` (or non-finite) mean "we have no
        trustworthy reading": nothing is recorded and the zone's verdict becomes
        :data:`COMFORT_UNKNOWN`. It is never interpreted as ``0``.

        Valid observations are throttled to one per ``sample_interval``. The time
        semantics are still updated on a throttled call - that is what keeps the
        last recorded sample valid up to ``now`` - and the first valid reading
        after a gap always gets through, so a dropout is closed precisely rather
        than one throttle period late.
        """
        if not isinstance(zone_id, str) or not zone_id.strip():
            return
        zone_id = zone_id.strip()
        history = self._zones.get(zone_id)
        if history is None:
            history = _ZoneHistory(samples=[], gaps=[])
            self._zones[zone_id] = history
        history.last_note_at = now

        value = _finite(current)
        goal = _finite(target)
        if value is None or goal is None:
            # No usable reading: open a gap (idempotently) so the previous sample
            # is not extrapolated across it, and record nothing.
            if not history.gap_open:
                history.gaps.append(now)
                history.gap_open = True
            self._prune_history(history, now)
            return

        was_gap = history.gap_open
        history.gap_open = False

        if (
            not was_gap
            and history.last_sample_at is not None
            and self._sample_interval > 0.0
            and (now - history.last_sample_at).total_seconds() < self._sample_interval
        ):
            # Throttled: the state persists through the next recorded sample, so
            # nothing is lost except time resolution. Memory stays bounded.
            self._prune_history(history, now)
            return

        history.samples.append(
            ComfortSample(
                at=now,
                current=value,
                target=goal,
                demanding=bool(demanding),
            )
        )
        history.last_sample_at = now
        self._prune_history(history, now)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def verdict(self, zone_id: str, now: datetime) -> ComfortVerdict:
        """Return the verdict for ``zone_id`` at ``now``.

        Reading is idempotent for a fixed ``now``: :meth:`verdicts` may be called
        repeatedly (the coordinator does) without changing the result. The only
        side effect is pruning data older than the window.
        """
        key = zone_id.strip() if isinstance(zone_id, str) else zone_id
        history = self._zones.get(key) if isinstance(key, str) else None
        if history is None:
            return _unknown_verdict(zone_id)

        self._prune_history(history, now)
        horizon = self._horizon(history, now)
        cutoff = now - timedelta(seconds=self._window)
        window_samples = [sample for sample in history.samples if cutoff <= sample.at <= horizon]
        segments = self._segments(history, cutoff, horizon)

        over_seconds = 0.0
        under_seconds = 0.0
        below_no_demand_seconds = 0.0
        deviation_seconds = 0.0
        attributed_seconds = 0.0

        over_run = 0.0
        under_run = 0.0
        below_no_demand_run = 0.0
        over_run_start: datetime | None = None
        under_run_start: datetime | None = None
        below_no_demand_run_start: datetime | None = None

        for start, end, sample in segments:
            duration = (end - start).total_seconds()
            if sample is None:
                # Unknown stretch: it breaks every run, so a dropout can never be
                # counted towards a dwell.
                over_run = under_run = below_no_demand_run = 0.0
                over_run_start = under_run_start = below_no_demand_run_start = None
                continue

            deviation = sample.current - sample.target
            #: Time-weighted, so the figure is "the deviation the room spent its
            #: time at" rather than an average over an arbitrary sample cadence.
            deviation_seconds += deviation * duration
            attributed_seconds += duration

            if deviation >= self._overheat_delta:
                over_seconds += duration
                over_run += duration
                if over_run_start is None:
                    over_run_start = start
                under_run = below_no_demand_run = 0.0
                under_run_start = below_no_demand_run_start = None
            elif -deviation >= self._underheat_delta and sample.demanding:
                under_seconds += duration
                under_run += duration
                if under_run_start is None:
                    under_run_start = start
                over_run = below_no_demand_run = 0.0
                over_run_start = below_no_demand_run_start = None
            elif -deviation >= self._underheat_delta:
                # Below target with the thermostat not calling for heat: excluded
                # from ``underheated_seconds`` on purpose, because that figure
                # feeds the "underheating" verdict the sensor exposes.
                below_no_demand_seconds += duration
                below_no_demand_run += duration
                if below_no_demand_run_start is None:
                    below_no_demand_run_start = start
                over_run = under_run = 0.0
                over_run_start = under_run_start = None
            else:
                over_run = under_run = below_no_demand_run = 0.0
                over_run_start = under_run_start = below_no_demand_run_start = None

        # Extremes are properties of the readings, not of how long each one
        # happened to hold: take them from the window's samples so the newest
        # sample (which has no following segment) still counts.
        max_over = max((sample.current - sample.target for sample in window_samples), default=0.0)
        max_under = max((sample.target - sample.current for sample in window_samples), default=0.0)
        samples = len(window_samples)
        average = deviation_seconds / attributed_seconds if attributed_seconds > 0.0 else 0.0

        if samples == 0 or history.gap_open:
            # Either nothing trustworthy in the window, or the feed is currently
            # lost. Both are "unknown", never "ok".
            return ComfortVerdict(
                zone_id=zone_id,
                verdict=COMFORT_UNKNOWN,
                advice=ADVICE_NONE,
                average_deviation=average,
                max_over=max(max_over, 0.0),
                max_under=max(max_under, 0.0),
                overheated_seconds=over_seconds,
                underheated_seconds=under_seconds,
                samples=samples,
                reason_codes=(REASON_NO_DATA,),
                below_target_no_demand_seconds=below_no_demand_seconds,
            )

        if over_run >= self._overheat_dwell:
            elsewhere = self._demanding_elsewhere(
                key, over_run_start or (now - timedelta(seconds=over_run)), horizon
            )
            reasons = [REASON_OVERHEAT]
            if elsewhere:
                reasons.append(REASON_OTHER_ZONE_DEMANDING)
            return _verdict(
                zone_id,
                COMFORT_OVERHEATING,
                ADVICE_LOWER_SUPPLY if elsewhere else ADVICE_CHECK_SCHEDULE,
                average,
                max_over,
                max_under,
                over_seconds,
                under_seconds,
                samples,
                reasons,
                below_no_demand_seconds,
            )

        if under_run >= self._underheat_dwell:
            return _verdict(
                zone_id,
                COMFORT_UNDERHEATING,
                ADVICE_RAISE_SUPPLY,
                average,
                max_over,
                max_under,
                over_seconds,
                under_seconds,
                samples,
                [REASON_UNDERHEAT, REASON_DEMAND_PRESENT],
                below_no_demand_seconds,
            )

        if below_no_demand_run >= self._underheat_dwell:
            # Below target for a full dwell with the thermostat not asking for
            # heat: deadband no longer explains it, so point at the hydraulics.
            # The verdict stays OK - see the module docstring.
            return _verdict(
                zone_id,
                COMFORT_OK,
                ADVICE_CHECK_HYDRAULICS,
                average,
                max_over,
                max_under,
                over_seconds,
                under_seconds,
                samples,
                [REASON_BELOW_TARGET_WITHOUT_DEMAND],
                below_no_demand_seconds,
            )

        return _verdict(
            zone_id,
            COMFORT_OK,
            ADVICE_NONE,
            average,
            max_over,
            max_under,
            over_seconds,
            under_seconds,
            samples,
            [],
            below_no_demand_seconds,
        )

    def verdicts(self, now: datetime) -> dict[str, ComfortVerdict]:
        """Return the verdict for every zone the analyser has seen.

        Zones whose feed is currently lost are included, with a
        :data:`COMFORT_UNKNOWN` verdict - a lost room must stay visible.
        """
        return {zone_id: self.verdict(zone_id, now) for zone_id in list(self._zones)}

    def drop_zone(self, zone_id: str) -> None:
        """Forget everything about ``zone_id`` (removed from the options)."""
        if isinstance(zone_id, str):
            self._zones.pop(zone_id.strip(), None)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable state (round-trips through :meth:`restore`)."""
        return {
            "zones": {
                zone_id: {
                    "samples": [
                        [_dump_dt(s.at), s.current, s.target, s.demanding] for s in history.samples
                    ],
                    "gaps": [_dump_dt(gap) for gap in history.gaps],
                    "last_sample_at": _dump_dt(history.last_sample_at),
                    "last_note_at": _dump_dt(history.last_note_at),
                    "gap_open": history.gap_open,
                }
                for zone_id, history in self._zones.items()
            }
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore a :meth:`snapshot`; malformed input yields a clean state.

        Tolerant on purpose: this runs while loading persisted state, and a
        payload written by an older version (or hand-edited) must not break
        setup. Unknown keys and unusable entries are skipped.
        """
        self._zones = {}
        if not data or not isinstance(data, dict):
            return
        stored = data.get("zones")
        if not isinstance(stored, dict):
            return

        for raw_id, payload in stored.items():
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            if not isinstance(payload, dict):
                continue

            samples = _opt_samples(payload.get("samples"))
            gaps = [gap for gap in _opt_dts(payload.get("gaps")) if gap is not None]
            last_sample_at = _opt_dt(payload.get("last_sample_at"))
            last_note_at = _opt_dt(payload.get("last_note_at"))
            gap_open = _opt_bool(payload.get("gap_open"))

            # Consistency: the throttle clock cannot precede the newest sample,
            # and an "open gap" must be backed by a gap record that is at least as
            # new as the newest sample. Otherwise it is stale bookkeeping (or a
            # hand-edited payload), and trusting it would pin the zone to
            # ``unknown`` forever.
            if samples:
                newest = samples[-1].at
                if last_sample_at is None or last_sample_at < newest:
                    last_sample_at = newest
            if last_note_at is None:
                last_note_at = last_sample_at
            if not gaps or (samples and gaps[-1] < samples[-1].at):
                gap_open = False

            self._zones[raw_id.strip()] = _ZoneHistory(
                samples=samples,
                gaps=gaps,
                last_sample_at=last_sample_at,
                last_note_at=last_note_at,
                gap_open=gap_open,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _horizon(self, history: _ZoneHistory, now: datetime) -> datetime:
        """Latest instant we have any evidence for.

        The last recorded sample is held forward - that is what makes a sample
        represent the state until the next one - but only as far as the last
        ``note`` call. If the caller stops feeding the analyser, the evidence
        stops growing instead of silently covering the whole window.
        """
        if history.last_note_at is None:
            return now
        return min(now, history.last_note_at)

    def _segments(
        self, history: _ZoneHistory, cutoff: datetime, horizon: datetime
    ) -> list[tuple[datetime, datetime, ComfortSample | None]]:
        """Tile ``[cutoff, horizon]`` with ``(start, end, sample|None)``.

        ``sample is None`` marks a stretch during which the feed was lost. Points
        are clipped to the window, so an interval straddling the cutoff
        contributes only its in-window part.

        A sample also stops speaking after ``window`` seconds of silence. In
        normal operation the caller feeds every cycle, so this never binds - but
        if cycles were missed for hours, letting a single old reading vouch for
        the whole span would fabricate evidence (a room could be reported as
        "overheating all afternoon" from one morning reading). A reading cannot
        claim more history than the engine retains at all.
        """
        points: list[tuple[datetime, ComfortSample | None]] = [
            (sample.at, sample) for sample in history.samples
        ]
        points.extend((gap, None) for gap in history.gaps)
        points.sort(key=lambda item: item[0])

        segments: list[tuple[datetime, datetime, ComfortSample | None]] = []
        for index, (at, sample) in enumerate(points):
            end = points[index + 1][0] if index + 1 < len(points) else horizon
            if sample is not None:
                end = min(end, at + timedelta(seconds=self._window))
            start = max(at, cutoff)
            end = min(end, horizon)
            if end > start:
                segments.append((start, end, sample))
        return segments

    def _demanding_elsewhere(self, zone_id: str, start: datetime, horizon: datetime) -> bool:
        """True when another zone was genuinely demanding heat during ``start..horizon``.

        Used to choose between "the supply curve is too hot for this room"
        (someone else needs the heat) and "why is this room being heated at all".
        """
        for other_id, history in self._zones.items():
            if other_id == zone_id:
                continue
            other_horizon = self._horizon(history, horizon)
            if other_horizon <= start:
                continue
            for _, _, sample in self._segments(history, start, other_horizon):
                if sample is not None and sample.demanding:
                    return True
        return False

    def _prune_history(self, history: _ZoneHistory, now: datetime) -> None:
        """Drop data older than the window, keeping the state at the cutoff.

        The newest point at or before the cutoff is retained because it is the
        state in effect *at* the cutoff; dropping it would leave the first
        in-window stretch unattributed. That keeps pruning idempotent for a fixed
        ``now`` (the coordinator calls :meth:`verdicts` more than once).
        """
        cutoff = now - timedelta(seconds=self._window)
        history.samples = _trim(history.samples, cutoff, MAX_SAMPLES_PER_ZONE)
        history.gaps = _trim(history.gaps, cutoff, MAX_GAPS_PER_ZONE)


def _trim(items: list[Any], cutoff: datetime, limit: int) -> list[Any]:
    """Keep in-window items plus the last one before ``cutoff``, bounded."""
    kept = [item for item in items if _at_of(item) >= cutoff]
    older = [item for item in items if _at_of(item) < cutoff]
    if older:
        kept.insert(0, older[-1])
    return kept[-limit:] if limit > 0 else kept


def _at_of(item: Any) -> datetime:
    return item.at if isinstance(item, ComfortSample) else item


def _verdict(
    zone_id: str,
    verdict: str,
    advice: str,
    average: float,
    max_over: float,
    max_under: float,
    over_seconds: float,
    under_seconds: float,
    samples: int,
    reasons: list[str],
    below_no_demand_seconds: float = 0.0,
) -> ComfortVerdict:
    return ComfortVerdict(
        zone_id=zone_id,
        verdict=verdict,
        advice=advice,
        average_deviation=average,
        max_over=max(0.0, max_over),
        max_under=max(0.0, max_under),
        overheated_seconds=over_seconds,
        underheated_seconds=under_seconds,
        samples=samples,
        reason_codes=tuple(reasons),
        below_target_no_demand_seconds=below_no_demand_seconds,
    )


def _unknown_verdict(zone_id: str) -> ComfortVerdict:
    return ComfortVerdict(
        zone_id=zone_id,
        verdict=COMFORT_UNKNOWN,
        advice=ADVICE_NONE,
        average_deviation=0.0,
        max_over=0.0,
        max_under=0.0,
        overheated_seconds=0.0,
        underheated_seconds=0.0,
        samples=0,
        reason_codes=(REASON_NO_DATA,),
    )


# ---------------------------------------------------------------------------
# Coercion helpers (caller input and persisted payloads are untrusted)
# ---------------------------------------------------------------------------
def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not usable.

    ``None``, text, and ``NaN``/``inf`` all mean "no reading" - never ``0``.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _opt_samples(raw: Any) -> list[ComfortSample]:
    out: list[ComfortSample] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        at = _opt_dt(item[0])
        current = _finite(item[1])
        target = _finite(item[2])
        if at is None or current is None or target is None:
            continue
        out.append(
            ComfortSample(
                at=at,
                current=current,
                target=target,
                demanding=bool(item[3]),
            )
        )
    out.sort(key=lambda sample: sample.at)
    return out[-MAX_SAMPLES_PER_ZONE:]


def _opt_dts(raw: Any) -> list[datetime | None]:
    if not isinstance(raw, list):
        return []
    return [_opt_dt(item) for item in raw]


def _opt_bool(value: Any) -> bool:
    """Coerce a persisted flag: only a real ``True`` counts.

    ``bool("no")`` is ``True``, so a hand-edited payload could otherwise pin a
    zone to "unknown" forever.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
