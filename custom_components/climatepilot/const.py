"""Constants for the ClimatePilot integration.

ClimatePilot (智暖领航) is a multi-zone heating coordination layer for Home
Assistant. The domain is ``climatepilot`` and the integration is published by
HomeAxis.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "climatepilot"

# ---------------------------------------------------------------------------
# Branding / attribution
# ---------------------------------------------------------------------------
DEFAULT_NAME: Final = "ClimatePilot"
MANUFACTURER: Final = "HomeAxis"
MODEL: Final = "Heating Coordinator"

# ---------------------------------------------------------------------------
# Config entry data / options keys
# ---------------------------------------------------------------------------
CONF_SYSTEM_NAME: Final = "system_name"
CONF_BOILER_ENTITY: Final = "boiler_entity"
CONF_OUTDOOR_TEMP_ENTITY: Final = "outdoor_temperature_entity"
CONF_BURNER_STATE_ENTITY: Final = "burner_state_entity"
#: Which hydraulic circuit the burner-state sensor watches. The Rinnai
#: integration exposes one burner sensor per circuit using the *same* value
#: vocabulary ("Standby" / "Heating Water" / "Burning" / "Error"), so the raw
#: string alone cannot say whether a burn is space heating or hot water - only
#: knowing which circuit the sensor belongs to can.
CONF_BURNER_CIRCUIT: Final = "burner_circuit"
CONF_SUPPLY_TEMP_ENTITY: Final = "supply_temp_entity"
CONF_RETURN_TEMP_ENTITY: Final = "return_temp_entity"
CONF_ZONES: Final = "zones"
CONF_WEATHER_CURVE: Final = "weather_curve"
CONF_WEATHER_MODE: Final = "weather_mode"
CONF_HEAT_SOURCE: Final = "heat_source"
CONF_AUX_LOAD: Final = "aux_load"
CONF_CONTROL_MODE: Final = "control_mode"

# Per-zone option keys (inside a CONF_ZONES list entry)
CONF_ZONE_ID: Final = "zone_id"
CONF_ZONE_ENTITY: Final = "entity_id"
CONF_ZONE_NAME: Final = "name"
CONF_ZONE_MODE: Final = "mode"
CONF_ZONE_MIN_TEMP: Final = "min_temp"
CONF_ZONE_MAX_TEMP: Final = "max_temp"
CONF_ZONE_STEP: Final = "target_step"
CONF_ZONE_TOLERANCE: Final = "tolerance"
CONF_ZONE_SCHEDULE: Final = "schedule"
CONF_ZONE_SLOTS: Final = "slots"
CONF_ZONE_MODES: Final = "modes"
CONF_ZONE_OVERRIDE_POLICY: Final = "override_policy"
CONF_ZONE_OVERRIDE_MINUTES: Final = "override_minutes"
CONF_ZONE_COLD_ADJUSTMENT: Final = "cold_adjustment"
CONF_ZONE_AUX_LOAD: Final = "aux_load"
CONF_ZONE_ACTION_ENTITY: Final = "hvac_action_entity"

# System-level tuning keys
CONF_OUTDOOR_STALE_MINUTES: Final = "outdoor_stale_minutes"
CONF_SHORT_CYCLE_SECONDS: Final = "short_cycle_seconds"
CONF_STARTUP_GRACE: Final = "startup_grace"
CONF_PERIODIC_AUDIT: Final = "periodic_audit"

# Device semantics verification (design doc section 6.3 / 14). These are
# *user attestations* collected in the config flow. They gate every write to
# the heat source; without them the integration stays in suggest-only mode.
CONF_VERIFY_HEATING_ONLY: Final = "verify_set_hvac_mode_heating_only"
CONF_VERIFY_SUPPLY_TEMP: Final = "verify_set_temperature_is_supply"
CONF_VERIFY_MIN_FLOW: Final = "verify_min_flow_hydraulics"

# ---------------------------------------------------------------------------
# Control modes (system level)
# ---------------------------------------------------------------------------
CONTROL_MODE_OBSERVE: Final = "observe"
CONTROL_MODE_COORDINATE: Final = "coordinate"
CONTROL_MODE_PAUSED: Final = "paused"

CONTROL_MODES: Final = [
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_PAUSED,
]

# ---------------------------------------------------------------------------
# Zone modes (per room)
# ---------------------------------------------------------------------------
ZONE_MODE_AUTO: Final = "auto"
ZONE_MODE_COMFORT: Final = "comfort"
ZONE_MODE_ECO: Final = "eco"
ZONE_MODE_AWAY: Final = "away"
ZONE_MODE_MANUAL: Final = "manual"

ZONE_MODES: Final = [
    ZONE_MODE_AUTO,
    ZONE_MODE_COMFORT,
    ZONE_MODE_ECO,
    ZONE_MODE_AWAY,
    ZONE_MODE_MANUAL,
]

# ---------------------------------------------------------------------------
# Manual override restore policies
# ---------------------------------------------------------------------------
OVERRIDE_NEXT_SCHEDULE: Final = "next_schedule"
OVERRIDE_FIXED_DURATION: Final = "fixed_duration"
OVERRIDE_INDEFINITE: Final = "indefinite"

OVERRIDE_POLICIES: Final = [
    OVERRIDE_NEXT_SCHEDULE,
    OVERRIDE_FIXED_DURATION,
    OVERRIDE_INDEFINITE,
]

DEFAULT_OVERRIDE_MINUTES: Final = 120

# ---------------------------------------------------------------------------
# Control sources (who owns the effective setpoint)
# ---------------------------------------------------------------------------
SOURCE_MANUAL: Final = "manual"
SOURCE_FIXED_MODE: Final = "fixed_mode"
SOURCE_SCHEDULE: Final = "schedule"
SOURCE_COLD_ADJUSTMENT: Final = "cold_adjustment"
SOURCE_AUX_LOAD: Final = "aux_load"
SOURCE_FALLBACK: Final = "fallback"

# ---------------------------------------------------------------------------
# Reason codes - every write decision must be explainable (design doc 9.1)
# ---------------------------------------------------------------------------
REASON_MANUAL_OVERRIDE: Final = "manual_override"
REASON_SCHEDULE_TRANSITION: Final = "schedule_transition"
REASON_COLD_ADJUSTMENT: Final = "cold_adjustment"
REASON_AUX_LOAD: Final = "aux_load"
REASON_AUX_LOAD_EXIT: Final = "aux_load_exit"
REASON_RANGE_CLAMP: Final = "range_clamp"
REASON_TOLERANCE: Final = "within_tolerance"
REASON_THROTTLED: Final = "write_throttled"
REASON_NO_PENDING_ECHO: Final = "no_pending_command"
REASON_BOILER_MIN_FLOW_UNVERIFIED: Final = "boiler_min_flow_unverified"
REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED: Final = "heat_source_semantics_unverified"
REASON_OBSERVE_MODE: Final = "observe_mode"
REASON_CONTROL_PAUSED: Final = "control_paused"
REASON_ENTITY_UNAVAILABLE: Final = "entity_unavailable"
REASON_WEATHER_STALE: Final = "weather_data_stale"
REASON_LATE_ECHO: Final = "late_echo_superseded"
REASON_DEMAND_DEBOUNCE: Final = "demand_debounce"
REASON_MIN_ON_TIME: Final = "minimum_on_time"
REASON_MIN_OFF_TIME: Final = "minimum_off_time"
REASON_VALVE_PREOPEN: Final = "valve_preopen_wait"
REASON_PUMP_OVERRUN: Final = "pump_overrun"
REASON_RECOVERY_GRACE: Final = "recovery_grace_period"
REASON_DEGRADED: Final = "communication_degraded"

# ---------------------------------------------------------------------------
# Feedback quality (design doc 4.2 / 4.6)
# ---------------------------------------------------------------------------
FEEDBACK_VALID: Final = "valid"
FEEDBACK_STALE: Final = "stale"
FEEDBACK_UNCERTAIN: Final = "uncertain"
FEEDBACK_UNAVAILABLE: Final = "unavailable"

# ---------------------------------------------------------------------------
# Heating FSM states (design doc 6.2)
# ---------------------------------------------------------------------------
FSM_OBSERVE: Final = "observe"
FSM_IDLE: Final = "idle"
FSM_PREPARE: Final = "prepare"
FSM_HEATING: Final = "heating"
FSM_SATISFIED: Final = "satisfied"
FSM_DEGRADED: Final = "degraded"

FSM_STATES: Final = [
    FSM_OBSERVE,
    FSM_IDLE,
    FSM_PREPARE,
    FSM_HEATING,
    FSM_SATISFIED,
    FSM_DEGRADED,
]

# Heat source decisions
DECISION_NONE: Final = "none"
DECISION_REQUEST_HEAT: Final = "request_heat"
DECISION_REVOKE_HEAT: Final = "revoke_heat"
DECISION_SET_SUPPLY: Final = "set_supply_temperature"
DECISION_DEGRADED: Final = "degraded"

# ---------------------------------------------------------------------------
# V0.2 - zone-combination analysis
# ---------------------------------------------------------------------------
#: How long one combination of simultaneously-demanding zones must persist
#: before it is treated as a distinct observation rather than a transition.
DEFAULT_COMBINATION_MIN_DWELL: Final = 120.0
#: Combinations shorter than this are noise (a zone flickering) and are folded
#: into the neighbouring observation instead of being counted separately.
DEFAULT_COMBINATION_MIN_DURATION: Final = 60.0
#: Retention for combination history, matching the burner window.
DEFAULT_COMBINATION_WINDOW: Final = 24 * 3600.0
#: A combination is "dominant" when it accounts for at least this share of
#: heating time in the window.
DEFAULT_COMBINATION_DOMINANT_SHARE: Final = 0.30
#: Combustion quality grading thresholds (design doc 8 / V0.2).
DEFAULT_CYCLING_GOOD_SECONDS: Final = 600.0
DEFAULT_CYCLING_POOR_SECONDS: Final = 300.0

#: Sentinel for "no zone was demanding heat" in combination keys and reports.
COMBINATION_NONE: Final = "none"

#: Combustion event quality grades.
QUALITY_GOOD: Final = "good"
QUALITY_FAIR: Final = "fair"
QUALITY_POOR: Final = "poor"
QUALITY_UNKNOWN: Final = "unknown"

#: Hydraulic circuits a burner-state sensor can watch (see CONF_BURNER_CIRCUIT).
CIRCUIT_HEATING: Final = "heating"
CIRCUIT_DHW: Final = "dhw"

CIRCUITS: Final = [CIRCUIT_HEATING, CIRCUIT_DHW]

#: Why a burn happened (V0.2 correlation).
BURN_CAUSE_DEMAND: Final = "demand"
BURN_CAUSE_DHW: Final = "dhw"
BURN_CAUSE_UNEXPLAINED: Final = "unexplained"
BURN_CAUSE_UNKNOWN: Final = "unknown"

# ---------------------------------------------------------------------------
# V0.2 - comfort diagnostics (over/under temperature)
# ---------------------------------------------------------------------------
#: A room is "overheating" when the measured temperature exceeds the effective
#: target by at least this much for at least the dwell time below.
DEFAULT_OVERHEAT_DELTA: Final = 1.0
DEFAULT_OVERHEAT_DWELL: Final = 1800.0
#: A room is "underheating" when it stays this far *below* target while the
#: zone is genuinely demanding heat - that is a capacity or hydraulics symptom,
#: not a setpoint mistake.
DEFAULT_UNDERHEAT_DELTA: Final = 1.5
DEFAULT_UNDERHEAT_DWELL: Final = 3600.0
#: Retention for comfort samples.
DEFAULT_COMFORT_WINDOW: Final = 24 * 3600.0
#: Minimum spacing between recorded comfort samples, to bound memory.
DEFAULT_COMFORT_SAMPLE_INTERVAL: Final = 300.0

#: Comfort verdicts.
COMFORT_OK: Final = "ok"
COMFORT_OVERHEATING: Final = "overheating"
COMFORT_UNDERHEATING: Final = "underheating"
COMFORT_UNKNOWN: Final = "unknown"

#: Actionable advice codes emitted by the comfort analyser.
ADVICE_LOWER_SUPPLY: Final = "lower_supply_temperature"
ADVICE_RAISE_SUPPLY: Final = "raise_supply_temperature"
ADVICE_CHECK_HYDRAULICS: Final = "check_hydraulics_or_balancing"
ADVICE_CHECK_SCHEDULE: Final = "check_schedule_or_override"
ADVICE_NONE: Final = "none"

#: Reason code for "the room sits below its target but nothing is calling for
#: heat". This is deliberately NOT a comfort fault: the control loop has no
#: setpoint it could change to fix it, so the verdict stays OK while the advice
#: points at the hardware. The deviation is still reported, so the room is never
#: presented as warm.
REASON_BELOW_TARGET_WITHOUT_DEMAND: Final = "below_target_without_demand"

# ---------------------------------------------------------------------------
# HVAC actions / modes as reported by climate entities
# ---------------------------------------------------------------------------
HVAC_ACTION_HEATING: Final = "heating"
HVAC_ACTION_IDLE: Final = "idle"
HVAC_ACTION_OFF: Final = "off"

HVAC_MODE_HEAT: Final = "heat"
HVAC_MODE_OFF: Final = "off"

STATE_UNAVAILABLE: Final = "unavailable"
STATE_UNKNOWN: Final = "unknown"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_TARGET_STEP: Final = 0.5
DEFAULT_MIN_TEMP: Final = 16.0
DEFAULT_MAX_TEMP: Final = 26.0
DEFAULT_ZONE_TOLERANCE: Final = 0.2
DEFAULT_WRITE_MIN_INTERVAL: Final = 60  # seconds, per zone
DEFAULT_WRITE_RETRY_MAX: Final = 3
DEFAULT_WRITE_RETRY_BACKOFF: Final = 15.0  # seconds, multiplied by attempt
DEFAULT_PENDING_TIMEOUT: Final = 90.0  # seconds to wait for an echo
#: Extra grace after a *failed* write during which its echo is still recognised
#: as ours. Bounded on purpose: after this, the same value is treated as
#: ambiguous and the user's possible manual entry is protected (design doc 4.4).
DEFAULT_ECHO_GRACE: Final = 300.0

# Heat source defaults
DEFAULT_DEMAND_ON_DEBOUNCE: Final = 60.0
DEFAULT_DEMAND_OFF_DEBOUNCE: Final = 300.0
DEFAULT_VALVE_PREOPEN_WAIT: Final = 180.0
DEFAULT_MIN_ON_TIME: Final = 300.0
DEFAULT_MIN_OFF_TIME: Final = 300.0
DEFAULT_PUMP_OVERRUN: Final = 180.0
DEFAULT_SUPPLY_HYSTERESIS: Final = 1.0
DEFAULT_SUPPLY_MIN_INTERVAL: Final = 1800.0

# Weather compensation defaults (design doc 7)
DEFAULT_OUTDOOR_DESIGN: Final = 0.0
DEFAULT_OUTDOOR_STOP: Final = 15.0
DEFAULT_SUPPLY_DESIGN: Final = 45.0
DEFAULT_SUPPLY_ECO: Final = 40.0
DEFAULT_SUPPLY_MIN: Final = 35.0
DEFAULT_SUPPLY_MAX: Final = 55.0
DEFAULT_WEATHER_STALE_AFTER: Final = 90.0  # minutes
DEFAULT_WEATHER_HYSTERESIS: Final = 1.0
DEFAULT_WEATHER_MIN_INTERVAL: Final = 30.0  # minutes
WEATHER_MODE_SUGGEST: Final = "suggest"
WEATHER_MODE_APPLY: Final = "apply"

# Auxiliary heat load defaults (design doc 5.3)
DEFAULT_AUX_LOAD_DELTA: Final = 0.5
DEFAULT_AUX_LOAD_MAX_TEMP: Final = 24.0
DEFAULT_AUX_LOAD_HYSTERESIS: Final = 0.3
DEFAULT_AUX_LOAD_MIN_SWITCH_INTERVAL: Final = 1800.0

# Diagnostics
DEFAULT_SHORT_CYCLE_SECONDS: Final = 300.0
DEFAULT_SHORT_CYCLE_WINDOW: Final = 24 * 3600.0

# Recovery
DEFAULT_STARTUP_GRACE: Final = 120.0  # seconds after start before writes resume
DEFAULT_PERIODIC_AUDIT: Final = 900.0  # 15 min consistency check (was the blueprint's polling)

# Storage
STORAGE_VERSION: Final = 1
STORAGE_KEY_PREFIX: Final = f"{DOMAIN}.state"

# Services
SERVICE_RESUME_AUTO: Final = "resume_auto"
SERVICE_SET_SUPPLY_TEMPERATURE: Final = "set_supply_temperature"
SERVICE_REASSESS: Final = "reassess"
