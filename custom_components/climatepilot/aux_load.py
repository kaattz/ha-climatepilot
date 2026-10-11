"""Bounded auxiliary heat load (design doc 5.3).

The legacy blueprint raised the living-room target to ``current room
temperature + 2 °C`` whenever a bedroom called for heat. That chases its own
tail: the setpoint climbs as the room warms, so the room never reaches it.

This module replaces it with a **bounded, hysteretic, reversible** strategy:

* only active while *other* zones genuinely demand heat;
* only raises the target by a small configured delta above the room's own
  schedule target - never relative to the measured room temperature;
* hard absolute ceiling, night block, manual-override block;
* minimum interval between switch events, and an explicit return to the normal
  target when it exits.

The auxiliary load is a comfort/hydraulics aid only. It is **not** a minimum
flow guarantee - the design doc is explicit that hydraulics must be solved in
hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import (
    REASON_AUX_LOAD,
    REASON_AUX_LOAD_EXIT,
    REASON_MANUAL_OVERRIDE,
    REASON_THROTTLED,
)
from .models import AuxLoadConfig, AuxLoadState, ManualOverride, ZoneState


@dataclass(frozen=True, slots=True)
class AuxLoadDecision:
    """Outcome of one auxiliary-load evaluation."""

    active: bool
    delta: float
    target: float
    reason_codes: tuple[str, ...] = ()


class AuxLoadController:
    """Decide whether a zone should act as the auxiliary heat load."""

    def __init__(self, config: AuxLoadConfig | None = None) -> None:
        self.config = config or AuxLoadConfig()

    def evaluate(
        self,
        state: AuxLoadState,
        zone: ZoneState,
        now: datetime,
        *,
        other_demand: int,
        base_target: float,
        override: ManualOverride | None = None,
        in_use: bool = False,
    ) -> AuxLoadDecision:
        """Evaluate the auxiliary load for ``zone``.

        ``other_demand`` is the number of *other* zones with a genuine heat
        demand. ``base_target`` is the zone's own arbitrated target before any
        auxiliary delta is applied. ``in_use`` is the caller's arbitration of
        *which* eligible zone currently holds the auxiliary slot (first eligible
        wins), so at most one zone is ever boosted.
        """
        cfg = self.config
        key = zone.zone_id
        ceiling = min(cfg.max_temperature, zone.max_temp)
        current = zone.current_temperature

        if not cfg.enabled or not zone.aux_load_enabled:
            return self._decide(state, False, 0.0, base_target, ())

        override = override if override is not None else zone.manual_override
        reasons: list[str] = []

        # --- Blocking conditions ------------------------------------------
        override_active = override is not None and override.is_active(now)
        night = cfg.night_block and cfg.is_night(now.timetz().replace(tzinfo=None))
        # The normal target must already be below the ceiling, otherwise the
        # "boost" would immediately be clamped away and we would only be
        # creating write churn.
        headroom = base_target < ceiling

        blocked = other_demand <= 0 or override_active or night or not in_use or not headroom
        if other_demand <= 0:
            reasons.append(REASON_AUX_LOAD_EXIT)
        if override_active:
            reasons.append(REASON_MANUAL_OVERRIDE)
        if night:
            reasons.append(REASON_AUX_LOAD_EXIT)
        if not headroom:
            reasons.append(REASON_AUX_LOAD_EXIT)

        was_active = state.active and state.zone_switch_at.get(key) is not None

        # --- Should it be active? -----------------------------------------
        want = not blocked

        if want:
            # Hysteresis: once active, stay active until the room reaches the
            # ceiling; when inactive, only start with real headroom left.
            if was_active:
                if current is not None and current >= ceiling:
                    want = False
                    reasons.append(REASON_AUX_LOAD_EXIT)
            elif current is not None and current >= ceiling - cfg.hysteresis:
                want = False
                reasons.append(REASON_AUX_LOAD_EXIT)

        if want and not was_active:
            # Entering: engage immediately, but record the switch time.
            state.zone_switch_at[key] = now
            state.active = True
        elif not want and was_active:
            if state.can_switch(now, cfg.min_switch_interval, key):
                state.zone_switch_at.pop(key, None)
                state.active = any(v is not None for v in state.zone_switch_at.values())
            else:
                # Minimum switch interval not elapsed: keep the current state.
                want = True
                reasons = [*reasons, REASON_THROTTLED]

        if want:
            reasons.append(REASON_AUX_LOAD)

        delta = self._active_delta(cfg, base_target, ceiling) if want else 0.0
        return self._decide(state, want, delta, base_target, tuple(reasons))

    # ------------------------------------------------------------------
    @staticmethod
    def _active_delta(cfg: AuxLoadConfig, base_target: float, ceiling: float) -> float:
        """Delta while active - always recomputed from the *base* target.

        Critically, this never references the measured room temperature: the
        old blueprint's ``current_temperature + 2 °C`` chase is exactly what
        this replaces (design doc 5.3).
        """
        return max(0.0, min(base_target + cfg.delta, ceiling) - base_target)

    @staticmethod
    def _decide(
        state: AuxLoadState,
        active: bool,
        delta: float,
        base_target: float,
        reasons: tuple[str, ...],
    ) -> AuxLoadDecision:
        return AuxLoadDecision(
            active=active,
            delta=round(delta, 3) if active else 0.0,
            target=round(base_target + (delta if active else 0.0), 3),
            reason_codes=tuple(dict.fromkeys(reasons)),
        )

    # ------------------------------------------------------------------
    def should_shed_for_demand_end(
        self,
        state: AuxLoadState,
        now: datetime,
        *,
        other_demand: int,
    ) -> bool:
        """True when the auxiliary load must stop because demand ended.

        The design doc requires an explicit exit rather than leaving a daytime
        companion target in place overnight (5.3, 13.8).
        """
        return state.active and other_demand <= 0

    def restore(self, state: AuxLoadState, data: dict[str, object] | None, now: datetime) -> None:
        """Restore persisted state, refusing to resume a stale activation.

        The design doc calls out the night-time failure mode explicitly: after a
        restart the integration must not silently keep a daytime companion
        target alive (5.3, 13.8).
        """
        restored = AuxLoadState.from_dict(data)
        stale = [
            key for key, at in restored.zone_switch_at.items() if (now - at) > timedelta(hours=6)
        ]
        for key in stale:
            restored.zone_switch_at.pop(key, None)
        if not restored.zone_switch_at:
            restored.active = False
        state.active = restored.active
        state.last_switch = restored.last_switch
        state.zone_switch_at = restored.zone_switch_at
