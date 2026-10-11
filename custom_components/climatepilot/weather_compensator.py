"""Outdoor temperature -> supply temperature compensation (design doc 7).

Pure engine: this module **must not import Home Assistant** (docs/INTERFACES.md
section 0.1) so it can be unit tested without booting HA. Every timestamp is
passed in by the caller through the ``now`` argument; the module never reads the
wall clock itself.

Safety contract
---------------

* A stale, unavailable or non-finite outdoor reading **never** produces a new
  computation. The last *confirmed* value is kept instead and
  :data:`REASON_WEATHER_STALE` explains why. In particular the engine must
  never fall back to 0 degrees C (design doc 7, INTERFACES 2.2).
* ``WEATHER_MODE_SUGGEST`` (and any unrecognised mode) never returns
  ``should_apply=True``; it only produces a value for display/debugging.
* Hysteresis and the minimum interval are measured against the last value that
  was actually written downstream (announced through :meth:`note_applied`).

Reason code order in :class:`CompensationResult`: staleness first, then
``REASON_RANGE_CLAMP``, then ``REASON_TOLERANCE`` / ``REASON_THROTTLED``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import (
    DEFAULT_WEATHER_HYSTERESIS,
    DEFAULT_WEATHER_MIN_INTERVAL,
    DEFAULT_WEATHER_STALE_AFTER,
    FEEDBACK_STALE,
    FEEDBACK_UNAVAILABLE,
    FEEDBACK_VALID,
    REASON_RANGE_CLAMP,
    REASON_THROTTLED,
    REASON_TOLERANCE,
    REASON_WEATHER_STALE,
    WEATHER_MODE_APPLY,
    WEATHER_MODE_SUGGEST,
)
from .models import WeatherCurve


@dataclass(frozen=True, slots=True)
class WeatherReading:
    """One sample of the configured outdoor temperature entity."""

    value: float | None
    updated_at: datetime | None
    quality: str


@dataclass(frozen=True, slots=True)
class CompensationResult:
    """Outcome of one :meth:`WeatherCompensator.evaluate` call."""

    suggested: float | None
    outdoor: float | None
    quality: str
    reason_codes: tuple[str, ...]
    should_apply: bool


class WeatherCompensator:
    """Linear weather curve with clamping, hysteresis and write throttling."""

    def __init__(
        self,
        curve: WeatherCurve,
        *,
        stale_after: float = DEFAULT_WEATHER_STALE_AFTER,
        hysteresis: float = DEFAULT_WEATHER_HYSTERESIS,
        min_interval: float = DEFAULT_WEATHER_MIN_INTERVAL,
        mode: str = WEATHER_MODE_SUGGEST,
    ) -> None:
        self._curve = curve
        self._stale_after = float(stale_after)
        self._hysteresis = float(hysteresis)
        self._min_interval = float(min_interval)
        self._mode = mode
        self._last_applied: float | None = None
        self._last_applied_at: datetime | None = None
        self._last_suggested: float | None = None

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate(
        self,
        reading: WeatherReading,
        now: datetime,
        *,
        night: bool = False,
    ) -> CompensationResult:
        """Return the compensated supply setpoint for ``reading``.

        ``suggested`` keeps the last confirmed value while the reading is
        unusable, so callers can keep the boiler on its last safe target.
        ``should_apply`` is the only signal that may lead to a write.
        """
        quality, outdoor = self._resolve(reading, now)
        if quality != FEEDBACK_VALID or outdoor is None:
            fallback = (
                self._last_suggested if self._last_suggested is not None else self._last_applied
            )
            return CompensationResult(
                suggested=fallback,
                outdoor=None,
                quality=quality,
                reason_codes=(REASON_WEATHER_STALE,),
                should_apply=False,
            )

        suggested = self._curve.supply_for(outdoor, night=night)
        self._last_suggested = suggested

        reasons: list[str] = []
        if abs(self._raw_supply(outdoor, night) - suggested) > 1e-9:
            reasons.append(REASON_RANGE_CLAMP)

        should_apply = False
        if self._mode == WEATHER_MODE_APPLY:
            blocked = False
            if (
                self._last_applied is not None
                and abs(suggested - self._last_applied) < self._hysteresis
            ):
                reasons.append(REASON_TOLERANCE)
                blocked = True
            if self._last_applied_at is not None and (
                (now - self._last_applied_at).total_seconds() < self._min_interval * 60.0
            ):
                reasons.append(REASON_THROTTLED)
                blocked = True
            should_apply = not blocked

        return CompensationResult(
            suggested=suggested,
            outdoor=outdoor,
            quality=FEEDBACK_VALID,
            reason_codes=tuple(reasons),
            should_apply=should_apply,
        )

    def note_applied(self, value: float, now: datetime) -> None:
        """Record that ``value`` was actually written to the heat source."""
        self._last_applied = float(value)
        self._last_applied_at = now

    @property
    def last_applied(self) -> float | None:
        """Last value written downstream, or ``None`` when never written."""
        return self._last_applied

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable state (round-trips through :meth:`restore`)."""
        return {
            "last_applied": self._last_applied,
            "last_applied_at": _dump_dt(self._last_applied_at),
            "last_suggested": self._last_suggested,
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore a snapshot; malformed input falls back to a clean state."""
        if not data:
            return
        self._last_applied = _opt_float(data.get("last_applied"))
        self._last_applied_at = _opt_dt(data.get("last_applied_at"))
        self._last_suggested = _opt_float(data.get("last_suggested"))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve(self, reading: WeatherReading, now: datetime) -> tuple[str, float | None]:
        """Return ``(quality, value)`` - value is ``None`` unless usable."""
        if reading.quality == FEEDBACK_UNAVAILABLE or reading.value is None:
            return FEEDBACK_UNAVAILABLE, None
        try:
            value = float(reading.value)
        except (TypeError, ValueError):
            return FEEDBACK_UNAVAILABLE, None
        if not math.isfinite(value):
            return FEEDBACK_UNAVAILABLE, None
        if reading.quality != FEEDBACK_VALID:
            # stale / uncertain / unknown quality: keep the last confirmed value
            return FEEDBACK_STALE, None
        if reading.updated_at is None:
            # freshness cannot be proven -> fail safe, never guess
            return FEEDBACK_STALE, None
        age = (now - reading.updated_at).total_seconds()
        if age > self._stale_after * 60.0:
            return FEEDBACK_STALE, None
        return FEEDBACK_VALID, value

    def _raw_supply(self, outdoor: float, night: bool) -> float:
        """Unclamped curve value, mirrors :meth:`WeatherCurve.supply_for`."""
        curve = self._curve
        span = curve.outdoor_stop - curve.outdoor_design
        slope = 0.0 if span == 0 else (curve.supply_eco - curve.supply_design) / span
        raw = curve.supply_design + slope * (outdoor - curve.outdoor_design)
        if night:
            raw += curve.night_offset
        return raw


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
