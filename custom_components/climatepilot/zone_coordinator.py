"""Per-zone write coordination for ClimatePilot (design doc 4.4, 4.7).

This module answers two questions, both of which must never be answered by
guessing:

1. **Did the user change the setpoint, or is this our own write echoing back?**
   KNX panels have no Home Assistant user identity, so ``context.user_id`` is
   only a weak hint (design doc 4.4). The reliable signal is the pending-command
   transaction: a readback that matches a write we issued inside its confirmation
   window is our own echo, anything else is an external change.
2. **Should this setpoint be written right now?** Writes are throttled per zone,
   retried with bounded backoff, and suppressed while the device is unreachable.

The class is pure Python: the caller (``coordinator``) supplies snapshots and
performs the actual service call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .const import (
    DEFAULT_ECHO_GRACE,
    DEFAULT_PENDING_TIMEOUT,
    DEFAULT_WRITE_MIN_INTERVAL,
    DEFAULT_WRITE_RETRY_BACKOFF,
    DEFAULT_WRITE_RETRY_MAX,
    DEFAULT_ZONE_TOLERANCE,
    FEEDBACK_STALE,
    FEEDBACK_UNAVAILABLE,
    FEEDBACK_VALID,
    OVERRIDE_NEXT_SCHEDULE,
    REASON_ENTITY_UNAVAILABLE,
    REASON_LATE_ECHO,
    REASON_THROTTLED,
    REASON_TOLERANCE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from .models import (
    ConfirmedWrite,
    PendingCommand,
    SetpointDecision,
    WriteDecision,
    ZoneState,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .climate_adapter import ClimateSnapshot
    from .temperature_arbiter import TemperatureArbiter


#: Outcome of inspecting a device readback.
SYNC_ECHO = "echo"
SYNC_EXTERNAL = "external"
SYNC_UNCHANGED = "unchanged"
SYNC_UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What a readback turned out to be."""

    kind: str
    zone_id: str
    value: float | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ZoneWritePlan:
    """The full plan for one zone in one evaluation cycle."""

    zone_id: str
    decision: SetpointDecision | None
    write: WriteDecision | None

    @property
    def should_write(self) -> bool:
        return bool(self.write and self.write.should_write)


