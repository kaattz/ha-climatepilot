"""Runtime coordination for ClimatePilot.

``ClimatePilotCoordinator`` is the only place that performs I/O. It wires the
pure engines (arbiter, zone coordinator, weather compensator, heat-source state
machine, burner diagnostics) to Home Assistant's state machine and services,
and it owns what the platform entities read.

Everything the engines decide is derived from the design document
``Heating Coordinator 技术设计方案 V1.0``:

* one effective setpoint per zone, manual overrides always win;
* the heat source is only started/stopped when its semantics were verified;
* a stale outdoor reading keeps the last confirmed safe supply temperature;
* unknown/unavailable states are never interpreted as "no demand".
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .aux_load import AuxLoadController, AuxLoadState
from .climate_adapter import ClimateAdapter, ClimateSnapshot
from .comfort_diagnostics import ComfortAnalyser, ComfortVerdict
from .const import (
    CIRCUIT_HEATING,
    COMFORT_OVERHEATING,
    COMFORT_UNDERHEATING,
    CONF_AUX_LOAD,
    CONF_BOILER_ENTITY,
    CONF_BURNER_CIRCUIT,
    CONF_BURNER_STATE_ENTITY,
    CONF_CONDENSING_RETURN_C,
    CONF_CONTROL_MODE,
    CONF_DELTA_T_HEALTHY_MAX,
    CONF_DELTA_T_HEALTHY_MIN,
    CONF_DELTA_T_WINDOW_SECONDS,
    CONF_HEAT_SOURCE,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PROBE_FROZEN_SECONDS,
    CONF_PROBE_MAX_DIVERGENCE,
    CONF_PROBE_STALE_SECONDS,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SHORT_CYCLE_SECONDS,
    CONF_SUPPLY_PROBE_ENTITY,
    CONF_SYSTEM_NAME,
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_MIN_FLOW,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_WEATHER_CURVE,
    CONF_WEATHER_MODE,
    CONF_ZONES,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    DEFAULT_CONDENSING_RETURN_C,
    DEFAULT_DELTA_T_HEALTHY_MAX,
    DEFAULT_DELTA_T_HEALTHY_MIN,
    DEFAULT_DELTA_T_WINDOW,
    DEFAULT_NAME,
    DEFAULT_PERIODIC_AUDIT,
    DEFAULT_PROBE_FROZEN_AFTER,
    DEFAULT_PROBE_MAX_DIVERGENCE,
    DEFAULT_PROBE_STALE_AFTER,
    DEFAULT_SHORT_CYCLE_SECONDS,
    DEFAULT_STARTUP_GRACE,
    DEFAULT_TARGET_STEP,
    DEFAULT_WEATHER_STALE_AFTER,
    DOMAIN,
    FEEDBACK_UNAVAILABLE,
    FSM_DEGRADED,
    FSM_STATES,
    HVAC_ACTION_HEATING,
    QUALITY_UNKNOWN,
    REASON_CONTROL_PAUSED,
    REASON_DEGRADED,
    REASON_ENTITY_UNAVAILABLE,
    REASON_OBSERVE_MODE,
    REASON_RECOVERY_GRACE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    WEATHER_MODE_APPLY,
    WEATHER_MODE_SUGGEST,
)
from .delta_t_diagnostics import DeltaTDiagnostics, DeltaTReport
from .diagnostics_engine import BurnerStats
from .heat_source import (
    BoilerCapabilities,
    HeatSourceConfig,
    HeatSourceDecision,
    HeatSourceInput,
    HeatSourceStateMachine,
)
from .models import (
    AuxLoadConfig,
    Schedule,
    SetpointDecision,
    WeatherCurve,
    ZoneState,
)
from .probe_monitor import (
    ProbeMonitor,
    ProbePairAssessment,
    ProbeReading,
)
from .store import ClimatePilotStore
from .temperature_arbiter import TemperatureArbiter, active_slot_key
from .weather_compensator import CompensationResult, WeatherCompensator, WeatherReading
from .zone_combinations import CombinationReport, ZoneCombinationTracker
from .zone_coordinator import ZoneCoordinator

_LOGGER = logging.getLogger(__name__)

#: Debounce for the event-driven path (design doc 2: 15-minute polling replaced
#: by events plus a period consistency check).
REFRESH_DEBOUNCE = 2.0


class ClimatePilotCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate every zone and the heat source of one heating system."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.system_id = entry.entry_id
        self.adapter = ClimateAdapter(hass)

        options: dict[str, Any] = {**entry.data, **entry.options}
        self.system_name: str = options.get(CONF_SYSTEM_NAME) or DEFAULT_NAME
        self.boiler_entity: str | None = options.get(CONF_BOILER_ENTITY)
        self.outdoor_entity: str | None = options.get(CONF_OUTDOOR_TEMP_ENTITY)
        self.burner_entity: str | None = options.get(CONF_BURNER_STATE_ENTITY)

        self.control_mode: str = options.get(CONF_CONTROL_MODE, CONTROL_MODE_OBSERVE)

        # --- engines -----------------------------------------------------
        self.arbiter = TemperatureArbiter()
        self.zone_coordinator = ZoneCoordinator(self.arbiter)
        self.weather = WeatherCompensator(
            WeatherCurve.from_dict(options.get(CONF_WEATHER_CURVE)),
            stale_after=float(options.get(CONF_OUTDOOR_STALE_MINUTES, DEFAULT_WEATHER_STALE_AFTER)),
            mode=(
                WEATHER_MODE_APPLY
                if options.get(CONF_WEATHER_MODE) == WEATHER_MODE_APPLY
                and self._semantics_verified(options)
                else WEATHER_MODE_SUGGEST
            ),
        )
        self.burner = BurnerStats(
            short_cycle_seconds=float(
                options.get(CONF_SHORT_CYCLE_SECONDS, DEFAULT_SHORT_CYCLE_SECONDS)
            ),
            # Which circuit the burner sensor watches. The Rinnai integration
            # exposes one sensor per circuit using the *same* value vocabulary,
            # so "Heating Water" on the space-heating sensor means the heating
            # circuit is firing - a fact the raw string cannot convey.
            circuit=options.get(CONF_BURNER_CIRCUIT, CIRCUIT_HEATING),
        )

        capabilities = self._build_capabilities(options)
        self.state_machine = HeatSourceStateMachine(
            HeatSourceConfig.from_dict(options.get(CONF_HEAT_SOURCE)),
            capabilities=capabilities,
        )
        self.aux_controller = AuxLoadController(AuxLoadConfig.from_dict(options.get(CONF_AUX_LOAD)))

        # --- V0.2 diagnostics engines ------------------------------------
        self.combinations_tracker = ZoneCombinationTracker()
        self.comfort_analyser = ComfortAnalyser()

        # --- V0.3 external pipe probes ------------------------------------
        # These are monitoring/optimisation inputs only. They never gate a
        # write: a failed probe degrades the diagnostics, never the heating
        # (design doc 8, V0.3 delivery standard).
        self.probe_monitor = ProbeMonitor(
            stale_after=float(options.get(CONF_PROBE_STALE_SECONDS, DEFAULT_PROBE_STALE_AFTER)),
            frozen_after=float(options.get(CONF_PROBE_FROZEN_SECONDS, DEFAULT_PROBE_FROZEN_AFTER)),
            max_divergence=float(
                options.get(CONF_PROBE_MAX_DIVERGENCE, DEFAULT_PROBE_MAX_DIVERGENCE)
            ),
        )
        self.delta_t_engine = DeltaTDiagnostics(
            window=float(options.get(CONF_DELTA_T_WINDOW_SECONDS, DEFAULT_DELTA_T_WINDOW)),
            healthy_min=float(options.get(CONF_DELTA_T_HEALTHY_MIN, DEFAULT_DELTA_T_HEALTHY_MIN)),
            healthy_max=float(options.get(CONF_DELTA_T_HEALTHY_MAX, DEFAULT_DELTA_T_HEALTHY_MAX)),
            condensing_return_c=float(
                options.get(CONF_CONDENSING_RETURN_C, DEFAULT_CONDENSING_RETURN_C)
            ),
        )
        self.return_probe_entity: str | None = options.get(CONF_RETURN_TEMP_ENTITY)
        self.supply_probe_entity: str | None = options.get(CONF_SUPPLY_PROBE_ENTITY)

        self.store = ClimatePilotStore(hass, self.system_id)

        # --- runtime state ----------------------------------------------
        self.zones: dict[str, ZoneState] = {}
        self.zone_schedules: dict[str, Schedule] = {}
        self.zone_decisions: dict[str, SetpointDecision] = {}
        self.zone_modes_fixed: dict[str, dict[str, float]] = {}
        self.heat_decision: HeatSourceDecision | None = None
        self.compensation: CompensationResult | None = None
        self.aux_state = AuxLoadState()
        self.diagnostics_snapshot: dict[str, Any] = {}

        #: V0.2 diagnostics, refreshed every evaluation cycle.
        self.combinations: CombinationReport = self.combinations_tracker.report(dt_util.now())
        self.comfort: dict[str, ComfortVerdict] = {}

        #: V0.3 probe diagnostics. A ``None`` value is meaningful: it says "no
        #: usable probe", which is different from "the pipe is cold". Seeded
        #: with empty readings so the properties are never ``None`` before the
        #: first cycle.
        self.probes: ProbePairAssessment = self.probe_monitor.assess_pair(
            ProbeReading(entity_id="", value=None, updated_at=None),
            ProbeReading(entity_id="", value=None, updated_at=None),
            dt_util.now(),
        )
        self.delta_t: DeltaTReport = self.delta_t_engine.report(dt_util.now())

        self._zone_slot_keys: dict[str, tuple[Any, ...] | None] = {}
        self._started_at: datetime = dt_util.now()
        self._unsubscribers: list[CALLBACK_TYPE] = []
        self._loaded = False
        self._supply_dirty = False
        self._save_debouncer: Debouncer | None = None
        self._startup_grace = float(options.get("startup_grace", DEFAULT_STARTUP_GRACE))
        self._audit_interval = float(options.get("periodic_audit", DEFAULT_PERIODIC_AUDIT))

        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly rather than relying on the ContextVar: Home
            # Assistant reports that pattern and breaks it in 2026.8.
            config_entry=entry,
            name=f"{DOMAIN}:{self.system_name}",
            update_interval=timedelta(seconds=self._audit_interval),
        )
        # ``name`` belongs to DataUpdateCoordinator (it is the logger label), so
        # the human-facing system name is kept separately rather than shadowing
        # it - otherwise the device would be called "climatepilot:<name>".
        self.name = self.system_name

        self._build_zones(options)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _semantics_verified(options: dict[str, Any]) -> bool:
        """All three device-semantics attestations must be true (6.3, 14)."""
        return all(
            bool(options.get(key))
            for key in (
                CONF_VERIFY_HEATING_ONLY,
                CONF_VERIFY_SUPPLY_TEMP,
                CONF_VERIFY_MIN_FLOW,
            )
        )

    def _build_capabilities(self, options: dict[str, Any]) -> BoilerCapabilities | None:
        if not self.boiler_entity:
            return None
        snapshot = self.adapter.snapshot(self.boiler_entity)
        return BoilerCapabilities(
            entity_id=self.boiler_entity,
            min_temp=snapshot.min_temp,
            max_temp=snapshot.max_temp,
            target_step=snapshot.target_step or 1.0,
            hvac_modes=snapshot.hvac_modes,
            semantics_verified=self._semantics_verified(options),
            min_flow_verified=bool(options.get(CONF_VERIFY_MIN_FLOW)),
        )

    def _build_zones(self, options: dict[str, Any]) -> None:
        self.zones.clear()
        self.zone_schedules.clear()
        self.zone_modes_fixed.clear()
        for raw in options.get(CONF_ZONES) or []:
            if not isinstance(raw, dict) or not raw.get("entity_id"):
                continue
            zone_id = str(raw.get("zone_id") or raw["entity_id"].split(".", 1)[-1])
            snapshot = self.adapter.snapshot(str(raw["entity_id"]))
            zone = self.adapter.to_zone_state({**raw, "zone_id": zone_id}, snapshot)
            self.zones[zone_id] = zone
            self.zone_schedules[zone_id] = Schedule.from_dict(raw.get("schedule"))
            self.zone_modes_fixed[zone_id] = {
                str(k): float(v) for k, v in (raw.get("modes") or {}).items()
            }
            self._zone_slot_keys[zone_id] = None

    @property
    def zone_ids(self) -> list[str]:
        return list(self.zones)

    def zone_schedule(self, zone_id: str) -> Schedule:
        return self.zone_schedules.get(zone_id) or Schedule()

    @property
    def writes_enabled(self) -> bool:
        """Zone writes are allowed only in coordinate mode and past the grace."""
        return self.control_mode == CONTROL_MODE_COORDINATE and self._grace_elapsed

    @property
    def heat_source_enabled(self) -> bool:
        """Heat-source writes additionally require verified semantics."""
        if not self.writes_enabled:
            return False
        caps = self.state_machine.capabilities
        return bool(caps and caps.semantics_verified)

    def _now(self) -> datetime:
        """The single clock this coordinator reads.

        Every time-dependent decision goes through here so the recovery grace
        period, the override deadlines and the evaluation cycle can never
        disagree about what time it is - and so the whole control loop can be
        driven deterministically in tests.
        """
        return dt_util.now()

    @property
    def _grace_elapsed(self) -> bool:
        return (self._now() - self._started_at).total_seconds() >= self._startup_grace

    @property
    def heat_state(self) -> str:
        return self.state_machine.state if self.state_machine else FSM_DEGRADED

    @property
    def active_zone_count(self) -> int:
        return sum(1 for z in self.zones.values() if self._zone_demands_heat(z))

    @property
    def unknown_zone_count(self) -> int:
        return sum(1 for z in self.zones.values() if z.feedback_quality == FEEDBACK_UNAVAILABLE)

    def setpoint_source(self, zone_id: str) -> str | None:
        decision = self.zone_decisions.get(zone_id)
        return decision.source if decision else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @callback
    def async_setup_listeners(self) -> None:
        """Start watching entities and the periodic audit.

        A plain callback rather than a coroutine: registering listeners is
        synchronous work, and making it ``async`` invited a caller to forget the
        ``await`` - which silently left the integration deaf to every state
        change.
        """
        watched = {z.entity_id for z in self.zones.values()}
        if self.boiler_entity:
            watched.add(self.boiler_entity)
        if self.outdoor_entity:
            watched.add(self.outdoor_entity)

        if watched:
            self._unsubscribers.append(
                async_track_state_change_event(self.hass, sorted(watched), self._handle_state_event)
            )

        self._unsubscribers.append(
            async_track_time_interval(
                self.hass, self._handle_audit, timedelta(seconds=self._audit_interval)
            )
        )

        self._save_debouncer = Debouncer(
            self.hass,
            _LOGGER,
            cooldown=10,
            immediate=False,
            function=self._async_save_state,
        )

    @callback
    def async_shutdown_listeners(self) -> None:
        while self._unsubscribers:
            self._unsubscribers.pop()()

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------
    @callback
    def _handle_state_event(self, event: Event) -> None:
        self.hass.async_create_task(self._async_debounced_refresh())

    async def _async_debounced_refresh(self) -> None:
        """Coalesce bursts of KNX telegrams into one evaluation cycle."""
        if self._save_debouncer is None:
            await self.async_request_refresh()
            return
        await self.async_request_refresh()

    @callback
    def _handle_audit(self, now: datetime) -> None:
        self.hass.async_create_task(self.async_request_refresh())

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        """Run one full evaluation cycle."""
        if not self._loaded:
            await self._async_restore_state()
            self._loaded = True

        now = self._now()
        try:
            await self._async_sync_zones(now)
            self._async_arbitrate_zones(now)
            await self._async_apply_zone_writes(now)
            await self._async_evaluate_weather(now)
            await self._async_evaluate_heat_source(now)
            self._async_update_diagnostics(now)
        except Exception as err:
            _LOGGER.exception("ClimatePilot evaluation cycle failed")
            raise UpdateFailed(str(err)) from err

        self._async_schedule_save()
        return {
            "zones": self.zones,
            "heat_state": self.heat_state,
            "control_mode": self.control_mode,
        }

    async def _async_sync_zones(self, now: datetime) -> None:
        stale = self.zone_coordinator.expire_pending(list(self.zones.values()), now)
        for zone_id in stale:
            _LOGGER.warning(
                "Zone %s did not confirm a setpoint write within the window; "
                "marking feedback as stale",
                zone_id,
            )

        for zone in self.zones.values():
            snapshot = self.adapter.snapshot(zone.entity_id)
            if snapshot.exists:
                self._apply_capabilities(zone, snapshot)
            self.zone_coordinator.sync_from_device(zone, snapshot, now)
            self._update_custom_demand(zone)

    @staticmethod
    def _apply_capabilities(zone: ZoneState, snapshot: ClimateSnapshot) -> None:
        """Adopt the device's reported range and step.

        The device range is recorded separately from the user's configured
        range and the two are then intersected by ``refresh_limits()``. Writing
        ``min_temp``/``max_temp`` directly here would discard the narrowing the
        user configured - a room limited to 18-30 °C on a 16-26 °C thermostat
        must stay 18-26 °C, not silently widen back to 16-26 °C.
        """
        if snapshot.min_temp is not None:
            zone.device_min_temp = snapshot.min_temp
        if snapshot.max_temp is not None:
            zone.device_max_temp = snapshot.max_temp
        zone.refresh_limits()
        if snapshot.target_step:
            zone.target_step = snapshot.target_step
        elif not zone.target_step:
            zone.target_step = DEFAULT_TARGET_STEP
        zone.supports_action = snapshot.hvac_action is not None

    def _update_custom_demand(self, zone: ZoneState) -> None:
        """Adaptation mode: an explicit request entity replaces hvac_action.

        Not every KNX thermostat publishes a trustworthy ``hvac_action``. For
        those, design doc 6.1 allows a dedicated request object / binary sensor
        to be configured instead.
        """
        if not zone.hvac_action_entity:
            return
        state = self.hass.states.get(zone.hvac_action_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            # An unreadable request entity is *unknown*, never "no demand".
            zone.hvac_action = None
            zone.feedback_quality = FEEDBACK_UNAVAILABLE
            return
        zone.hvac_action = (
            HVAC_ACTION_HEATING
            if state.state.lower() in ("on", "heat", "heating", "true", "1")
            else "idle"
        )

    def _async_arbitrate_zones(self, now: datetime) -> None:
        cold_delta = self._cold_adjustment_delta(now)
        for zone_id, zone in self.zones.items():
            schedule = self.zone_schedule(zone_id)

            # Release next_schedule overrides exactly at a switch point.
            key = active_slot_key(schedule, now)
            previous_key = self._zone_slot_keys.get(zone_id)
            if (
                previous_key is not None
                and key != previous_key
                and self.arbiter.note_schedule_transition(zone, now)
            ):
                _LOGGER.debug(
                    "Released schedule-bound manual override for zone %s",
                    zone_id,
                )
            self._zone_slot_keys[zone_id] = key

            # Auxiliary load first: the delta is derived from the *base* target.
            base_decision = self.arbiter.arbitrate(zone, now, schedule=schedule)
            zone.base_setpoint = base_decision.effective

            aux_delta = self._aux_delta(zone, now, base_decision.effective)

            decision = self.arbiter.arbitrate(
                zone,
                now,
                schedule=schedule,
                cold_adjustment=(cold_delta if zone.cold_adjustment_enabled else 0.0),
                aux_delta=aux_delta,
            )
            zone.effective_setpoint = decision.effective
            zone.reason_codes = decision.reason_codes
            self.zone_decisions[zone_id] = decision

        released = self.arbiter.expire_overrides(list(self.zones.values()), now)
        for zone_id in released:
            _LOGGER.info("Manual override for zone %s expired", zone_id)

    def _cold_adjustment_delta(self, now: datetime) -> float:
        """Optional room-target bump during a cold snap (design doc 5.2).

        Deliberately independent from the weather *water* compensation: raising
        both at once double-counts the same cold weather.
        """
        if not self.arbiter.config.cold_adjustment_enabled_by_default:
            return 0.0
        if self.compensation is None or self.compensation.outdoor is None:
            return 0.0
        threshold = self.weather.curve.outdoor_design
        if self.compensation.outdoor <= threshold:
            return self.arbiter.config.cold_adjustment_delta
        return 0.0

    def _aux_delta(self, zone: ZoneState, now: datetime, base_target: float) -> float:
        """Bounded auxiliary-load delta, referenced to the *base* target.

        Only one zone may hold the auxiliary slot at a time: the first eligible
        zone in configuration order wins, so a second eligible room cannot turn
        the load into an unbounded whole-house boost.
        """
        eligible = [
            z
            for z in self.zones.values()
            if z.aux_load_enabled
            and z.manual_override is None
            and self.aux_controller.config.enabled
        ]
        holder_id = eligible[0].zone_id if eligible else None
        in_use = holder_id == zone.zone_id

        other_demand = sum(
            1
            for other in self.zones.values()
            if other.zone_id != zone.zone_id and self._zone_demands_heat(other)
        )

        decision = self.aux_controller.evaluate(
            self.aux_state,
            zone,
            now,
            other_demand=other_demand,
            base_target=base_target,
            override=zone.manual_override,
            in_use=in_use,
        )
        if decision.active:
            zone.auto_adjustments["aux_load"] = decision.delta
            return decision.delta
        zone.auto_adjustments.pop("aux_load", None)
        return 0.0

    async def _async_apply_zone_writes(self, now: datetime) -> None:
        if not self.writes_enabled:
            return
        for zone_id, zone in self.zones.items():
            decision = self.zone_decisions.get(zone_id)
            if decision is None:
                continue
            plan = self.zone_coordinator.plan_write(zone, decision, now, enabled=True)
            if not plan.should_write:
                continue

            request_id = uuid.uuid4().hex
            self.zone_coordinator.note_write_issued(zone, plan, now, request_id)
            ok = await self.adapter.async_set_temperature(zone.entity_id, plan.target)
            if ok:
                self.zone_coordinator.note_write_succeeded(zone)
                _LOGGER.debug(
                    "Set %s to %.1f (%s)",
                    zone.entity_id,
                    plan.target,
                    ",".join(plan.reason_codes),
                )
            else:
                retry_at = self.zone_coordinator.note_write_failed(zone, now)
                _LOGGER.warning(
                    "Write to %s failed (%s); %s",
                    zone.entity_id,
                    ",".join(plan.reason_codes),
                    f"retrying at {retry_at}" if retry_at else "retry budget exhausted",
                )

    async def _async_evaluate_weather(self, now: datetime) -> None:
        reading = self._outdoor_reading(now)
        result = self.weather.evaluate(reading, now, night=self._is_night(now))
        self.compensation = result

    def _outdoor_reading(self, now: datetime) -> WeatherReading:
        if not self.outdoor_entity:
            from .const import FEEDBACK_UNAVAILABLE as _UNAVAILABLE

            return WeatherReading(value=None, updated_at=None, quality=_UNAVAILABLE)

        state = self.hass.states.get(self.outdoor_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return WeatherReading(value=None, updated_at=None, quality=FEEDBACK_UNAVAILABLE)
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return WeatherReading(value=None, updated_at=None, quality=FEEDBACK_UNAVAILABLE)
        return WeatherReading(value=value, updated_at=state.last_updated, quality="valid")

    def _is_night(self, now: datetime) -> bool:
        cfg = self.aux_controller.config
        return cfg.is_night(now.timetz().replace(tzinfo=None))

    async def _async_evaluate_heat_source(self, now: datetime) -> None:
        caps = self.state_machine.capabilities
        boiler_available = True
        boiler_mode: str | None = None
        if self.boiler_entity:
            snapshot = self.adapter.snapshot(self.boiler_entity)
            boiler_available = snapshot.exists and snapshot.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            )
            boiler_mode = snapshot.state

        has_demand = self.active_zone_count > 0
        self.state_machine.note_demand_change(now, has_demand)

        data = HeatSourceInput(
            now=now,
            active_zones=self.active_zone_count,
            unknown_zones=self.unknown_zone_count,
            control_mode=self.control_mode,
            boiler_mode=boiler_mode,
            boiler_available=boiler_available,
            zones_faulted=self.unknown_zone_count > 0,
        )
        supply = self.compensation.suggested if self.compensation else None
        decision = self.state_machine.evaluate(data, supply=supply)
        self.heat_decision = decision

        if not self.heat_source_enabled or caps is None:
            return

        if decision.decision == "degraded":
            return

        if decision.target_supply is not None and self._supply_is_new(decision.target_supply):
            ok = await self.adapter.async_set_supply_temperature(
                caps.entity_id, decision.target_supply
            )
            if ok:
                self.weather.note_applied(decision.target_supply, now)
                _LOGGER.debug(
                    "Supply temperature set to %.1f (%s)",
                    decision.target_supply,
                    ",".join(decision.reason_codes),
                )

        if decision.target_mode is not None and decision.target_mode != boiler_mode:
            ok = await self.adapter.async_set_hvac_mode(caps.entity_id, decision.target_mode)
            if ok:
                _LOGGER.info(
                    "Heat source %s -> %s (%s)",
                    caps.entity_id,
                    decision.target_mode,
                    ",".join(decision.reason_codes),
                )

    def _supply_is_new(self, target: float) -> bool:
        snapshot = self.adapter.snapshot(self.state_machine.capabilities.entity_id)  # type: ignore[union-attr]
        current = snapshot.temperature
        if current is None:
            return True
        step = snapshot.target_step or 1.0
        return abs(current - target) >= max(step, 0.1)

    # ------------------------------------------------------------------
    # V0.2 diagnostics
    # ------------------------------------------------------------------
    def _async_update_v02_diagnostics(self, now: datetime) -> None:
        """Refresh the zone-combination and comfort diagnostics.

        Both engines are observation-only: they never influence arbitration or
        what gets written to a device. Their output exists to answer "why is the
        system behaving like this" (design doc 8, 10.3).
        """
        # --- zone combinations -------------------------------------------
        demanding = sorted(
            zone_id for zone_id, zone in self.zones.items() if self._zone_demands_heat(zone)
        )
        self.combinations_tracker.note_demand(demanding, now)
        self.combinations = self.combinations_tracker.report(now)

        # --- per-zone comfort --------------------------------------------
        for zone_id, zone in self.zones.items():
            if zone.feedback_quality == FEEDBACK_UNAVAILABLE:
                # No trustworthy reading: the analyser records nothing and
                # reports "unknown" rather than inventing a temperature.
                current: float | None = None
            else:
                current = zone.current_temperature

            self.comfort_analyser.note(
                zone_id,
                current=current,
                target=zone.effective_setpoint,
                demanding=self._zone_demands_heat(zone),
                now=now,
            )

        # Drop zones that were removed from the options, so a renamed or
        # deleted room cannot leave a stale verdict behind.
        for stale_id in set(self.comfort_analyser.verdicts(now)) - set(self.zones):
            self.comfort_analyser.drop_zone(stale_id)

        self.comfort = self.comfort_analyser.verdicts(now)

        self._async_update_v03_diagnostics(now)

    def _async_update_v03_diagnostics(self, now: datetime) -> None:
        """Refresh the external pipe-probe diagnostics.

        Strictly observation-only, by design: the V0.3 delivery standard is that
        a failed probe must not affect core heating. Nothing here feeds back
        into arbitration, the state machine, or any write - the probes exist to
        explain *why* the system behaves as it does, and to suggest a curve
        calibration the user may choose to apply.

        ``None`` is carried through rather than substituted: an absent reading
        means "no usable probe", which must never be rendered as a cold pipe.
        """
        burning = bool((self.diagnostics_snapshot.get("burner") or {}).get("burning", False))

        supply_reading = self._probe_reading(self.supply_probe_entity)
        return_reading = self._probe_reading(self.return_probe_entity)

        self.probes = self.probe_monitor.assess_pair(
            supply_reading, return_reading, now, burning=burning
        )

        # The delta-T engine only sees values that survived the probe checks.
        supply_value = self.probes.supply.value if self.probes.supply.usable else None
        return_value = self.probes.return_.value if self.probes.return_.usable else None

        underheating = sum(
            1 for verdict in self.comfort.values() if verdict.verdict == COMFORT_UNDERHEATING
        )
        overheating = sum(
            1 for verdict in self.comfort.values() if verdict.verdict == COMFORT_OVERHEATING
        )

        self.delta_t_engine.note(
            supply=supply_value,
            return_=return_value,
            burning=burning,
            active_zones=self.active_zone_count,
            now=now,
            underheating_zones=underheating,
            overheating_zones=overheating,
        )
        self.delta_t = self.delta_t_engine.report(now)

    def _probe_reading(self, entity_id: str | None) -> ProbeReading:
        """Read one probe entity into a raw, un-judged reading.

        Deliberately does not interpret: sentinel rejection, range checks and
        staleness belong to the probe monitor, so the policy lives in one place
        and is testable without Home Assistant.
        """
        if not entity_id:
            return ProbeReading(entity_id="", value=None, updated_at=None, raw=None)

        state = self.hass.states.get(entity_id)
        if state is None:
            return ProbeReading(entity_id=entity_id, value=None, updated_at=None)

        raw = state.state
        if raw in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return ProbeReading(entity_id=entity_id, value=None, updated_at=None, raw=raw)

        try:
            value = float(raw)
        except (TypeError, ValueError):
            return ProbeReading(entity_id=entity_id, value=None, updated_at=None, raw=raw)

        return ProbeReading(
            entity_id=entity_id,
            value=value,
            updated_at=state.last_updated,
            raw=raw,
        )

    @property
    def probe_fault(self) -> bool:
        """True when any *configured* probe is not usable.

        An unconfigured probe is not a fault: most installations have no
        external probes at all, and reporting a permanent fault would train the
        user to ignore the flag.
        """
        if self.supply_probe_entity and not self.probes.supply.usable:
            return True
        return bool(self.return_probe_entity and not self.probes.return_.usable)

    @property
    def probes_configured(self) -> bool:
        return bool(self.supply_probe_entity or self.return_probe_entity)

    @property
    def cycling_quality(self) -> str:
        """Combustion quality grade over the diagnostics window."""
        burner_snapshot = self.diagnostics_snapshot.get("burner") or {}
        return str(burner_snapshot.get("cycling_quality", QUALITY_UNKNOWN))

    @property
    def cycling_quality_reason(self) -> str:
        burner_snapshot = self.diagnostics_snapshot.get("burner") or {}
        return str(burner_snapshot.get("cycling_quality_reason", ""))

    @property
    def unexplained_burns(self) -> int:
        """Ignitions in the last 24 h with no zone demanding heat."""
        burner_snapshot = self.diagnostics_snapshot.get("burner") or {}
        return int(burner_snapshot.get("unexplained_burns_24h", 0))

    def comfort_for(self, zone_id: str) -> ComfortVerdict | None:
        return self.comfort.get(zone_id)

    def _async_update_diagnostics(self, now: datetime) -> None:
        """Feed the burner statistics.
        A dedicated burner-state sensor (the Rinnai integration exposes one per
        circuit) is preferred over the boiler's own ``hvac_action``, because it
        lets heating burns be told apart from domestic-hot-water burns.
        """
        source: str | None = None
        if self.burner_entity:
            state = self.hass.states.get(self.burner_entity)
            source = state.state if state is not None else None
        elif self.boiler_entity:
            source = self.adapter.snapshot(self.boiler_entity).hvac_action

        # The burner correlation needs to know whether *we* were asking for
        # heat, so it must be told before the state is fed in - otherwise a
        # demand-less ignition would be attributed to a live demand.
        self.burner.note_demand(now, self.active_zone_count > 0)
        self.burner.note_state(source, now)

        self._async_update_v02_diagnostics(now)

        self.diagnostics_snapshot = {
            "burner": self.burner.snapshot(now),
            "heat_state": self.heat_state,
            "heat_decision": self.heat_decision.decision if self.heat_decision else None,
            "active_zones": self.active_zone_count,
            "unknown_zones": self.unknown_zone_count,
            "control_mode": self.control_mode,
            "zones": {
                zid: {
                    "effective_setpoint": zone.effective_setpoint,
                    "actual_setpoint": zone.actual_setpoint,
                    "current_temperature": zone.current_temperature,
                    "source": self.zone_decisions[zid].source
                    if zid in self.zone_decisions
                    else None,
                    "reason_codes": list(zone.reason_codes),
                    "feedback_quality": zone.feedback_quality,
                    "manual_override": zone.manual_override is not None,
                }
                for zid, zone in self.zones.items()
            },
        }

    # ------------------------------------------------------------------
    # Demand detection
    # ------------------------------------------------------------------
    @staticmethod
    def _zone_demands_heat(zone: ZoneState) -> bool:
        """``heat`` mode is *not* demand; only a real heating action is."""
        if zone.feedback_quality == FEEDBACK_UNAVAILABLE:
            return False
        if zone.hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
            return False
        return zone.hvac_action == "heating"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def _async_restore_state(self) -> None:
        data = await self.store.async_load()
        if not data:
            return
        stored_zones = data.get("zones") or {}
        for zone_id, payload in stored_zones.items():
            zone = self.zones.get(zone_id)
            if zone is None:
                continue
            override = payload.get("manual_override")
            if override:
                from .models import ManualOverride

                restored = ManualOverride.from_dict(override)
                # A fixed-duration override keeps its absolute expiry, so a
                # restart recomputes the remaining time instead of extending it.
                if restored.is_active(self._now()):
                    zone.manual_override = restored
            confirmed = payload.get("last_confirmed_write")
            if confirmed:
                from .models import ConfirmedWrite

                zone.last_confirmed_write = ConfirmedWrite.from_dict(confirmed)
            external = payload.get("last_external_change")
            if external:
                zone.last_external_change = datetime.fromisoformat(external)

        self.aux_controller.restore(self.aux_state, data.get("aux_load"), self._now())
        self.zone_coordinator.restore(data.get("zone_coordinator"))
        self.state_machine.restore(data.get("heat_source"))
        self.weather.restore(data.get("weather"))
        self.burner.restore(data.get("burner"))
        # V0.2 diagnostics. Restoring them matters practically: a restart would
        # otherwise reset the burner history and make the system look like it
        # had just been commissioned, hiding exactly the cycling problems the
        # diagnostics exist to surface.
        self.combinations_tracker.restore(data.get("combinations"))
        self.comfort_analyser.restore(data.get("comfort"))
        self.probe_monitor.restore(data.get("probes"))
        self.delta_t_engine.restore(data.get("delta_t"))
        _LOGGER.info("Restored ClimatePilot state for %d zones", len(stored_zones))

    def _async_schedule_save(self) -> None:
        if self._save_debouncer is not None:
            self._save_debouncer.async_schedule_call()

    async def _async_save_state(self) -> None:
        await self.store.async_save(self._state_payload())

    def _state_payload(self) -> dict[str, Any]:
        now = self._now()
        return {
            "zones": {zid: zone.to_dict() for zid, zone in self.zones.items()},
            "aux_load": self.aux_state.to_dict(),
            "zone_coordinator": self.zone_coordinator.snapshot(),
            "heat_source": self.state_machine.snapshot(),
            "weather": self.weather.snapshot(),
            "burner": self.burner.snapshot(now),
            "combinations": self.combinations_tracker.snapshot(),
            "comfort": self.comfort_analyser.snapshot(),
            "probes": self.probe_monitor.snapshot(),
            "delta_t": self.delta_t_engine.snapshot(),
            "control_mode": self.control_mode,
        }

    # ------------------------------------------------------------------
    # Commands used by entities and services
    # ------------------------------------------------------------------
    async def async_set_control_mode(self, mode: str) -> None:
        self.control_mode = mode
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_CONTROL_MODE: mode}
        )
        await self.async_request_refresh()

    async def async_resume_auto(self, zone_id: str | None = None) -> None:
        now = self._now()
        targets = (
            [self.zones[zone_id]]
            if zone_id and zone_id in self.zones
            else list(self.zones.values())
        )
        for zone in targets:
            self.arbiter.resume_auto(zone, now)
        await self.async_request_refresh()
        await self._async_save_state()

    async def async_set_zone_mode(self, zone_id: str, mode: str) -> None:
        zone = self.zones.get(zone_id)
        if zone is None:
            return
        if mode != "manual":
            self.arbiter.resume_auto(zone, self._now())
        zone.mode = mode
        await self.async_request_refresh()

    async def async_set_zone_aux_load(self, zone_id: str, enabled: bool) -> None:
        """Enable/disable a zone as the auxiliary heat load.

        Switching this off immediately releases the auxiliary slot so the room
        returns to its own target (design doc 5.3, 13.8).
        """
        zone = self.zones.get(zone_id)
        if zone is None:
            return
        zone.aux_load_enabled = enabled
        if not enabled:
            self.aux_state.zone_switch_at.pop(zone_id, None)
            zone.auto_adjustments.pop("aux_load", None)
            self.aux_state.active = any(
                v is not None for v in self.aux_state.zone_switch_at.values()
            )
        await self.async_request_refresh()

    async def async_reconfigure(self) -> None:
        """Rebuild runtime state after the options were changed.

        The arbitration state (manual overrides, pending commands, burner
        history) is deliberately preserved: reloading options must not silently
        discard a user's override (design doc 4.3, 11).
        """
        options: dict[str, Any] = {**self.entry.data, **self.entry.options}
        self.system_name = options.get(CONF_SYSTEM_NAME) or self.system_name
        self.name = self.system_name
        self.boiler_entity = options.get(CONF_BOILER_ENTITY)
        self.outdoor_entity = options.get(CONF_OUTDOOR_TEMP_ENTITY)
        self.burner_entity = options.get(CONF_BURNER_STATE_ENTITY)
        self.control_mode = options.get(CONF_CONTROL_MODE, self.control_mode)
        self.weather.curve = WeatherCurve.from_dict(options.get(CONF_WEATHER_CURVE))

        # The capabilities are re-read from the device, so the control loop has
        # to be rebuilt rather than mutated: HeatSourceStateMachine exposes
        # ``capabilities`` as read-only on purpose, so a capability cannot be
        # changed underneath a running state machine. The FSM's own runtime
        # state is carried across so a reconfigure never fakes a fresh start.
        previous_fsm = self.state_machine
        self.state_machine = HeatSourceStateMachine(
            HeatSourceConfig.from_dict(options.get(CONF_HEAT_SOURCE)),
            capabilities=self._build_capabilities(options),
        )
        self.state_machine.restore(previous_fsm.snapshot())

        # Rebuild zones, carrying over the arbitration state of zones whose
        # thermostat did not change. ``previous`` must be a copy: ``_build_zones``
        # clears ``self.zones`` in place, so holding the dict itself would leave
        # nothing to carry across and silently drop every manual override.
        previous = dict(self.zones)
        self._build_zones(options)
        for zone_id, zone in self.zones.items():
            old = previous.get(zone_id)
            if old is None or old.entity_id != zone.entity_id:
                self._zone_slot_keys[zone_id] = None
                continue
            zone.manual_override = old.manual_override
            zone.pending_command = old.pending_command
            zone.last_command = old.last_command
            zone.last_confirmed_write = old.last_confirmed_write
            zone.last_external_change = old.last_external_change
            zone.last_write_at = old.last_write_at
            zone.actual_setpoint = old.actual_setpoint

        await self.async_request_refresh()

    async def async_set_zone_fixed_target(self, zone_id: str, mode: str, value: float) -> None:
        self.zone_modes_fixed.setdefault(zone_id, {})[mode] = value
        await self.async_request_refresh()

    async def async_set_supply_temperature(self, temperature: float) -> None:
        """Explicit supply-temperature request from the number entity/service."""
        caps = self.state_machine.capabilities
        if caps is None:
            _LOGGER.warning("No heat source configured; supply request ignored")
            return
        if not self.state_machine.can_write_supply:
            _LOGGER.warning(
                "Supply temperature write refused: device semantics are not "
                "verified, or the system is not coordinating"
            )
            return
        target = temperature
        if caps.min_temp is not None:
            target = max(caps.min_temp, target)
        if caps.max_temp is not None:
            target = min(caps.max_temp, target)
        if await self.adapter.async_set_supply_temperature(caps.entity_id, target):
            self.weather.note_applied(target, self._now())

    async def async_reassess(self) -> None:
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Diagnostics for the binary sensors
    # ------------------------------------------------------------------
    @property
    def short_cycle_alert(self) -> bool:
        snap = self.diagnostics_snapshot.get("burner") or {}
        return bool(snap.get("short_cycle_alert"))

    @property
    def degraded(self) -> bool:
        return self.heat_state == FSM_DEGRADED or self.unknown_zone_count > 0

    def reason_codes_for(self, zone_id: str) -> list[str]:
        decision = self.zone_decisions.get(zone_id)
        return list(decision.reason_codes) if decision else []

    def supply_temperature_reading(self) -> float | None:
        if not self.boiler_entity:
            return None
        snapshot = self.adapter.snapshot(self.boiler_entity)
        return snapshot.temperature


# Re-exported for the platform layer and the full integration test suite.
__all__ = [
    "FSM_STATES",
    "REASON_CONTROL_PAUSED",
    "REASON_DEGRADED",
    "REASON_ENTITY_UNAVAILABLE",
    "REASON_OBSERVE_MODE",
    "REASON_RECOVERY_GRACE",
    "ClimatePilotCoordinator",
]
