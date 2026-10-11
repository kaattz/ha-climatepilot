"""Heat source demand aggregation and scheduling FSM (design doc 6).

Pure engine: **no Home Assistant imports** (docs/INTERFACES.md section 0.1).
Every timestamp arrives through ``HeatSourceInput.now``; the module never reads
the wall clock.

Safety contract
---------------

1. **Semantics gate.** ``climate.set_hvac_mode`` may only be used once the user
   has attested that ``heat``/``off`` affect space heating alone and that
   ``set_temperature`` really targets the supply temperature (design doc 6.3,
   14). Without ``BoilerCapabilities.semantics_verified`` the FSM refuses to
   emit ``heat``/``off`` and reports ``FSM_OBSERVE`` +
   :data:`REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED`. No capability object at all
   is treated the same way - unverified is unverified.
2. **All actuators closed wins over every timer.** As soon as demand is gone
   *and* no zone is unknown, the heat request is revoked. ``min_on_time`` is
   explicitly bypassed on that path - software must never force the burner to
   keep firing once every actuator is closed (design doc 6.2, last line).
   ``min_on_time`` may only delay a revoke while zone states are *unknown*, and
   even then only until it elapses, after which the FSM degrades to manual
   takeover instead of guessing (design doc 6.1, 11).
3. **Unknown is not "no demand".** ``unknown_zones > 0`` never counts as demand
   and never authorises a new heat request, but it also never authorises
   treating the zone as satisfied.
4. **The FSM only turns off what it turned on.** ``_heat_requested`` tracks our
   own request; a boiler already sitting in ``heat`` before the integration
   started is left alone (user intent first, design doc 4.2/P1).
5. **DEGRADED stops new writes.** Boiler unavailable or faulted zones produce
   ``DECISION_DEGRADED`` with no target mode. The single exception is a faulted
   zone while our own heat request is still outstanding: revoking that request
   is always safe, so it is attempted.

Reason codes on :class:`HeatSourceDecision` are always ``const`` ``REASON_*``
values.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import (
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
    DECISION_DEGRADED,
    DECISION_NONE,
    DECISION_REQUEST_HEAT,
    DECISION_REVOKE_HEAT,
    DECISION_SET_SUPPLY,
    DEFAULT_DEMAND_OFF_DEBOUNCE,
    DEFAULT_DEMAND_ON_DEBOUNCE,
    DEFAULT_MIN_OFF_TIME,
    DEFAULT_MIN_ON_TIME,
    DEFAULT_PUMP_OVERRUN,
    DEFAULT_VALVE_PREOPEN_WAIT,
    FSM_DEGRADED,
    FSM_HEATING,
    FSM_IDLE,
    FSM_OBSERVE,
    FSM_PREPARE,
    FSM_SATISFIED,
    FSM_STATES,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    REASON_BOILER_MIN_FLOW_UNVERIFIED,
    REASON_CONTROL_PAUSED,
    REASON_DEGRADED,
    REASON_DEMAND_DEBOUNCE,
    REASON_ENTITY_UNAVAILABLE,
    REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED,
    REASON_MIN_OFF_TIME,
    REASON_MIN_ON_TIME,
    REASON_OBSERVE_MODE,
    REASON_PUMP_OVERRUN,
    REASON_RANGE_CLAMP,
    REASON_VALVE_PREOPEN,
)


@dataclass(frozen=True, slots=True)
class BoilerCapabilities:
    """What the boiler ``climate`` entity can actually do (design doc 6.3)."""

    entity_id: str
    min_temp: float | None
    max_temp: float | None
    target_step: float
    hvac_modes: tuple[str, ...]
    semantics_verified: bool
    min_flow_verified: bool


@dataclass(frozen=True, slots=True)
class HeatSourceConfig:
    """Independent FSM parameters (design doc 6.2)."""

    demand_on_debounce: float = DEFAULT_DEMAND_ON_DEBOUNCE
    demand_off_debounce: float = DEFAULT_DEMAND_OFF_DEBOUNCE
    valve_preopen_wait: float = DEFAULT_VALVE_PREOPEN_WAIT
    min_on_time: float = DEFAULT_MIN_ON_TIME
    min_off_time: float = DEFAULT_MIN_OFF_TIME
    pump_overrun: float = DEFAULT_PUMP_OVERRUN

    def to_dict(self) -> dict[str, float]:
        """Serialise for the options flow and the diagnostics export."""
        return {
            "demand_on_debounce": self.demand_on_debounce,
            "demand_off_debounce": self.demand_off_debounce,
            "valve_preopen_wait": self.valve_preopen_wait,
            "min_on_time": self.min_on_time,
            "min_off_time": self.min_off_time,
            "pump_overrun": self.pump_overrun,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> HeatSourceConfig:
        """Build from stored/optional data, ignoring unknown keys.

        Tolerant on purpose: this runs while loading options and persisted
        state, so a stale or hand-edited mapping must not break setup.
        """
        if not data:
            return cls()
        known: dict[str, float] = {}
        for field in (
            "demand_on_debounce",
            "demand_off_debounce",
            "valve_preopen_wait",
            "min_on_time",
            "min_off_time",
            "pump_overrun",
        ):
            value = data.get(field)
            if value is None:
                continue
            try:
                known[field] = float(value)
            except (TypeError, ValueError):
                continue
        return cls(**known)


@dataclass(frozen=True, slots=True)
class HeatSourceInput:
    """One evaluation input snapshot."""

    now: datetime
    active_zones: int
    unknown_zones: int
    control_mode: str
    boiler_mode: str | None
    boiler_available: bool
    zones_faulted: bool = False


@dataclass(frozen=True, slots=True)
class HeatSourceDecision:
    """What the coordinator should do with the heat source right now."""

    state: str
    decision: str
    target_mode: str | None
    target_supply: float | None
    wait_seconds: float | None
    reason_codes: tuple[str, ...]


class HeatSourceStateMachine:
    """Debounced demand -> boiler mode/supply scheduler."""

    def __init__(
        self,
        config: HeatSourceConfig | None = None,
        capabilities: BoilerCapabilities | None = None,
    ) -> None:
        self._config = config or HeatSourceConfig()
        self._capabilities = capabilities

        self._state: str = FSM_IDLE
        self._heat_requested = False
        self._demand_on_since: datetime | None = None
        self._demand_off_since: datetime | None = None
        self._prepare_started_at: datetime | None = None
        self._heat_started_at: datetime | None = None
        self._last_off_at: datetime | None = None
        self._satisfied_at: datetime | None = None
        self._last_now: datetime | None = None

    # ------------------------------------------------------------------
    # Public properties (consumed by the coordinator / platform layer)
    # ------------------------------------------------------------------
    @property
    def capabilities(self) -> BoilerCapabilities | None:
        """The boiler capabilities this machine was built with, if any."""
        return self._capabilities

    @property
    def can_write_supply(self) -> bool:
        """True when an explicit supply-temperature write is permitted.

        Requires known capabilities, verified semantics and a non-degraded FSM.
        """
        caps = self._capabilities
        return caps is not None and caps.semantics_verified and self._state != FSM_DEGRADED

    @property
    def state(self) -> str:
        """Current FSM state (``FSM_*``)."""
        return self._state

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate(self, data: HeatSourceInput, *, supply: float | None) -> HeatSourceDecision:
        """Evaluate the heat source for ``data`` (design doc 6.2)."""
        now = data.now
        self._last_now = now

        caps = self._capabilities
        semantics_ok = caps is not None and caps.semantics_verified
        boiler_mode = _norm(data.boiler_mode)
        cfg = self._config

        # --- 1. paused: stop coordinating, but never leave our own request
        #        asserted without an owner (design doc 11).
        if data.control_mode == CONTROL_MODE_PAUSED:
            if self._heat_requested and data.boiler_available and semantics_ok:
                return self._revoke(
                    boiler_mode,
                    now,
                    final_state=FSM_IDLE,
                    extra_reasons=(REASON_CONTROL_PAUSED,),
                )
            self._state = FSM_IDLE
            reasons = [REASON_CONTROL_PAUSED]
            if self._heat_requested and not data.boiler_available:
                # We still believe we requested heat but cannot withdraw it.
                reasons.append(REASON_ENTITY_UNAVAILABLE)
            return _decision(FSM_IDLE, DECISION_NONE, tuple(reasons))

        # --- 2. communication faults stop every new write ------------------
        if not data.boiler_available or data.zones_faulted:
            reason = REASON_ENTITY_UNAVAILABLE if not data.boiler_available else REASON_DEGRADED
            self._state = FSM_DEGRADED
            # A faulted zone may mean every valve is closed: revoking our own
            # request is the one write that is always safe, but only when the
            # boiler is reachable and the mode semantics were verified.
            if (
                data.zones_faulted
                and data.boiler_available
                and semantics_ok
                and self._heat_requested
            ):
                return self._revoke(
                    boiler_mode,
                    now,
                    final_state=FSM_DEGRADED,
                    extra_reasons=(reason, REASON_DEGRADED),
                )
            return _decision(FSM_DEGRADED, DECISION_DEGRADED, (reason, REASON_DEGRADED))

        # --- 3. observe / unknown control mode: never write ----------------
        if data.control_mode != CONTROL_MODE_COORDINATE:
            self._state = FSM_OBSERVE
            if data.control_mode == CONTROL_MODE_OBSERVE:
                return _decision(FSM_OBSERVE, DECISION_NONE, (REASON_OBSERVE_MODE,))
            # Unrecognised mode falls back to the safe read-only behaviour.
            return _decision(FSM_OBSERVE, DECISION_NONE, (REASON_OBSERVE_MODE,))

        # --- 4. semantics gate: no heat/off without user attestation -------
        if not semantics_ok:
            self._state = FSM_OBSERVE
            gates: list[str] = [REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED]
            if caps is None or not caps.min_flow_verified:
                # Both gates are user-facing: they explain why nothing happened.
                gates.append(REASON_BOILER_MIN_FLOW_UNVERIFIED)
            return _decision(FSM_OBSERVE, DECISION_NONE, tuple(gates))

        # --- demand debounce bookkeeping -----------------------------------
        active = data.active_zones > 0
        all_closed = data.active_zones == 0 and data.unknown_zones == 0
        uncertain = data.active_zones == 0 and data.unknown_zones > 0

        if active:
            if self._demand_on_since is None:
                self._demand_on_since = now
            self._demand_off_since = None
        else:
            if self._demand_off_since is None:
                self._demand_off_since = now
            self._demand_on_since = None
            if self._state == FSM_PREPARE:
                # Demand vanished before we ever asked for heat; nothing to
                # revoke, and the pre-open wait must start over next time.
                self._state = FSM_IDLE
                self._prepare_started_at = None

        if active:
            # Already heating: keep the request asserted (mode may have been
            # changed externally) and publish the supply target.
            if self._heat_requested or self._state == FSM_HEATING:
                self._state = FSM_HEATING
                self._heat_requested = True
                if self._heat_started_at is None:
                    self._heat_started_at = now
                return self._heating_decision(boiler_mode, supply)

            if self._demand_on_since is None:
                # Defensive: a state restored without its demand clock restarts
                # the debounce rather than trusting a missing timestamp.
                self._demand_on_since = now
            waited_on = (now - self._demand_on_since).total_seconds()
            if waited_on < cfg.demand_on_debounce:
                self._state = FSM_IDLE
                return _decision(
                    FSM_IDLE,
                    DECISION_NONE,
                    (REASON_DEMAND_DEBOUNCE,),
                    wait_seconds=cfg.demand_on_debounce - waited_on,
                )

            if not caps.hvac_modes or HVAC_MODE_HEAT in caps.hvac_modes:
                pass
            else:
                # The entity reports its modes and heat is not among them:
                # starting a burn is not possible, let alone verified.
                self._state = FSM_IDLE
                return _decision(
                    FSM_IDLE,
                    DECISION_NONE,
                    _gate_reasons(caps, REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED),
                )

            if self._last_off_at is not None:
                waited_off = (now - self._last_off_at).total_seconds()
                if waited_off < cfg.min_off_time:
                    self._state = FSM_IDLE
                    return _decision(
                        FSM_IDLE,
                        DECISION_NONE,
                        (REASON_MIN_OFF_TIME,),
                        wait_seconds=cfg.min_off_time - waited_off,
                    )

            if not caps.min_flow_verified:
                # Hydraulics unverified: never start a burn, but always allow
                # the revoke path below.
                self._state = FSM_IDLE
                return _decision(
                    FSM_IDLE,
                    DECISION_NONE,
                    (REASON_BOILER_MIN_FLOW_UNVERIFIED,),
                )

            if self._state != FSM_PREPARE or self._prepare_started_at is None:
                self._state = FSM_PREPARE
                self._prepare_started_at = now
            waited_valve = (now - self._prepare_started_at).total_seconds()
            if waited_valve < cfg.valve_preopen_wait:
                # No position feedback exists, so this wait only coordinates -
                # it cannot prove the valve opened (design doc 6.3).
                return _decision(
                    FSM_PREPARE,
                    DECISION_NONE,
                    (REASON_VALVE_PREOPEN,),
                    wait_seconds=cfg.valve_preopen_wait - waited_valve,
                )

            self._state = FSM_HEATING
            self._heat_requested = True
            self._heat_started_at = now
            self._prepare_started_at = None
            return self._heating_decision(boiler_mode, supply)

        # --- no active demand ---------------------------------------------
        if self._demand_off_since is None:
            self._demand_off_since = now
        waited = (now - self._demand_off_since).total_seconds()
        if waited < cfg.demand_off_debounce:
            if self._heat_requested:
                # Hold through the debounce window: this is the anti-flicker
                # mechanism, not a software minimum runtime.
                self._state = FSM_HEATING
                return self._heating_decision(
                    boiler_mode,
                    supply,
                    extra_reasons=(REASON_DEMAND_DEBOUNCE,),
                )
            self._state = FSM_IDLE
            return _decision(
                FSM_IDLE,
                DECISION_NONE,
                (REASON_DEMAND_DEBOUNCE,),
                wait_seconds=cfg.demand_off_debounce - waited,
            )

        if self._heat_requested:
            if all_closed:
                # HARD SAFETY (design doc 6.2): every actuator is known closed,
                # so the request is revoked now. min_on_time must never be used
                # to force the burner to keep heating.
                return self._revoke(boiler_mode, now)
            # unknown_zones > 0: unknown is not "no demand", so we do not
            # revoke on unproven data - but we do not fire forever either.
            if not self._min_on_elapsed(now):
                remaining = self._config.min_on_time - self._elapsed_since(
                    self._heat_started_at, now
                )
                self._state = FSM_HEATING
                return self._heating_decision(
                    boiler_mode,
                    supply,
                    extra_reasons=(REASON_MIN_ON_TIME, REASON_ENTITY_UNAVAILABLE),
                    wait_seconds=max(0.0, remaining),
                )
            # Still unknown after the grace window: hand over to a human rather
            # than guess whether the valves are open (design doc 6.1, 11).
            self._state = FSM_DEGRADED
            return _decision(
                FSM_DEGRADED,
                DECISION_DEGRADED,
                (REASON_ENTITY_UNAVAILABLE, REASON_DEGRADED),
            )

        # Nothing of ours is outstanding: settlement / overrun bookkeeping.
        if uncertain:
            # Never start a burn on unproven demand.
            self._state = FSM_IDLE
            return _decision(
                FSM_IDLE,
                DECISION_NONE,
                (REASON_ENTITY_UNAVAILABLE,),
            )

        if self._state == FSM_SATISFIED and self._satisfied_at is not None:
            remaining = cfg.pump_overrun - self._elapsed_since(self._satisfied_at, now)
            if remaining > 0:
                # Post-circulation is coordinated here but hydraulically
                # guaranteed by the hardware (design doc 6.3, 11).
                return _decision(
                    FSM_SATISFIED,
                    DECISION_NONE,
                    (REASON_PUMP_OVERRUN,),
                    wait_seconds=remaining,
                )
        self._state = FSM_IDLE
        self._satisfied_at = None
        return _decision(FSM_IDLE, DECISION_NONE, ())

    def note_demand_change(self, now: datetime, has_demand: bool) -> None:
        """Seed the debounce clocks from an explicit demand event."""
        self._last_now = now
        if has_demand:
            if self._demand_on_since is None:
                self._demand_on_since = now
            self._demand_off_since = None
        else:
            if self._demand_off_since is None:
                self._demand_off_since = now
            self._demand_on_since = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return the persistable FSM state."""
        return {
            "state": self._state,
            "heat_requested": self._heat_requested,
            "demand_on_since": _dump_dt(self._demand_on_since),
            "demand_off_since": _dump_dt(self._demand_off_since),
            "prepare_started_at": _dump_dt(self._prepare_started_at),
            "heat_started_at": _dump_dt(self._heat_started_at),
            "last_off_at": _dump_dt(self._last_off_at),
            "satisfied_at": _dump_dt(self._satisfied_at),
        }

    def restore(self, data: dict[str, Any]) -> None:
        """Restore a :meth:`snapshot`; inconsistent payloads fall back safely."""
        if not data:
            return
        state = data.get("state")
        self._state = state if state in FSM_STATES else FSM_IDLE
        self._heat_requested = bool(data.get("heat_requested", False))
        self._demand_on_since = _opt_dt(data.get("demand_on_since"))
        self._demand_off_since = _opt_dt(data.get("demand_off_since"))
        self._prepare_started_at = _opt_dt(data.get("prepare_started_at"))
        self._heat_started_at = _opt_dt(data.get("heat_started_at"))
        self._last_off_at = _opt_dt(data.get("last_off_at"))
        self._satisfied_at = _opt_dt(data.get("satisfied_at"))

        # Never resume a burn bookkeeping that has no start time: the FSM would
        # believe it is heating forever.
        if self._state == FSM_HEATING and self._heat_started_at is None:
            self._state = FSM_IDLE
            self._heat_requested = False
        if self._state == FSM_PREPARE and self._prepare_started_at is None:
            self._state = FSM_IDLE
        if self._state == FSM_SATISFIED and self._satisfied_at is None:
            self._state = FSM_IDLE

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _heating_decision(
        self,
        boiler_mode: str | None,
        supply: float | None,
        *,
        extra_reasons: tuple[str, ...] = (),
        wait_seconds: float | None = None,
    ) -> HeatSourceDecision:
        target_supply, clamp_reasons = self._clamp_supply(supply)
        reasons = (*extra_reasons, *clamp_reasons)
        self._heat_requested = True
        if self._heat_started_at is None:
            self._heat_started_at = self._last_now

        if boiler_mode != HVAC_MODE_HEAT:
            return _decision(
                FSM_HEATING,
                DECISION_REQUEST_HEAT,
                reasons,
                target_mode=HVAC_MODE_HEAT,
                target_supply=target_supply,
                wait_seconds=wait_seconds,
            )
        if target_supply is not None:
            return _decision(
                FSM_HEATING,
                DECISION_SET_SUPPLY,
                reasons,
                target_supply=target_supply,
                wait_seconds=wait_seconds,
            )
        return _decision(FSM_HEATING, DECISION_NONE, reasons, wait_seconds=wait_seconds)

    def _revoke(
        self,
        boiler_mode: str | None,
        now: datetime,
        *,
        final_state: str = FSM_SATISFIED,
        extra_reasons: tuple[str, ...] = (),
    ) -> HeatSourceDecision:
        """Drop our own heat request; only write ``off`` if we asked for heat."""
        self._heat_requested = False
        self._heat_started_at = None
        self._prepare_started_at = None
        self._last_off_at = now
        self._satisfied_at = now
        self._state = final_state
        if boiler_mode == HVAC_MODE_HEAT:
            return _decision(
                final_state,
                DECISION_REVOKE_HEAT,
                extra_reasons,
                target_mode=HVAC_MODE_OFF,
            )
        # Already off: no write needed, just settle.
        return _decision(final_state, DECISION_NONE, extra_reasons)

    def _clamp_supply(self, supply: float | None) -> tuple[float | None, tuple[str, ...]]:
        """Clamp/snap a supply target onto the boiler's declared range."""
        if supply is None:
            return None, ()
        try:
            value = float(supply)
        except (TypeError, ValueError):
            return None, ()
        if not math.isfinite(value):
            return None, ()

        caps = self._capabilities
        if caps is None:
            return None, ()

        reasons: list[str] = []
        if caps.min_temp is not None and value < caps.min_temp:
            value = caps.min_temp
            reasons.append(REASON_RANGE_CLAMP)
        if caps.max_temp is not None and value > caps.max_temp:
            value = caps.max_temp
            reasons.append(REASON_RANGE_CLAMP)

        step = caps.target_step or 0.0
        if step > 0:
            value = round(value / step) * step
            # Re-clamp: snapping must not escape the device range.
            if caps.min_temp is not None:
                value = max(caps.min_temp, value)
            if caps.max_temp is not None:
                value = min(caps.max_temp, value)
            value = round(value, 3)

        return value, tuple(reasons)

    def _elapsed_since(self, start: datetime | None, now: datetime) -> float:
        if start is None:
            return math.inf
        return (now - start).total_seconds()

    def _min_on_elapsed(self, now: datetime) -> bool:
        return self._elapsed_since(self._heat_started_at, now) >= self._config.min_on_time


def _decision(
    state: str,
    decision: str,
    reason_codes: tuple[str, ...],
    *,
    target_mode: str | None = None,
    target_supply: float | None = None,
    wait_seconds: float | None = None,
) -> HeatSourceDecision:
    return HeatSourceDecision(
        state=state,
        decision=decision,
        target_mode=target_mode,
        target_supply=target_supply,
        wait_seconds=wait_seconds,
        reason_codes=reason_codes,
    )


def _norm(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower()


def _gate_reasons(caps: BoilerCapabilities | None, primary: str) -> tuple[str, ...]:
    """Return the user-facing gate reasons, always including the primary one."""
    reasons: list[str] = []
    if primary not in reasons:
        reasons.append(primary)
    if (
        caps is None or not caps.min_flow_verified
    ) and REASON_BOILER_MIN_FLOW_UNVERIFIED not in reasons:
        reasons.append(REASON_BOILER_MIN_FLOW_UNVERIFIED)
    return tuple(reasons)


def _dump_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _opt_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
