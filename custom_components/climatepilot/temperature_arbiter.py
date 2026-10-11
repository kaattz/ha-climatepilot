"""Temperature arbitration for ClimatePilot (design doc section 4).

The arbiter produces exactly **one** effective setpoint per zone. This is the
heart of the integration: it is the only place allowed to decide what
temperature a room should hold, and it is pure Python so the priority rules can
be tested exhaustively without Home Assistant.

Priority (design doc 4.2)
-------------------------
====  ==================================  ====================================
P0    hardware safety / configured range   clamp the value that may be sent
P1    KNX panel or HA manual override      freeze automatic targets
P2    explicit user-selected fixed mode    comfort / eco / away
P3    per-room automatic schedule          time-of-day base target
P4    automatic optimisation strategies    only applied to P2/P3 base targets
====  ==================================  ====================================

A manual override never receives automatic adjustments (design doc 4.1), and an
override is never extended by later automatic activity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import zip_longest

from .const import (
    DEFAULT_OVERRIDE_MINUTES,
    OVERRIDE_FIXED_DURATION,
    OVERRIDE_INDEFINITE,
    OVERRIDE_NEXT_SCHEDULE,
    OVERRIDE_POLICIES,
    REASON_COLD_ADJUSTMENT,
    REASON_MANUAL_OVERRIDE,
    REASON_RANGE_CLAMP,
    SOURCE_COLD_ADJUSTMENT,
    SOURCE_FALLBACK,
    SOURCE_FIXED_MODE,
    SOURCE_MANUAL,
    SOURCE_SCHEDULE,
    ZONE_MODE_AWAY,
    ZONE_MODE_COMFORT,
    ZONE_MODE_ECO,
    ZONE_MODE_MANUAL,
)
from .models import ManualOverride, Schedule, SetpointDecision, ZoneState

#: Fixed-mode targets are deliberately *relative* defaults; the options flow
#: stores the real numbers per zone. These keep ``arbitrate`` total when a zone
#: was created before the user configured its fixed modes.
FIXED_MODE_FALLBACK: dict[str, float] = {
    ZONE_MODE_COMFORT: 22.0,
    ZONE_MODE_ECO: 19.0,
    ZONE_MODE_AWAY: 16.0,
}


@dataclass(frozen=True, slots=True)
class ArbiterConfig:
    """Tunables for :class:`TemperatureArbiter`."""

    #: Master switch - a cold snap may raise the *water* temperature without
    #: touching room targets (design doc 5.2). Off by default on purpose.
    cold_adjustment_enabled_by_default: bool = False
    cold_adjustment_delta: float = 1.0
    #: Zone-independent ceiling applied after every other rule.
    default_override_minutes: int = DEFAULT_OVERRIDE_MINUTES


def _clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    """Clamp ``value`` into ``[low, high]``, reporting whether it moved."""
    if high < low:  # degenerate config - prefer the lower bound
        return low, value != low
    if value < low:
        return low, True
    if value > high:
        return high, True
    return value, False


class TemperatureArbiter:
    """Resolve the single effective setpoint for a zone."""

    def __init__(self, config: ArbiterConfig | None = None) -> None:
        self.config = config or ArbiterConfig()

    # ------------------------------------------------------------------
    # Core arbitration
    # ------------------------------------------------------------------
    def arbitrate(
        self,
        zone: ZoneState,
        now: datetime,
        *,
        schedule: Schedule | None = None,
        cold_adjustment: float = 0.0,
        aux_delta: float = 0.0,
    ) -> SetpointDecision:
        """Return the arbitrated setpoint for ``zone`` at ``now``.

        The returned decision is already snapped to the device step and clamped
        into the zone's configured range, so callers may send it as-is.
        """
        reasons: list[str] = []

        # --- P1: manual override freezes everything automatic -------------
        override = zone.manual_override
        if override is not None and override.is_active(now):
            reasons.append(REASON_MANUAL_OVERRIDE)
            return self._finalize(
                zone,
                value=override.value,
                base=override.value,
                source=SOURCE_MANUAL,
                reasons=reasons,
                adjustment_delta=0.0,
            )

        # --- P2 / P3: base target ----------------------------------------
        base, source, base_reasons = self._base_target(zone, schedule, now)
        reasons.extend(base_reasons)

        # --- P4: automatic adjustments (never on a manual override) -------
        delta = 0.0
        if cold_adjustment:
            delta += cold_adjustment
            reasons.append(REASON_COLD_ADJUSTMENT)
            source = SOURCE_COLD_ADJUSTMENT
        if aux_delta:
            delta += aux_delta

        value = base + delta
        return self._finalize(
            zone,
            value=value,
            base=base,
            source=source,
            reasons=reasons,
            adjustment_delta=delta,
        )

    # ------------------------------------------------------------------
    # Manual override lifecycle
    # ------------------------------------------------------------------
    def note_external_change(
        self,
        zone: ZoneState,
        value: float,
        now: datetime,
        *,
        policy: str = OVERRIDE_NEXT_SCHEDULE,
        duration: timedelta | None = None,
        source_entity: str | None = None,
    ) -> ManualOverride:
        """Record a user-driven setpoint as a manual override.

        ``policy`` is one of :data:`OVERRIDE_POLICIES`. ``fixed_duration``
        stores an absolute UTC expiry so a restart can recompute the remaining
        time instead of extending it (design doc 4.3, 9.1).
        """
        if policy not in OVERRIDE_POLICIES:
            policy = OVERRIDE_NEXT_SCHEDULE

        until: datetime | None = None
        if policy == OVERRIDE_FIXED_DURATION:
            window = duration or timedelta(minutes=self.config.default_override_minutes)
            until = now + window
        elif policy == OVERRIDE_INDEFINITE:
            until = None
        # next_schedule resolves its expiry at the next schedule transition;
        # ``until`` stays None so no countdown is invented.

        override = ManualOverride(
            value=value,
            created_at=now,
            policy=policy,
            until=until,
            source_entity=source_entity,
        )
        zone.manual_override = override
        zone.mode = ZONE_MODE_MANUAL
        zone.last_external_change = now
        return override

    def note_schedule_transition(self, zone: ZoneState, now: datetime) -> bool:
        """Release a ``next_schedule`` override at a schedule switch point.

        Returns True when the override was cleared. ``fixed_duration`` and
        ``indefinite`` overrides survive schedule transitions on purpose.
        """
        override = zone.manual_override
        if override is None:
            return False
        if override.policy != OVERRIDE_NEXT_SCHEDULE:
            return False
        if not override.is_active(now):
            return False

        zone.manual_override = None
        if zone.mode == ZONE_MODE_MANUAL:
            zone.mode = "auto"
        return True

    def resume_auto(self, zone: ZoneState, now: datetime) -> None:
        """Clear any override immediately ("恢复自动" / resume automatic)."""
        if zone.manual_override is not None:
            zone.manual_override = None
        if zone.mode == ZONE_MODE_MANUAL:
            zone.mode = "auto"
        zone.last_external_change = now

    def expire_overrides(self, zones: Iterable[ZoneState], now: datetime) -> list[str]:
        """Drop expired fixed-duration overrides. Returns the affected zone ids."""
        released: list[str] = []
        for zone in zones:
            override = zone.manual_override
            if override is None:
                continue
            if override.policy != OVERRIDE_FIXED_DURATION:
                continue
            if override.is_active(now):
                continue
            zone.manual_override = None
            if zone.mode == ZONE_MODE_MANUAL:
                zone.mode = "auto"
            released.append(zone.zone_id)
        return released

    # ------------------------------------------------------------------
    # Auxiliary load bookkeeping
    # ------------------------------------------------------------------
    def aux_load_delta(
        self,
        zone: ZoneState,
        *,
        now: datetime | None = None,
        allowed: bool,
    ) -> float:
        """Return the bounded auxiliary-load delta for ``zone``.

        Guard rails from design doc 5.3: never while a manual override is
        active, never above the zone's configured cap, and never an unbounded
        "current room temperature + 2 °C" chase.
        """
        if not allowed or not zone.aux_load_enabled:
            return 0.0
        if now is not None:
            override = zone.manual_override
            if override is not None and override.is_active(now):
                return 0.0
        cap = zone.aux_load_cap
        if (
            cap is not None
            and zone.current_temperature is not None
            and zone.current_temperature >= cap
        ):
            return 0.0
        return float(zone.auto_adjustments.get("aux_load", 0.0))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _base_target(
        self,
        zone: ZoneState,
        schedule: Schedule | None,
        now: datetime,
    ) -> tuple[float, str, list[str]]:
        """Resolve P2 (fixed mode) / P3 (schedule) base target."""
        if zone.mode in FIXED_MODE_FALLBACK:
            fallback = FIXED_MODE_FALLBACK[zone.mode]
            if schedule is None or not schedule.slots:
                return zone.base_setpoint or fallback, SOURCE_FIXED_MODE, []
            # A fixed mode outranks the schedule, but keeps the schedule's
            # shape: use the fixed mode's own base target.
            return zone.base_setpoint or fallback, SOURCE_FIXED_MODE, []

        slot = schedule.active_slot(now) if schedule is not None else None
        if slot is not None:
            return slot.setpoint, SOURCE_SCHEDULE, []

        # No schedule configured: fall back to the zone's stored base target
        # rather than inventing a value.
        if zone.base_setpoint:
            return zone.base_setpoint, SOURCE_FALLBACK, []
        return FIXED_MODE_FALLBACK[ZONE_MODE_COMFORT], SOURCE_FALLBACK, []

    def _finalize(
        self,
        zone: ZoneState,
        *,
        value: float,
        base: float,
        source: str,
        reasons: list[str],
        adjustment_delta: float,
    ) -> SetpointDecision:
        """Clamp, snap to the device step and build the decision."""
        clamped_value, was_clamped = _clamp(value, zone.min_temp, zone.max_temp)
        if was_clamped:
            reasons.append(REASON_RANGE_CLAMP)
        snapped = zone.round_target(clamped_value)
        if snapped != clamped_value and REASON_RANGE_CLAMP not in reasons:
            # Snapping onto the device step is a (small) range adjustment too.
            reasons.append(REASON_RANGE_CLAMP)
        return SetpointDecision(
            zone_id=zone.zone_id,
            effective=snapped,
            base=zone.round_target(base),
            source=source,
            reason_codes=tuple(dict.fromkeys(reasons)),
            adjustment_delta=round(adjustment_delta, 3),
            clamped=was_clamped,
        )


# ---------------------------------------------------------------------------
# Helper used by the coordinator to detect schedule switch points
# ---------------------------------------------------------------------------


def active_slot_key(schedule: Schedule, now: datetime) -> tuple[time, float] | None:
    """Return a comparable key identifying the slot in effect at ``now``.

    The coordinator stores the previous key and calls
    :meth:`TemperatureArbiter.note_schedule_transition` whenever it changes, so
    a ``next_schedule`` override is released exactly at a real switch point.
    """
    slot = schedule.active_slot(now)
    if slot is None:
        return None
    return (slot.at, slot.setpoint)


def pairwise(iterable: Iterable[tuple[time, float]]) -> Iterable[tuple[object, object]]:
    """``s -> (s0,s1), (s1,s2), ...`` - small local helper, avoids an import."""
    first, second = iter(iterable), iter(iterable)
    next(second, None)
    return zip_longest(first, second)
