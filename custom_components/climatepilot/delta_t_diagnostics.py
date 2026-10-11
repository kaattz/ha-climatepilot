"""Delta-T trend, condensing regime, hydraulic verdict and curve advice (V0.3).

Pure engine: **no Home Assistant imports** (docs/INTERFACES.md section 0.1,
re-affirmed by docs/INTERFACES_V0.3.md section 1). Every timestamp arrives
through the ``now`` argument; this module never reads the wall clock. It also
deliberately does not import ``probe_monitor`` or any other engine, so it can be
unit tested standalone.

READ THIS BEFORE USING ANY NUMBER FROM THIS MODULE
--------------------------------------------------

**A temperature difference is not heat power.** The difference between supply
and return temperature (``delta_t``) is an *indirect* indicator of flow and
condensing conditions. It is **not** a heat output, **not** an efficiency, and
**not** a thermal power measurement - calculating real output would require flow
rate and gas consumption data that this integration does not have (design doc
8, and section 1.2 "不做" list). Every report therefore carries
``is_power_measurement = False`` in :meth:`DeltaTReport.as_dict` and
:data:`REASON_NOT_A_POWER_MEASUREMENT` in its ``reason_codes``. That is a
contract, not decoration: it is what stops a downstream consumer (human or code)
from treating delta-T as heat output.

**The probes are monitoring inputs only.** They never gate, block or trigger a
boiler write. A broken probe must not change any control decision - it only
degrades the quality label on diagnostic entities (design doc 8, section 1
hard constraint 1).

Reading the hydraulic verdict correctly
---------------------------------------

The verdict *keys* follow docs/INTERFACES_V0.3.md section 4.2 verbatim, where a
delta-T below the healthy band maps to :data:`HYDRAULIC_LOW_FLOW` and one above
it maps to :data:`HYDRAULIC_HIGH_FLOW`. Read those two keys as **"the low
delta-T condition"** and **"the high delta-T condition"**. The physical cause is
usually the *opposite* direction of flow, which is why both are hydraulic
signals and **neither is a judgement about efficiency or quality**:

* a **low** delta-T on a constant-speed pump usually means **too much flow, or a
  short circuit through a bypass** - the water is moving faster than the
  emitters can extract heat from it;
* a **high** delta-T usually means **insufficient flow** - the water is giving up
  more heat than the circuit is being fed, which points at a throttled valve, a
  blocked strainer, air, or a pump curve that is too low.

Both are things to look at with a wrench, not a score. Nothing here is
"good" or "bad" in an efficiency sense, and none of it justifies overriding the
boiler's own safety logic (design doc 8).

Safety contract
---------------

1. **A missing reading is never a temperature.** ``supply``/``return_`` of
   ``None``, or a non-finite value, records **no sample** and never becomes
   ``0``. Treating it as zero would fabricate an enormous, entirely fictitious
   delta-T.
2. **A dropout breaks the settled run.** Time continuity is tracked explicitly:
   seeing a missing reading ends the current stable run, so a probe that goes
   away and comes back cannot have its before/after readings stitched into one
   "stable" span.
3. **Missing evidence is never "normal".** While either probe is currently
   unusable the verdict fields report :data:`HYDRAULIC_UNKNOWN` /
   :data:`CONDENSING_UNKNOWN` / :data:`CALIBRATION_INSUFFICIENT_DATA` - never
   ``OK``, ``LIKELY`` or ``NONE``. This is the V0.1 rule "never treat a missing
   or unknown value as 0 or as *normal*", applied to delta-T.
4. **Verdicts need a settled run.** ``hydraulic`` (and with it the curve advice)
   is only issued once ``stable_seconds >= min_stable_seconds``; anything
   shorter is dominated by valve and pump transients, so the verdict is
   :data:`HYDRAULIC_UNKNOWN` with :data:`REASON_DELTA_T_UNSTABLE`.
5. **Advice is advice.** ``calibration`` is a suggestion for a human. This engine
   never writes the weather curve - the curve stays owned by the user's
   configuration.

What is a "stable" run?
-----------------------

A run is the trailing sequence of recorded samples in which each neighbouring
pair differs by no more than :data:`STABLE_TOLERANCE_C` **and** no probe dropout
was recorded between them. ``stable_seconds`` is the wall-clock span of that run.
It is deliberately measured between the first and last *recorded* sample, so the
figure understates rather than overstates how long conditions have been settled.

Historical statistics versus current verdicts
---------------------------------------------

``average`` / ``minimum`` / ``maximum`` / ``trend_per_hour`` / ``samples``
describe the **recorded window**: they are facts about history and are reported
even while the probes are currently unavailable. ``delta_t``,
``hydraulic``, ``condensing`` and ``calibration`` describe **now** and are
subject to contract 3 above. Keeping the two apart lets a dashboard show "fine
right now, but delta-T was climbing all afternoon" without the current verdict
silently inheriting old evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    CALIBRATION_INSUFFICIENT_DATA,
    CALIBRATION_LOWER_CURVE,
    CALIBRATION_NONE,
    CALIBRATION_RAISE_CURVE,
    CONDENSING_LIKELY,
    CONDENSING_UNKNOWN,
    CONDENSING_UNLIKELY,
    DEFAULT_CONDENSING_RETURN_C,
    DEFAULT_DELTA_T_HEALTHY_MAX,
    DEFAULT_DELTA_T_HEALTHY_MIN,
    DEFAULT_DELTA_T_MIN_STABLE_SECONDS,
    DEFAULT_DELTA_T_SAMPLE_INTERVAL,
    DEFAULT_DELTA_T_WINDOW,
    HYDRAULIC_HIGH_FLOW,
    HYDRAULIC_LOW_FLOW,
    HYDRAULIC_OK,
    HYDRAULIC_UNKNOWN,
    PROBE_QUALITIES,
    PROBE_UNAVAILABLE,
    PROBE_VALID,
    REASON_DELTA_T_HIGH,
    REASON_DELTA_T_LOW,
    REASON_DELTA_T_OK,
    REASON_DELTA_T_UNSTABLE,
    REASON_NOT_A_POWER_MEASUREMENT,
    REASON_PROBE_MISSING,
)

#: Maximum delta-T drift between two neighbouring samples still counted as the
#: same settled run, in degrees C. A wet floor-heating loop settles within a
#: degree or so; anything larger is a valve, pump or mixing transient.
#:
#: This is scoped here rather than in ``const.py`` because ``const.py`` is frozen
#: for V0.3. It is a *stability* criterion, not a user-facing threshold.
STABLE_TOLERANCE_C: float = 1.0

#: Hard cap on retained samples, so a resumed or hostile payload cannot unbound
#: memory. With the default 60 s interval an hour-long window holds ~60.
MAX_SAMPLES = 4096
MAX_BREAKS = 4096

#: Report reason codes that are not part of the frozen ``const`` block.
#: Scoped here for the same reason, following the ``REASON_*`` naming convention.
REASON_CALIBRATION_CONFLICT = "curve_advice_withheld_conflicting_zone_evidence"

#: Severity order for the "worse of the two probes wins" rule. ``PROBE_QUALITIES``
#: is documented as increasing severity, so the *last* match is the worst one.
_SEVERITY = {quality: index for index, quality in enumerate(PROBE_QUALITIES)}


@dataclass(frozen=True, slots=True)
class DeltaTSample:
    """One recorded delta-T observation (both probes were usable)."""

    at: datetime
    delta_t: float
    supply_c: float
    return_c: float


@dataclass(frozen=True, slots=True)
class DeltaTReport:
    """Delta-T trend and its indirect hydraulic/condensing reading.

    ``is_power_measurement`` is always ``False``: delta-T is an indirect flow and
    condensing indicator, never heat output or efficiency (see the module
    docstring). It is surfaced explicitly so no consumer has to guess.
    """

    delta_t: float | None
    average: float | None
    minimum: float | None
    maximum: float | None
    trend_per_hour: float | None
    stable_seconds: float
    samples: int
    quality: str
    condensing: str
    hydraulic: str
    calibration: str
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly export for entity attributes/diagnostics."""
        return {
            "delta_t": _round_opt(self.delta_t),
            "average": _round_opt(self.average),
            "minimum": _round_opt(self.minimum),
            "maximum": _round_opt(self.maximum),
            "trend_per_hour": _round_opt(self.trend_per_hour),
            "stable_seconds": round(self.stable_seconds, 1),
            "samples": self.samples,
            "quality": self.quality,
            "condensing": self.condensing,
            "hydraulic": self.hydraulic,
            "calibration": self.calibration,
            "reason_codes": list(self.reason_codes),
            #: HARD CONSTRAINT (docs/INTERFACES_V0.3.md section 1): a temperature
            #: difference is not heat power. Always False, on purpose, so that
            #: neither a human nor a dashboard template can mistake delta-T for
            #: thermal output or efficiency.
            "is_power_measurement": False,
        }


