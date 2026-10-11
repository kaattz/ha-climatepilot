"""Pure-python data models for ClimatePilot.

This module must stay **free of Home Assistant imports** so the arbitration and
coordination engines can be unit tested without booting Home Assistant. Only
``dataclasses``, ``datetime`` and typing helpers are allowed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class ScheduleSlot:
    """One daily setpoint slot.

    ``at`` is a local wall-clock time. ``days`` holds weekday numbers as used by
    :meth:`datetime.datetime.weekday` (Monday=0). An empty tuple means "every
    day". ``setpoint`` is the base target in degrees Celsius.
    """

    at: time
    setpoint: float
    days: tuple[int, ...] = ()

    def applies_to(self, weekday: int) -> bool:
        """Return True when this slot is active on ``weekday``."""
        return not self.days or weekday in self.days

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.strftime("%H:%M:%S"),
            "setpoint": self.setpoint,
            "days": list(self.days),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScheduleSlot:
        raw = str(data["at"])
        parts = [int(p) for p in raw.split(":")]
        while len(parts) < 3:
            parts.append(0)
        return cls(
            at=time(parts[0], parts[1], parts[2]),
            setpoint=float(data["setpoint"]),
            days=tuple(int(d) for d in data.get("days", ())),
        )


DEFAULT_SLOTS: tuple[ScheduleSlot, ...] = (
    # Mirrors the legacy blueprint's four phases, now per room.
    ScheduleSlot(at=time(6, 0), setpoint=22.0),
    ScheduleSlot(at=time(9, 0), setpoint=19.0),
    ScheduleSlot(at=time(17, 0), setpoint=22.0),
    ScheduleSlot(at=time(23, 0), setpoint=20.0),
)


@dataclass(frozen=True, slots=True)
class Schedule:
    """An ordered set of daily slots for one zone."""

    slots: tuple[ScheduleSlot, ...] = DEFAULT_SLOTS

    def sorted_slots(self) -> tuple[ScheduleSlot, ...]:
        return tuple(sorted(self.slots, key=lambda s: s.at))

    def active_slot(self, now: datetime) -> ScheduleSlot | None:
        """Return the slot in effect at ``now`` (local wall clock).

        Handles the cross-midnight case for day-restricted slots: at 02:00 on a
        Tuesday with only Monday slots configured, Monday's 22:00 slot is still
        the target that governs the room.
        """
        ordered = self.sorted_slots()

        # Today's slots that have already started.
        started = [
            slot for slot in ordered if slot.applies_to(now.weekday()) and slot.at <= now.time()
        ]
        if started:
            return started[-1]

        # Nothing has started today yet (or today carries no slot at all): walk
        # backwards to the most recent applicable day and take its last slot.
        for back in range(1, 8):
            weekday = (now.weekday() - back) % 7
            previous = [slot for slot in ordered if slot.applies_to(weekday)]
            if previous:
                return previous[-1]
        return None

    def next_transition(self, now: datetime) -> datetime | None:
        """Return the next local datetime at which the active target changes.

        The result is always strictly after ``now``. Returns ``None`` when the
        schedule carries no slot that applies within the coming week.
        """
        if not self.slots:
            return None

        for day_offset in range(8):
            day = (now + timedelta(days=day_offset)).date()
            for slot in self.sorted_slots():
                if not slot.applies_to(day.weekday()):
                    continue
                candidate = datetime.combine(day, slot.at, tzinfo=now.tzinfo)
                if candidate > now:
                    return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"slots": [s.to_dict() for s in self.sorted_slots()]}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Schedule:
        if not data or not data.get("slots"):
            return cls()
        return cls(slots=tuple(ScheduleSlot.from_dict(s) for s in data["slots"]))


# ---------------------------------------------------------------------------
# Weather compensation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeatherCurve:
    """Linear outdoor-temperature -> supply-temperature curve with clamping."""

    outdoor_design: float = 0.0
    outdoor_stop: float = 15.0
    supply_design: float = 45.0
    supply_eco: float = 40.0
    supply_min: float = 35.0
    supply_max: float = 55.0
    night_offset: float = 0.0

    def slope(self) -> float:
        span = self.outdoor_stop - self.outdoor_design
        if span == 0:
            return 0.0
        return (self.supply_eco - self.supply_design) / span

    def supply_for(self, outdoor: float, night: bool = False) -> float:
        """Return the clamped supply setpoint for an outdoor temperature."""
        raw = self.supply_design + self.slope() * (outdoor - self.outdoor_design)
        if night:
            raw += self.night_offset
        return max(self.supply_min, min(self.supply_max, raw))

    def to_dict(self) -> dict[str, float]:
        return {
            "outdoor_design": self.outdoor_design,
            "outdoor_stop": self.outdoor_stop,
            "supply_design": self.supply_design,
            "supply_eco": self.supply_eco,
            "supply_min": self.supply_min,
            "supply_max": self.supply_max,
            "night_offset": self.night_offset,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WeatherCurve:
        if not data:
            return cls()
        known = {f: float(data[f]) for f in cls().to_dict() if f in data}
        return cls(**known)


# ---------------------------------------------------------------------------
# Pending command bookkeeping (design doc 4.4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PendingCommand:
    """A setpoint write awaiting confirmation from the device."""

    request_id: str
    zone_id: str
    entity_id: str
    target: float
    issued_at: datetime
    expires_at: datetime
    tolerance: float
    attempts: int = 0
    confirmed: bool = False
    #: The service call reported a failure. The command stays open because the
    #: device may still have applied it - a late echo must be recognised as ours
    #: rather than mistaken for a manual change - but it no longer blocks a
    #: retry.
    failed: bool = False

    def matches(self, value: float, *, now: datetime | None = None) -> bool:
        """Return True when ``value`` can be the echo of this command."""
        if abs(value - self.target) > self.tolerance:
            return False
        return not (now is not None and now > self.expires_at)

    def is_expired(self, now: datetime) -> bool:
        return now > self.expires_at

    def blocks_new_write(self, now: datetime) -> bool:
        """True while an *unfailed* command is still awaiting confirmation."""
        return not self.failed and not self.is_expired(now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "zone_id": self.zone_id,
            "entity_id": self.entity_id,
            "target": self.target,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "tolerance": self.tolerance,
            "attempts": self.attempts,
            "confirmed": self.confirmed,
            "failed": self.failed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingCommand:
        return cls(
            request_id=data["request_id"],
            zone_id=data["zone_id"],
            entity_id=data["entity_id"],
            target=float(data["target"]),
            issued_at=datetime.fromisoformat(data["issued_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]),
            tolerance=float(data.get("tolerance", 0.2)),
            attempts=int(data.get("attempts", 0)),
            confirmed=bool(data.get("confirmed", False)),
            failed=bool(data.get("failed", False)),
        )


@dataclass(slots=True)
class ConfirmedWrite:
    """The last setpoint successfully written to a zone."""

    target: float
    at: datetime
    request_id: str
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "at": self.at.isoformat(),
            "request_id": self.request_id,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfirmedWrite:
        return cls(
            target=float(data["target"]),
            at=datetime.fromisoformat(data["at"]),
            request_id=data["request_id"],
            confirmed=bool(data.get("confirmed", False)),
        )


@dataclass(slots=True)
class ManualOverride:
    """A user-driven setpoint that outranks every automatic source."""

    value: float
    created_at: datetime
    policy: str = "next_schedule"
    until: datetime | None = None
    source_entity: str | None = None

    def is_active(self, now: datetime) -> bool:
        if self.policy == "fixed_duration" and self.until is not None:
            return now < self.until
        return True

    def remaining(self, now: datetime) -> timedelta | None:
        if self.until is None:
            return None
        return max(timedelta(0), self.until - now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "created_at": self.created_at.isoformat(),
            "policy": self.policy,
            "until": self.until.isoformat() if self.until else None,
            "source_entity": self.source_entity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManualOverride:
        until = data.get("until")
        return cls(
            value=float(data["value"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            policy=data.get("policy", "next_schedule"),
            until=datetime.fromisoformat(until) if until else None,
            source_entity=data.get("source_entity"),
        )


# ---------------------------------------------------------------------------
# Effective setpoint result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SetpointDecision:
    """The single arbitrated setpoint for one zone.

    ``reason_codes`` always explains the decision so the UI and the diagnostics
    export can show *why* a value was chosen (design doc 9.1, 10.3).
    """

    zone_id: str
    effective: float
    base: float
    source: str
    reason_codes: tuple[str, ...] = ()
    adjustment_delta: float = 0.0
    clamped: bool = False
    scaled_outdoor: float | None = None

    @property
    def explanation(self) -> str:
        return ", ".join(self.reason_codes) if self.reason_codes else self.source


@dataclass(frozen=True, slots=True)
class WriteDecision:
    """Whether a zone setpoint must be written, and why."""

    zone_id: str
    entity_id: str
    target: float
    should_write: bool
    reason_codes: tuple[str, ...] = ()
    retry_at: datetime | None = None
    kind: str = "setpoint"


# ---------------------------------------------------------------------------
# Zone runtime state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ZoneState:
    """Everything the coordinator needs to know about one room."""

    zone_id: str
    entity_id: str
    name: str = ""
    mode: str = "auto"
    base_setpoint: float = 20.0
    auto_adjustments: dict[str, float] = field(default_factory=dict)
    effective_setpoint: float | None = None
    actual_setpoint: float | None = None
    current_temperature: float | None = None
    manual_override: ManualOverride | None = None
    pending_command: PendingCommand | None = None
    #: The most recently issued command, kept after its confirmation window
    #: closes. A readback matching it is still *our* echo, never a user
    #: action - without this, a slow KNX readback of a value we sent would be
    #: frozen as a manual override (design doc 4.4.2 / 4.4.6).
    last_command: PendingCommand | None = None
    last_confirmed_write: ConfirmedWrite | None = None
    last_external_change: datetime | None = None
    last_write_at: datetime | None = None
    feedback_quality: str = "valid"
    hvac_action: str | None = None
    hvac_mode: str | None = None
    target_step: float = 0.5
    tolerance: float = 0.2
    supports_action: bool = True
    aux_load_enabled: bool = False
    aux_load_cap: float | None = None
    hvac_action_entity: str | None = None
    cold_adjustment_enabled: bool = False
    reason_codes: tuple[str, ...] = ()

    # --- P0 range limits -------------------------------------------------
    #: Range the *user* configured for this room.
    config_min_temp: float = 16.0
    config_max_temp: float = 26.0
    #: Range the *device* reports. ``None`` until a snapshot carried it.
    device_min_temp: float | None = None
    device_max_temp: float | None = None
    #: Effective P0 window: the intersection of both, refreshed by
    #: :meth:`refresh_limits`.
    min_temp: float = 16.0
    max_temp: float = 26.0

    def refresh_limits(self) -> None:
        """Recompute the effective P0 window.

        The user's configured range is the **outer** bound: it expresses what
        this room is allowed to be driven to, and nothing may widen it. The
        device's reported range can only *narrow* it, because the thermostat
        will not accept values outside its own window.

        When the two are disjoint (a mis-selected entity, or a boiler entity
        wired into a room slot), the configured range wins. Falling back to the
        device window there would be actively dangerous: a room whose config
        says 16-26 °C sitting on an entity that reports 40-82 °C would suddenly
        accept a 40 °C room target.
        """
        low = self.config_min_temp
        high = self.config_max_temp

        device_low = self.device_min_temp
        device_high = self.device_max_temp

        narrowed_low = max(low, device_low) if device_low is not None else low
        narrowed_high = min(high, device_high) if device_high is not None else high

        # Only adopt the narrowing when it leaves a usable window; otherwise the
        # configuration is the safe authority.
        if narrowed_high >= narrowed_low:
            low, high = narrowed_low, narrowed_high

        self.min_temp = low
        self.max_temp = high

    def round_target(self, value: float) -> float:
        """Snap ``value`` onto the device's real step, inside its range."""
        step = self.target_step or 0.5
        snapped = round(value / step) * step
        snapped = max(self.min_temp, min(self.max_temp, snapped))
        return round(snapped, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "entity_id": self.entity_id,
            "name": self.name,
            "mode": self.mode,
            "manual_override": self.manual_override.to_dict() if self.manual_override else None,
            "last_confirmed_write": self.last_confirmed_write.to_dict()
            if self.last_confirmed_write
            else None,
            "pending_command": self.pending_command.to_dict() if self.pending_command else None,
            "last_command": self.last_command.to_dict() if self.last_command else None,
            "last_external_change": self.last_external_change.isoformat()
            if self.last_external_change
            else None,
        }


