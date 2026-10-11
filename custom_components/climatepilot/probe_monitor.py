"""Pipe-probe quality assessment: refuse a reading we cannot trust (V0.3).

Pure engine: this module **must not import Home Assistant** (docs/INTERFACES.md
section 0.1) so it can be unit tested without booting HA, and it may only depend
on the standard library plus ``.const``. Every timestamp arrives through the
``now`` argument; the wall clock is never read here.

Why this module exists
----------------------

An external DS18B20 strapped to a heating pipe is cheap, useful and easy to
trust too much. The design doc (section 8) lists its failure modes and requires
that a broken probe "only affects diagnostics, never the heating". This engine
is the single place that decides whether a raw reading may be believed, so that
the rest of the integration never has to.

**Judgement order is contractual** (INTERFACES_V0.3 section 4.1, rules 1-6) and
is deliberate - the first rule that fires wins:

1. no value at all -> :data:`PROBE_UNAVAILABLE` (+ :data:`REASON_PROBE_MISSING`).
   Non-finite values (``NaN``/``inf``) and non-numeric strings land here too:
   ``NaN`` compares ``False`` against every bound, so a naive range check would
   let it through as a plausible temperature, and a reading that is not a number
   is not a temperature.
2. **the DS18B20 sentinel** -> :data:`PROBE_IMPLAUSIBLE`
   (+ :data:`REASON_PROBE_SENTINEL`) when
   ``abs(value - sentinel) <= sentinel_tolerance`` (85.0 +/- 0.5 by default).
   This is the most important check in the module. A DS18B20 reports *exactly*
   85 degrees C when its conversion fails or the bus breaks - which looks like a
   very hot supply pipe. Unchecked it either invents a boiler fault that does
   not exist, or produces an enormous delta-T that looks like a hydraulic
   success. It is also why the sentinel is checked *before* the range check:
   when a user narrows the plausible window, 85 still has to be reported as the
   sentinel, because that is the honest explanation.
3. outside ``[min_value, max_value]`` -> :data:`PROBE_IMPLAUSIBLE`
   (+ :data:`REASON_PROBE_OUT_OF_RANGE`). The bounds are inclusive.
4. older than ``stale_after`` -> :data:`PROBE_STALE` (+ :data:`REASON_PROBE_STALE`).
   A missing timestamp also lands here: without one, freshness cannot be proven,
   so the reading is not trusted. A timestamp in the *future* (clock skew) is
   treated as fresh per rule 4's literal ``age > stale_after``; the negative age
   is still reported so the skew is visible.
5. **frozen** -> :data:`PROBE_FROZEN` (+ :data:`REASON_PROBE_FROZEN`) when the
   value has been *exactly* identical for longer than ``frozen_after`` **and the
   boiler is burning**. A cooling, idle pipe legitimately holds one value for
   hours, so reporting frozen while not burning would be a false alarm - and a
   diagnostic that cries wolf is worse than no diagnostic. The unchanged span is
   tracked regardless, so the verdict becomes available the moment a burn starts.
6. otherwise :data:`PROBE_VALID`, with no reason codes.

``value`` is non-``None`` **only** on a :data:`PROBE_VALID` assessment: a
stale, frozen or implausible number must not be readable as a temperature by a
caller that forgets to check ``quality``.

Pair assessment
---------------

:meth:`ProbeMonitor.assess_pair` compares the supply and return probes on one
hydraulic loop. Two rules matter:

* if either probe is unusable, ``divergence`` is ``None``. A one-sided reading
  is never turned into a delta-T by treating the missing side as zero or by
  reusing a stale number;
* when both are usable but disagree by more than ``max_divergence``, **both** are
  downgraded to :data:`PROBE_IMPLAUSIBLE` and :data:`REASON_PROBE_DIVERGENT` is
  recorded. There is no way to tell which probe is wrong, so neither is
  believable. The size of the disagreement is kept in ``divergence`` (it is the
  diagnostic); the individual values are not.

Monitoring only
---------------

The public surface is exactly ``assess``, ``assess_pair``, ``snapshot`` and
``restore`` - deliberately nothing that could write, block or authorise a
boiler command. Probes are monitoring/optimisation inputs and never carry a
safety shutdown function (design doc 8, INTERFACES_V0.3 section 1): a failed
probe degrades the diagnostics, never the heating.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .const import (
    DEFAULT_PROBE_FROZEN_AFTER,
    DEFAULT_PROBE_MAX_C,
    DEFAULT_PROBE_MAX_DIVERGENCE,
    DEFAULT_PROBE_MIN_C,
    DEFAULT_PROBE_STALE_AFTER,
    DS18B20_ERROR_C,
    DS18B20_ERROR_TOLERANCE,
    PROBE_FROZEN,
    PROBE_IMPLAUSIBLE,
    PROBE_STALE,
    PROBE_UNAVAILABLE,
    PROBE_VALID,
    REASON_PROBE_DIVERGENT,
    REASON_PROBE_FROZEN,
    REASON_PROBE_MISSING,
    REASON_PROBE_OUT_OF_RANGE,
    REASON_PROBE_SENTINEL,
    REASON_PROBE_STALE,
)


@dataclass(frozen=True, slots=True)
class ProbeReading:
    """One raw observation of a pipe probe, before any judgement."""

    entity_id: str
    value: float | None
    #: ``state.last_updated``. Optional because "no timestamp" is itself a
    #: meaningful reading - freshness cannot be proven, so the monitor treats it
    #: as stale rather than guessing - and because the caller may construct a
    #: deliberately empty reading ("no usable probe") without inventing one.
    updated_at: datetime | None = None
    raw: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeAssessment:
    """A probe reading plus the quality verdict and why."""

    entity_id: str
    value: float | None
    quality: str
    reason_codes: tuple[str, ...]
    age_seconds: float | None
    frozen_seconds: float | None

    @property
    def usable(self) -> bool:
        """True only for :data:`PROBE_VALID` - the single trust predicate."""
        return self.quality == PROBE_VALID

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly export for entity attributes/diagnostics."""
        return {
            "entity_id": self.entity_id,
            "value": _round_opt(self.value, 3),
            "quality": self.quality,
            "usable": self.usable,
            "age_seconds": _round_opt(self.age_seconds, 1),
            "frozen_seconds": _round_opt(self.frozen_seconds, 1),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ProbePairAssessment:
    """Two probes on the same hydraulic loop."""

    supply: ProbeAssessment
    return_: ProbeAssessment
    divergence: float | None
    reason_codes: tuple[str, ...]

    @property
    def both_usable(self) -> bool:
        """True only when *both* sides are :data:`PROBE_VALID`."""
        return self.supply.usable and self.return_.usable

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly export for entity attributes/diagnostics."""
        return {
            "supply": self.supply.as_dict(),
            # "return" is the JSON spelling; the Python attribute keeps the
            # trailing underscore because ``return`` is a keyword.
            "return": self.return_.as_dict(),
            "divergence": _round_opt(self.divergence, 3),
            "both_usable": self.both_usable,
            "reason_codes": list(self.reason_codes),
            #: Hard constraints, stated in the payload so no consumer has to
            #: infer them (INTERFACES_V0.3 section 1). Probes are a monitoring
            #: input - they never gate or trigger a boiler write - and a
            #: temperature difference is *not* heat power or efficiency.
            "is_control_input": False,
            "is_power_measurement": False,
        }


class ProbeMonitor:
    """Judges raw pipe-probe readings and the pair they form.

    Holds only the bookkeeping needed to detect a frozen value, which is why
    :meth:`assess` has a side effect: it records when each entity's value last
    changed. Calling it again with the same inputs is idempotent.
    """

    def __init__(
        self,
        *,
        stale_after: float = DEFAULT_PROBE_STALE_AFTER,
        frozen_after: float = DEFAULT_PROBE_FROZEN_AFTER,
        min_value: float = DEFAULT_PROBE_MIN_C,
        max_value: float = DEFAULT_PROBE_MAX_C,
        max_divergence: float = DEFAULT_PROBE_MAX_DIVERGENCE,
        sentinel: float = DS18B20_ERROR_C,
        sentinel_tolerance: float = DS18B20_ERROR_TOLERANCE,
    ) -> None:
        self._stale_after = float(stale_after)
        self._frozen_after = float(frozen_after)
        self._min_value = float(min_value)
        self._max_value = float(max_value)
        self._max_divergence = float(max_divergence)
        self._sentinel = float(sentinel)
        self._sentinel_tolerance = float(sentinel_tolerance)
        #: Per-entity "value last seen" / "when it last changed", which is what
        #: makes a frozen probe distinguishable from a steady one.
        self._last_values: dict[str, float] = {}
        self._last_changed_at: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Single probe
    # ------------------------------------------------------------------
    def assess(
        self,
        reading: ProbeReading,
        now: datetime,
        *,
        burning: bool = False,
    ) -> ProbeAssessment:
        """Judge one reading, in the contractual rule order 1 to 6.

        ``burning`` only gates rule 5 (frozen). A value that has not moved for
        hours is normal on an idle pipe and suspicious only while the boiler is
        firing.
        """
        entity_id = reading.entity_id
        age = _age_seconds(reading.updated_at, now)
        value = _finite(reading.value)

        # Rule 1: nothing usable to judge. The frozen baseline is dropped as
        # well - while a value could not be read we cannot claim it was constant.
        if value is None:
            self._forget(entity_id)
            return ProbeAssessment(
                entity_id=entity_id,
                value=None,
                quality=PROBE_UNAVAILABLE,
                reason_codes=(REASON_PROBE_MISSING,),
                age_seconds=age,
                frozen_seconds=None,
            )

        frozen_seconds = self._track(entity_id, value, now)

        # Rule 2: the DS18B20 failure sentinel, before anything else.
        if abs(value - self._sentinel) <= self._sentinel_tolerance:
            return ProbeAssessment(
                entity_id=entity_id,
                value=None,
                quality=PROBE_IMPLAUSIBLE,
                reason_codes=(REASON_PROBE_SENTINEL,),
                age_seconds=age,
                frozen_seconds=frozen_seconds,
            )

        # Rule 3: outside the plausible window for a pipe surface probe.
        if value < self._min_value or value > self._max_value:
            return ProbeAssessment(
                entity_id=entity_id,
                value=None,
                quality=PROBE_IMPLAUSIBLE,
                reason_codes=(REASON_PROBE_OUT_OF_RANGE,),
                age_seconds=age,
                frozen_seconds=frozen_seconds,
            )

        # Rule 4: too old to describe the present, or no timestamp at all.
        if age is None or age > self._stale_after:
            return ProbeAssessment(
                entity_id=entity_id,
                value=None,
                quality=PROBE_STALE,
                reason_codes=(REASON_PROBE_STALE,),
                age_seconds=age,
                frozen_seconds=frozen_seconds,
            )

        # Rule 5: frozen, and only while the boiler is actually burning.
        if burning and frozen_seconds is not None and frozen_seconds > self._frozen_after:
            return ProbeAssessment(
                entity_id=entity_id,
                value=None,
                quality=PROBE_FROZEN,
                reason_codes=(REASON_PROBE_FROZEN,),
                age_seconds=age,
                frozen_seconds=frozen_seconds,
            )

        # Rule 6: believable.
        return ProbeAssessment(
            entity_id=entity_id,
            value=value,
            quality=PROBE_VALID,
            reason_codes=(),
            age_seconds=age,
            frozen_seconds=frozen_seconds,
        )

    # ------------------------------------------------------------------
    # Pair
    # ------------------------------------------------------------------
    def assess_pair(
        self,
        supply: ProbeReading,
        return_: ProbeReading,
        now: datetime,
        *,
        burning: bool = False,
    ) -> ProbePairAssessment:
        """Judge both probes and the disagreement between them."""
        supply_result = self.assess(supply, now, burning=burning)
        return_result = self.assess(return_, now, burning=burning)

        # A one-sided reading is never turned into a delta-T.
        if not (supply_result.usable and return_result.usable):
            return ProbePairAssessment(
                supply=supply_result,
                return_=return_result,
                divergence=None,
                reason_codes=(),
            )

        divergence = abs(supply_result.value - return_result.value)
        if divergence <= self._max_divergence:
            return ProbePairAssessment(
                supply=supply_result,
                return_=return_result,
                divergence=divergence,
                reason_codes=(),
            )

        # Neither probe can be trusted when they disagree: which one is wrong is
        # unknowable from here, so both are refused. The disagreement itself is
        # kept - it is the evidence for the verdict.
        downgrade = (REASON_PROBE_DIVERGENT,)
        supply_result = replace(
            supply_result,
            value=None,
            quality=PROBE_IMPLAUSIBLE,
            reason_codes=supply_result.reason_codes + downgrade,
        )
        return_result = replace(
            return_result,
            value=None,
            quality=PROBE_IMPLAUSIBLE,
            reason_codes=return_result.reason_codes + downgrade,
        )
        return ProbePairAssessment(
            supply=supply_result,
            return_=return_result,
            divergence=divergence,
            reason_codes=downgrade,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable state (round-trips through :meth:`restore`)."""
        return {
            "last_values": dict(self._last_values),
            "last_changed_at": {
                entity_id: at.isoformat() for entity_id, at in self._last_changed_at.items()
            },
        }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore a :meth:`snapshot`; malformed input yields a clean state.

        Tolerant on purpose: this runs while persisted state is loaded, and a
        payload written by an older version (or hand-edited) must not break
        setup. Anything that cannot be parsed as a paired (value, timestamp)
        entry is dropped rather than guessed at.
        """
        self._last_values = {}
        self._last_changed_at = {}

        if not data or not isinstance(data, dict):
            return

        raw_values = data.get("last_values")
        raw_times = data.get("last_changed_at")
        if not isinstance(raw_values, dict) or not isinstance(raw_times, dict):
            return

        for entity_id, raw_value in raw_values.items():
            if not isinstance(entity_id, str) or not entity_id:
                continue
            value = _finite(raw_value)
            changed_at = _opt_dt(raw_times.get(entity_id))
            # A value with no usable timestamp cannot support a frozen verdict.
            if value is None or changed_at is None:
                continue
            self._last_values[entity_id] = value
            self._last_changed_at[entity_id] = changed_at

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _track(self, entity_id: str, value: float, now: datetime) -> float | None:
        """Record ``value`` and return how long it has been unchanged.

        Returns ``None`` when the reading cannot be attributed to a probe (no
        ``entity_id``): without an identity, "this is the same probe as last
        time" cannot be claimed, so no frozen verdict is possible.

        The *raw resolved value* is tracked, including a sentinel or
        out-of-range one. That matters: a probe stuck at exactly 85 C and then
        recovering has genuinely changed value, so it must not be reported as
        frozen the moment it comes back.
        """
        if not entity_id:
            return None

        if self._last_values.get(entity_id) == value:
            changed_at = self._last_changed_at.get(entity_id)
            if changed_at is None:  # pragma: no cover - pairs are written together
                self._last_changed_at[entity_id] = now
                return 0.0
            # A duration cannot be negative; a backwards clock reads as "no
            # elapsed time" instead of a nonsensical negative span.
            return max(0.0, (now - changed_at).total_seconds())

        self._last_values[entity_id] = value
        self._last_changed_at[entity_id] = now
        return 0.0

    def _forget(self, entity_id: str) -> None:
        """Drop the frozen baseline for ``entity_id`` (observation interrupted)."""
        if not entity_id:
            return
        self._last_values.pop(entity_id, None)
        self._last_changed_at.pop(entity_id, None)


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it is not one.

    ``NaN`` and the infinities are rejected here, which is what keeps them out
    of the range check in rule 3.
    """
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _age_seconds(updated_at: datetime | None, now: datetime) -> float | None:
    """Seconds between ``updated_at`` and ``now``; ``None`` when unknowable.

    A naive/aware mismatch is not a crash but an unknowable age, and an
    unknowable age is treated as stale by rule 4.
    """
    if updated_at is None:
        return None
    try:
        return (now - updated_at).total_seconds()
    except TypeError:
        return None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _round_opt(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