@dataclass(frozen=True, slots=True)
class _Entry:
    """Internal sample plus the context the public sample shape does not carry.

    ``DeltaTSample`` is fixed by the interface contract at four fields, but
    condensing needs to know whether the boiler was burning and the curve advice
    needs the zone evidence. Keeping them in one internal record (instead of
    parallel lists) makes desynchronisation impossible.
    """

    sample: DeltaTSample
    burning: bool
    underheating: int
    overheating: int


class DeltaTDiagnostics:
    """Accumulates supply/return readings and grades the loop on demand."""

    def __init__(
        self,
        *,
        window: float = DEFAULT_DELTA_T_WINDOW,
        sample_interval: float = DEFAULT_DELTA_T_SAMPLE_INTERVAL,
        min_stable_seconds: float = DEFAULT_DELTA_T_MIN_STABLE_SECONDS,
        healthy_min: float = DEFAULT_DELTA_T_HEALTHY_MIN,
        healthy_max: float = DEFAULT_DELTA_T_HEALTHY_MAX,
        condensing_return_c: float = DEFAULT_CONDENSING_RETURN_C,
    ) -> None:
        self._window = max(0.0, float(window))
        self._sample_interval = max(0.0, float(sample_interval))
        self._min_stable_seconds = max(0.0, float(min_stable_seconds))
        self._healthy_min = float(healthy_min)
        self._healthy_max = float(healthy_max)
        self._condensing_return_c = float(condensing_return_c)

        self._entries: list[_Entry] = []
        self._breaks: list[datetime] = []
        self._last_sample_at: datetime | None = None
        self._last_note_at: datetime | None = None
        self._current_delta_t: float | None = None
        self._supply_quality: str = PROBE_UNAVAILABLE
        self._return_quality: str = PROBE_UNAVAILABLE

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def note(
        self,
        *,
        supply: float | None,
        return_: float | None,
        burning: bool,
        active_zones: int,
        now: datetime,
        underheating_zones: int = 0,
        overheating_zones: int = 0,
    ) -> None:
        """Feed one observation of the supply/return probe pair.

        A ``None`` (or non-finite) reading on **either** probe records no sample
        and is never interpreted as ``0``. It also marks a discontinuity in the
        evidence, so a dropout splits the settled run instead of letting the
        readings either side of it be stitched together.

        Usable readings are throttled to one recorded sample per
        ``sample_interval``. The quality labels and the current delta-T are
        refreshed on every call regardless of the throttle, so a probe dropping
        out is reflected immediately even when no sample is due to be recorded.

        ``active_zones`` and ``burning`` are accepted because they describe the
        context a hydraulic reading is only meaningful within (a delta-T taken
        with no zones open says nothing about the loop).
        """
        self._last_note_at = now
        supply_value = _finite(supply)
        return_value = _finite(return_)

        self._supply_quality = _side_quality(supply_value)
        self._return_quality = _side_quality(return_value)

        if supply_value is None or return_value is None:
            # Missing reading: no sample, and the settled run must not span it.
            self._current_delta_t = None
            if not self._breaks or self._breaks[-1] != now:
                self._breaks.append(now)
            self._prune(now)
            return

        self._current_delta_t = supply_value - return_value

        if (
            self._last_sample_at is not None
            and self._sample_interval > 0.0
            and (now - self._last_sample_at).total_seconds() < self._sample_interval
        ):
            # Throttled: the previous sample still describes this moment, so only
            # the time resolution is lost. Memory stays bounded.
            self._prune(now)
            return

        self._entries.append(
            _Entry(
                sample=DeltaTSample(
                    at=now,
                    delta_t=self._current_delta_t,
                    supply_c=supply_value,
                    return_c=return_value,
                ),
                burning=bool(burning),
                underheating=_count(underheating_zones),
                overheating=_count(overheating_zones),
            )
        )
        self._last_sample_at = now
        self._prune(now)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def report(self, now: datetime) -> DeltaTReport:
        """Return the delta-T report for ``now``.

        Reading is idempotent for a fixed ``now``: calling it repeatedly yields an
        equal report. The only side effect is pruning data outside the window
        (which is itself idempotent for a fixed ``now``), so the coordinator may
        call this as often as it likes.
        """
        self._prune(now)

        # Evidence stops at the last time we were actually fed; a stalled feed
        # must not let one old sample describe an ever-growing span.
        horizon = now if self._last_note_at is None else min(now, self._last_note_at)
        cutoff = now - timedelta(seconds=self._window)
        window = [entry for entry in self._entries if cutoff <= entry.sample.at <= horizon]

        samples = len(window)
        average = _mean([entry.sample.delta_t for entry in window]) if window else None
        minimum = min((entry.sample.delta_t for entry in window), default=None)
        maximum = max((entry.sample.delta_t for entry in window), default=None)
        trend = _trend_per_hour(window)

        run = self._stable_run(window)
        stable_seconds = (run[-1].sample.at - run[0].sample.at).total_seconds() if run else 0.0
        settled = bool(run) and stable_seconds >= self._min_stable_seconds

        quality = _worse_quality(self._supply_quality, self._return_quality)
        # Contract 3: a missing reading is never "normal". Without both probes
        # right now there is no current hydraulic or condensing state to report,
        # so every verdict field stays UNKNOWN/INSUFFICIENT_DATA rather than
        # defaulting to OK/LIKELY/NONE.
        #
        # Note on the quality label: the frozen signature passes bare values, so
        # this engine can only distinguish "usable" from "missing". The richer
        # stale/frozen/implausible verdicts belong to ``probe_monitor`` and reach
        # the UI through the coordinator, which passes ``None`` here for any probe
        # it judged unusable.
        probes_usable = quality == PROBE_VALID

        reasons: list[str] = [REASON_NOT_A_POWER_MEASUREMENT]
        if not probes_usable:
            reasons.append(REASON_PROBE_MISSING)

        hydraulic = HYDRAULIC_UNKNOWN
        condensing = CONDENSING_UNKNOWN
        calibration = CALIBRATION_INSUFFICIENT_DATA

        if probes_usable and settled and run is not None:
            run_average = _mean([entry.sample.delta_t for entry in run])
            if run_average < self._healthy_min:
                hydraulic = HYDRAULIC_LOW_FLOW
                reasons.append(REASON_DELTA_T_LOW)
            elif run_average > self._healthy_max:
                hydraulic = HYDRAULIC_HIGH_FLOW
                reasons.append(REASON_DELTA_T_HIGH)
            else:
                hydraulic = HYDRAULIC_OK
                reasons.append(REASON_DELTA_T_OK)

            burning = [entry for entry in run if entry.burning]
            if burning:
                mean_return = _mean([entry.sample.return_c for entry in burning])
                condensing = (
                    CONDENSING_LIKELY
                    if mean_return <= self._condensing_return_c
                    else CONDENSING_UNLIKELY
                )

            calibration = self._calibration(window, reasons)
        else:
            # No settled evidence (probes missing, or too short a run): the
            # hydraulic verdict cannot be issued, and neither can the curve
            # advice, which depends on knowing the loop is actually settled.
            reasons.append(REASON_DELTA_T_UNSTABLE)

        return DeltaTReport(
            delta_t=self._current_delta_t if probes_usable else None,
            average=average,
            minimum=minimum,
            maximum=maximum,
            trend_per_hour=trend,
            stable_seconds=stable_seconds,
            samples=samples,
            quality=quality,
            condensing=condensing,
            hydraulic=hydraulic,
            calibration=calibration,
            reason_codes=tuple(reasons),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable state (round-trips through :meth:`restore`)."""
        return {
            "entries": [
                [
                    _dump_dt(entry.sample.at),
                    entry.sample.delta_t,
                    entry.sample.supply_c,
                    entry.sample.return_c,
                    entry.burning,
                    entry.underheating,
                    entry.overheating,
                ]
                for entry in self._entries
            ],
            "breaks": [_dump_dt(at) for at in self._breaks],
            "last_sample_at": _dump_dt(self._last_sample_at),
            "last_note_at": _dump_dt(self._last_note_at),
            "current_delta_t": self._current_delta_t,
            "supply_quality": self._supply_quality,
            "return_quality": self._return_quality,
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore a :meth:`snapshot`; malformed input yields a clean state.

        Tolerant on purpose: this runs while loading persisted state, and a
        payload written by an older version (or hand-edited) must not break
        setup. Unknown keys and unusable entries are skipped, and any internal
        inconsistency is repaired rather than trusted.
        """
        self._entries = []
        self._breaks = []
        self._last_sample_at = None
        self._last_note_at = None
        self._current_delta_t = None
        self._supply_quality = PROBE_UNAVAILABLE
        self._return_quality = PROBE_UNAVAILABLE

        if not data or not isinstance(data, dict):
            return

        self._entries = _opt_entries(data.get("entries"))
        self._breaks = [at for at in _opt_dts(data.get("breaks")) if at is not None]
        self._last_sample_at = _opt_dt(data.get("last_sample_at"))
        self._last_note_at = _opt_dt(data.get("last_note_at"))
        self._current_delta_t = _finite(data.get("current_delta_t"))
        self._supply_quality = _opt_quality(data.get("supply_quality"))
        self._return_quality = _opt_quality(data.get("return_quality"))

        # Consistency repair. The throttle clock cannot precede the newest sample.
        if self._entries:
            newest = self._entries[-1].sample
            if self._last_sample_at is None or self._last_sample_at < newest.at:
                self._last_sample_at = newest.at
        if self._last_note_at is None:
            self._last_note_at = self._last_sample_at

        # A "usable, current reading" claim must be backed by evidence. A payload
        # carrying a quality label and a delta-T but no samples at all is either
        # corrupt or hand-edited, and honouring it would report a live reading
        # that was never observed - the exact fabrication this engine exists to
        # avoid (a probe that was never read must not look healthy).
        #
        # Note the deliberate asymmetry: a legitimate snapshot taken *while*
        # throttled legitimately carries a ``current_delta_t`` newer than the
        # newest recorded sample, so the current value is NOT forced to match the
        # newest sample. Only an entirely unbacked claim is rejected.
        if self._current_delta_t is None or not self._entries:
            self._current_delta_t = None
            self._supply_quality = PROBE_UNAVAILABLE
            self._return_quality = PROBE_UNAVAILABLE

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _stable_run(self, window: list[_Entry]) -> list[_Entry]:
        """Return the trailing settled run of ``window`` (may be a single item).

        Neighbours must be within :data:`STABLE_TOLERANCE_C` of each other and no
        probe dropout may have been recorded between them.
        """
        if not window:
            return []
        run_reversed: list[_Entry] = [window[-1]]
        for index in range(len(window) - 2, -1, -1):
            current = window[index]
            following = run_reversed[-1]
            if abs(following.sample.delta_t - current.sample.delta_t) > STABLE_TOLERANCE_C:
                break
            if self._break_between(current.sample.at, following.sample.at):
                break
            run_reversed.append(current)
        run_reversed.reverse()
        return run_reversed

    def _break_between(self, start: datetime, end: datetime) -> bool:
        """True when a dropout was recorded in ``(start, end]``."""
        return any(start < at <= end for at in self._breaks)

    def _calibration(self, window: list[_Entry], reasons: list[str]) -> str:
        """Derive the advisory curve suggestion from window zone evidence.

        Pure advice - the curve belongs to the user's configuration and is never
        written from here.

        Conflicting evidence (some rooms underheating while others overheat) is
        deliberately answered with :data:`CALIBRATION_NONE`: a *global* curve
        change cannot fix a distribution problem, and raising the supply while a
        room is already too warm would trade one fault for another. The withheld
        advice is explained by :data:`REASON_CALIBRATION_CONFLICT`.
        """
        under = any(entry.underheating > 0 for entry in window)
        over = any(entry.overheating > 0 for entry in window)
        if under and over:
            reasons.append(REASON_CALIBRATION_CONFLICT)
            return CALIBRATION_NONE
        if under:
            return CALIBRATION_RAISE_CURVE
        if over:
            return CALIBRATION_LOWER_CURVE
        return CALIBRATION_NONE

    def _prune(self, now: datetime) -> None:
        """Drop everything outside the window, so the window is a real bound.

        Unlike the comfort analyser this engine does no time-weighted
        attribution, so there is no "state at the cutoff" to preserve. Keeping a
        pre-cutoff sample would buy nothing and would make the window
        non-monotonic: asking for an earlier ``now`` after a later one could
        resurrect a sample, so the sample count would depend on query order
        rather than on the data. Dropping strictly by the window keeps
        :meth:`report` a pure function of the retained window and ``now``.
        """
        cutoff = now - timedelta(seconds=self._window)
        self._entries = [entry for entry in self._entries if entry.sample.at >= cutoff][
            -MAX_SAMPLES:
        ]
        self._breaks = [at for at in self._breaks if at >= cutoff][-MAX_BREAKS:]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------
def _side_quality(value: float | None) -> str:
    """Quality of one probe side, from the reading alone.

    The interface signature passes bare values rather than an assessment, so a
    side can only be "usable" or "missing". ``probe_monitor`` owns the richer
    stale/frozen/implausible verdicts; entity-level quality is assembled there.
    """
    return PROBE_VALID if value is not None else PROBE_UNAVAILABLE


def _worse_quality(left: str, right: str) -> str:
    """Return the worse of two qualities (``PROBE_QUALITIES`` is increasing)."""
    return left if _severity(left) >= _severity(right) else right


def _severity(quality: str) -> int:
    """Severity rank of ``quality``; anything unrecognised ranks worst."""
    return _SEVERITY.get(quality, len(PROBE_QUALITIES))


def _trend_per_hour(entries: list[_Entry]) -> float | None:
    """Least-squares slope of delta-T over the window, in degrees C per hour.

    Positive means the delta-T is widening. Uses a fit rather than first-to-last
    so one noisy endpoint cannot define the whole trend. Needs at least two
    samples at distinct times; otherwise there is no trend to speak of.
    """
    if len(entries) < 2:
        return None
    origin = entries[0].sample.at
    xs = [(entry.sample.at - origin).total_seconds() for entry in entries]
    ys = [entry.sample.delta_t for entry in entries]
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0.0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return slope / denominator * 3600.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _count(value: Any) -> int:
    """Coerce a zone count; negative or junk counts become 0."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _round_opt(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


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


def _opt_quality(value: Any) -> str:
    """Validate a persisted quality label; unknown labels mean unavailable."""
    if isinstance(value, str) and value in _SEVERITY:
        return value
    return PROBE_UNAVAILABLE


def _opt_entries(raw: Any) -> list[_Entry]:
    out: list[_Entry] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        at = _opt_dt(item[0])
        delta_t = _finite(item[1])
        supply = _finite(item[2])
        return_c = _finite(item[3])
        if at is None or delta_t is None or supply is None or return_c is None:
            continue
        out.append(
            _Entry(
                sample=DeltaTSample(at=at, delta_t=delta_t, supply_c=supply, return_c=return_c),
                burning=bool(item[4]) if len(item) > 4 else False,
                underheating=_count(item[5]) if len(item) > 5 else 0,
                overheating=_count(item[6]) if len(item) > 6 else 0,
            )
        )
    out.sort(key=lambda entry: entry.sample.at)
    return out[-MAX_SAMPLES:]


def _opt_dts(raw: Any) -> list[datetime | None]:
    if not isinstance(raw, list):
        return []
    return [_opt_dt(item) for item in raw]


def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