# ---------------------------------------------------------------------------
# Auxiliary heat load (design doc 5.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuxLoadConfig:
    """Bounded auxiliary heat load, replacing the blueprint's wash sale risk."""

    enabled: bool = False
    delta: float = 0.5
    max_temperature: float = 24.0
    hysteresis: float = 0.3
    min_switch_interval: float = 1800.0
    night_block: bool = True
    night_start: time = time(23, 0)
    night_end: time = time(6, 0)

    def is_night(self, at: time) -> bool:
        if self.night_start <= self.night_end:
            return self.night_start <= at < self.night_end
        return at >= self.night_start or at < self.night_end

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "delta": self.delta,
            "max_temperature": self.max_temperature,
            "hysteresis": self.hysteresis,
            "min_switch_interval": self.min_switch_interval,
            "night_block": self.night_block,
            "night_start": self.night_start.strftime("%H:%M:%S"),
            "night_end": self.night_end.strftime("%H:%M:%S"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuxLoadConfig:
        if not data:
            return cls()
        night_start = data.get("night_start", "23:00:00")
        night_end = data.get("night_end", "06:00:00")

        def _t(value: str | time) -> time:
            if isinstance(value, time):
                return value
            parts = [int(p) for p in str(value).split(":")]
            while len(parts) < 3:
                parts.append(0)
            return time(parts[0], parts[1], parts[2])

        return cls(
            enabled=bool(data.get("enabled", False)),
            delta=float(data.get("delta", 0.5)),
            max_temperature=float(data.get("max_temperature", 24.0)),
            hysteresis=float(data.get("hysteresis", 0.3)),
            min_switch_interval=float(data.get("min_switch_interval", 1800.0)),
            night_block=bool(data.get("night_block", True)),
            night_start=_t(night_start),
            night_end=_t(night_end),
        )


@dataclass(slots=True)
class AuxLoadState:
    """Runtime state of the auxiliary heat load."""

    active: bool = False
    last_switch: datetime | None = None
    #: Per-zone timestamps so one zone's auxiliary slot can be handed over to
    #: another without either of them churning on every cycle.
    zone_switch_at: dict[str, datetime] = field(default_factory=dict)

    def can_switch(self, now: datetime, interval: float, key: str = "") -> bool:
        last = self.zone_switch_at.get(key) if key else self.last_switch
        if last is None:
            return True
        return (now - last).total_seconds() >= interval

    def note_switch(self, now: datetime, key: str = "") -> None:
        if key:
            self.zone_switch_at[key] = now
        else:
            self.last_switch = now

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "last_switch": self.last_switch.isoformat() if self.last_switch else None,
            "zone_switch_at": {k: v.isoformat() for k, v in self.zone_switch_at.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AuxLoadState:
        if not data:
            return cls()
        last = data.get("last_switch")
        switches = data.get("zone_switch_at") or {}
        return cls(
            active=bool(data.get("active", False)),
            last_switch=datetime.fromisoformat(last) if last else None,
            zone_switch_at={str(k): datetime.fromisoformat(str(v)) for k, v in switches.items()},
        )


def with_delta(
    decision: SetpointDecision, delta: float, source: str, reason: str
) -> SetpointDecision:
    """Return a copy of ``decision`` with an extra additive adjustment."""
    return replace(
        decision,
        effective=decision.effective + delta,
        source=source,
        adjustment_delta=decision.adjustment_delta + delta,
        reason_codes=(*decision.reason_codes, reason),
    )