class ZoneCoordinator:
    """Owns per-zone write state: pending commands, throttling and retries."""

    def __init__(
        self,
        arbiter: TemperatureArbiter,
        *,
        min_write_interval: float = DEFAULT_WRITE_MIN_INTERVAL,
        pending_timeout: float = DEFAULT_PENDING_TIMEOUT,
        echo_grace: float = DEFAULT_ECHO_GRACE,
        retry_max: int = DEFAULT_WRITE_RETRY_MAX,
        retry_backoff: float = DEFAULT_WRITE_RETRY_BACKOFF,
        default_tolerance: float = DEFAULT_ZONE_TOLERANCE,
    ) -> None:
        self.arbiter = arbiter
        self.min_write_interval = min_write_interval
        self.pending_timeout = pending_timeout
        self.echo_grace = echo_grace
        self.retry_max = retry_max
        self.retry_backoff = retry_backoff
        self.default_tolerance = default_tolerance
        #: zone_id -> monotonic-ish count of consecutive failed attempts
        self._failures: dict[str, int] = {}
        self._next_retry: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Reads from the device
    # ------------------------------------------------------------------
    def sync_from_device(
        self,
        zone: ZoneState,
        snapshot: ClimateSnapshot | None,
        now: datetime,
    ) -> SyncOutcome:
        """Fold a device readback into the zone's arbitration state.

        Returns what the readback was, so the coordinator can log or count it.
        """
        if snapshot is None or not snapshot.exists:
            zone.feedback_quality = FEEDBACK_UNAVAILABLE
            return SyncOutcome(
                kind=SYNC_UNCERTAIN,
                zone_id=zone.zone_id,
                value=None,
                reason_codes=(REASON_ENTITY_UNAVAILABLE,),
            )

        raw_state = snapshot.state
        if raw_state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # Unknown is *not* "no demand" and must never become a written
            # value (design doc 6.1 / 11).
            zone.feedback_quality = FEEDBACK_UNAVAILABLE
            return SyncOutcome(
                kind=SYNC_UNCERTAIN,
                zone_id=zone.zone_id,
                value=None,
                reason_codes=(REASON_ENTITY_UNAVAILABLE,),
            )

        zone.feedback_quality = FEEDBACK_VALID
        zone.hvac_action = snapshot.hvac_action
        zone.hvac_mode = snapshot.state
        if snapshot.current_temperature is not None:
            zone.current_temperature = snapshot.current_temperature

        value = snapshot.temperature
        if value is None:
            # The device publishes no setpoint (the Rinnai boiler reports
            # ``temperature: null`` for unrelated reasons). Nothing to compare.
            return SyncOutcome(
                kind=SYNC_UNCHANGED,
                zone_id=zone.zone_id,
                value=None,
            )

        previous = zone.actual_setpoint

        # --- Is this readback explained by a command we issued? ------------
        # Three distinct cases, each with a different safe answer:
        #
        # 1. It matches a command still inside its confirmation window -> our
        #    echo (design doc 4.4.2, test 13.2).
        # 2. It matches a command whose service call *failed* -> still our echo.
        #    The device may well have applied it, and mistaking it for a manual
        #    change would freeze the room on a value we chose ourselves.
        # 3. It matches a command the user has already superseded -> void. A
        #    late echo must never undo a newer manual change (test 13.3).
        #
        # Anything else is an external change. That deliberately includes a
        # value matching a *successful* command whose window has expired: after
        # that point we can no longer tell our echo from a person entering the
        # same number, and design doc 11 requires protecting the possible
        # manual operation.
        pending = zone.pending_command
        last_command = zone.last_command

        if pending is not None and pending.matches(value, now=now):
            return self._accept_echo(zone, pending, value, now)

        if last_command is not None and last_command.matches(value):
            superseded = (
                zone.last_external_change is not None
                and zone.last_external_change > last_command.issued_at
            )
            if superseded:
                return SyncOutcome(
                    kind=SYNC_UNCHANGED,
                    zone_id=zone.zone_id,
                    value=value,
                    reason_codes=(REASON_LATE_ECHO,),
                )
            if last_command.failed:
                # A failed write may still have reached the device. Its echo is
                # recognised for a bounded grace period after the confirmation
                # window - long enough for a slow KNX readback, short enough
                # that an unrelated later entry of the same value is still
                # treated as the user's.
                grace_end = last_command.expires_at + timedelta(seconds=self.echo_grace)
                if now <= grace_end:
                    return self._accept_echo(zone, last_command, value, now)

        zone.actual_setpoint = value

        if previous is not None and abs(previous - value) <= self._tolerance(zone):
            return SyncOutcome(kind=SYNC_UNCHANGED, zone_id=zone.zone_id, value=value)

        # --- Genuine external change --------------------------------------
        # Nothing we issued explains this value, so a human moved it: on a KNX
        # panel or in Home Assistant. It becomes a manual override that
        # automatic arbitration must not touch until its policy expires.
        zone.pending_command = None
        self.arbiter.note_external_change(
            zone,
            value,
            now,
            policy=zone.manual_override.policy if zone.manual_override else OVERRIDE_NEXT_SCHEDULE,
            source_entity=zone.entity_id,
        )
        return SyncOutcome(kind=SYNC_EXTERNAL, zone_id=zone.zone_id, value=value)

    def _accept_echo(
        self,
        zone: ZoneState,
        command: PendingCommand,
        value: float,
        now: datetime,
    ) -> SyncOutcome:
        """Record that a readback confirmed a command we issued."""
        command.confirmed = True
        if zone.pending_command is command:
            zone.pending_command = None
        zone.actual_setpoint = value
        zone.last_confirmed_write = ConfirmedWrite(
            target=value,
            at=now,
            request_id=command.request_id,
            confirmed=True,
        )
        self._failures.pop(zone.zone_id, None)
        self._next_retry.pop(zone.zone_id, None)
        return SyncOutcome(kind=SYNC_ECHO, zone_id=zone.zone_id, value=value)

    # ------------------------------------------------------------------
    # Writes to the device
    # ------------------------------------------------------------------
    def plan_write(
        self,
        zone: ZoneState,
        decision: SetpointDecision,
        now: datetime,
        *,
        enabled: bool,
    ) -> WriteDecision:
        """Decide whether ``decision.effective`` must be sent to ``zone``.

        ``enabled`` is False when the system is observing, paused, or still in
        its startup grace period - the plan then reports why nothing is written.
        """
        target = decision.effective

        if not enabled:
            return WriteDecision(
                zone_id=zone.zone_id,
                entity_id=zone.entity_id,
                target=target,
                should_write=False,
                reason_codes=(*decision.reason_codes, REASON_THROTTLED),
            )

        if zone.feedback_quality == FEEDBACK_UNAVAILABLE:
            return WriteDecision(
                zone_id=zone.zone_id,
                entity_id=zone.entity_id,
                target=target,
                should_write=False,
                reason_codes=(*decision.reason_codes, REASON_ENTITY_UNAVAILABLE),
            )

        # Already there (within the device's own tolerance)?
        actual = zone.actual_setpoint
        if actual is not None and abs(actual - target) <= self._tolerance(zone):
            self._failures.pop(zone.zone_id, None)
            return WriteDecision(
                zone_id=zone.zone_id,
                entity_id=zone.entity_id,
                target=target,
                should_write=False,
                reason_codes=(*decision.reason_codes, REASON_TOLERANCE),
            )

        # A write we already issued is still awaiting confirmation.
        pending = zone.pending_command
        if pending is not None and pending.blocks_new_write(now):
            return WriteDecision(
                zone_id=zone.zone_id,
                entity_id=zone.entity_id,
                target=target,
                should_write=False,
                reason_codes=(*decision.reason_codes, REASON_THROTTLED),
                retry_at=pending.expires_at,
            )

        # Backoff after failures.
        retry_at = self._next_retry.get(zone.zone_id)
        if retry_at is not None and now < retry_at:
            return WriteDecision(
                zone_id=zone.zone_id,
                entity_id=zone.entity_id,
                target=target,
                should_write=False,
                reason_codes=(*decision.reason_codes, REASON_THROTTLED),
                retry_at=retry_at,
            )

        # Minimum interval between writes for this zone.
        if zone.last_write_at is not None:
            elapsed = (now - zone.last_write_at).total_seconds()
            if elapsed < self.min_write_interval:
                blocked_until = zone.last_write_at + timedelta(seconds=self.min_write_interval)
                return WriteDecision(
                    zone_id=zone.zone_id,
                    entity_id=zone.entity_id,
                    target=target,
                    should_write=False,
                    reason_codes=(*decision.reason_codes, REASON_THROTTLED),
                    retry_at=blocked_until,
                )

        return WriteDecision(
            zone_id=zone.zone_id,
            entity_id=zone.entity_id,
            target=target,
            should_write=True,
            reason_codes=decision.reason_codes,
        )

    def note_write_issued(
        self,
        zone: ZoneState,
        plan: WriteDecision,
        now: datetime,
        request_id: str,
    ) -> PendingCommand:
        """Record that a write was issued, opening its confirmation window.

        The command is also retained in ``last_command`` so that a readback
        arriving after the window still counts as our own echo rather than
        being mistaken for a manual change.
        """
        pending = PendingCommand(
            request_id=request_id,
            zone_id=zone.zone_id,
            entity_id=zone.entity_id,
            target=plan.target,
            issued_at=now,
            expires_at=now + timedelta(seconds=self.pending_timeout),
            tolerance=self._tolerance(zone),
        )
        zone.pending_command = pending
        zone.last_command = pending
        zone.last_write_at = now
        zone.effective_setpoint = plan.target
        return pending

    def note_write_failed(self, zone: ZoneState, now: datetime) -> datetime | None:
        """Record a failed write and return when the next attempt may run.

        The command is marked failed rather than dropped: it stops blocking a
        new command, but its echo is still recognisable as ours. Returns
        ``None`` when the retry budget is exhausted; the zone then waits for a
        genuine readback instead of hammering an unreachable device
        (design doc 4.4.5).
        """
        pending = zone.pending_command
        if pending is not None:
            pending.failed = True

        attempts = self._failures.get(zone.zone_id, 0) + 1
        self._failures[zone.zone_id] = attempts
        if attempts > self.retry_max:
            # Budget exhausted: keep the failed command in last_command so its
            # echo is still recognised, but stop scheduling retries.
            zone.pending_command = None
            return None
        delay = self.retry_backoff * attempts
        retry_at = now + timedelta(seconds=delay)
        self._next_retry[zone.zone_id] = retry_at
        zone.pending_command = None
        return retry_at

    def note_write_succeeded(self, zone: ZoneState) -> None:
        """Clear the failure counter after a service call the bus accepted."""
        self._failures.pop(zone.zone_id, None)
        self._next_retry.pop(zone.zone_id, None)

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------
    def expire_pending(self, zones: list[ZoneState], now: datetime) -> list[str]:
        """Drop pending commands that blew their confirmation window.

        The zone is flagged ``stale`` rather than silently rewritten: a readback
        that never arrived means we do not know the device state.
        """
        stale: list[str] = []
        for zone in zones:
            pending = zone.pending_command
            if pending is None:
                continue
            if not pending.is_expired(now):
                continue
            zone.pending_command = None
            if zone.feedback_quality == FEEDBACK_VALID:
                zone.feedback_quality = FEEDBACK_STALE
            stale.append(zone.zone_id)
        return stale

    def mark_unavailable(self, zone: ZoneState) -> None:
        """Freeze a zone whose entity vanished."""
        zone.feedback_quality = FEEDBACK_UNAVAILABLE
        zone.pending_command = None

    def failure_count(self, zone_id: str) -> int:
        return self._failures.get(zone_id, 0)

    def snapshot(self) -> dict[str, object]:
        return {
            "failures": dict(self._failures),
            "next_retry": {k: v.isoformat() for k, v in self._next_retry.items()},
        }

    def restore(self, data: dict[str, object] | None) -> None:
        if not data:
            return
        failures = data.get("failures") or {}
        self._failures = {str(k): int(v) for k, v in failures.items()}
        next_retry = data.get("next_retry") or {}
        self._next_retry = {str(k): datetime.fromisoformat(str(v)) for k, v in next_retry.items()}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _tolerance(self, zone: ZoneState) -> float:
        return zone.tolerance or self.default_tolerance
