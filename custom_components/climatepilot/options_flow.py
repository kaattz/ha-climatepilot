"""Options flow for ClimatePilot — rooms, strategies, heat source, diagnostics.

The options flow edits the **whole** configuration of the single existing config
entry: the entry data created by the wizard is merged with ``entry.options`` and
every step writes the complete merged mapping back, so the coordinator (which
reads ``{**entry.data, **entry.options}``) always sees a consistent picture.

This module also owns the shared option schema fragments and parsers
(:func:`curve_schema`, :func:`validate_curve`, :func:`default_zone_config`,
:func:`parse_schedule_text`, ...) because the initial wizard needs exactly the
same shapes. Keeping them in one place is what stops the wizard and the options
editor from drifting apart — ``config_flow`` imports them from here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import time as dt_time
from typing import Any, Final

import voluptuous as vol
from homeassistant.components.climate.const import ATTR_HVAC_MODES
from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import (
    CONF_BOILER_ENTITY,
    CONF_BURNER_STATE_ENTITY,
    CONF_CONDENSING_RETURN_C,
    CONF_CONTROL_MODE,
    CONF_DELTA_T_HEALTHY_MAX,
    CONF_DELTA_T_HEALTHY_MIN,
    CONF_DELTA_T_WINDOW_SECONDS,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PERIODIC_AUDIT,
    CONF_PROBE_FROZEN_SECONDS,
    CONF_PROBE_MAX_DIVERGENCE,
    CONF_PROBE_STALE_SECONDS,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SHORT_CYCLE_SECONDS,
    CONF_STARTUP_GRACE,
    CONF_SUPPLY_PROBE_ENTITY,
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_MIN_FLOW,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_WEATHER_CURVE,
    CONF_WEATHER_MODE,
    CONF_ZONE_ACTION_ENTITY,
    CONF_ZONE_AUX_LOAD,
    CONF_ZONE_COLD_ADJUSTMENT,
    CONF_ZONE_ENTITY,
    CONF_ZONE_ID,
    CONF_ZONE_MAX_TEMP,
    CONF_ZONE_MIN_TEMP,
    CONF_ZONE_MODE,
    CONF_ZONE_MODES,
    CONF_ZONE_NAME,
    CONF_ZONE_OVERRIDE_MINUTES,
    CONF_ZONE_OVERRIDE_POLICY,
    CONF_ZONE_SCHEDULE,
    CONF_ZONE_SLOTS,
    CONF_ZONE_STEP,
    CONF_ZONE_TOLERANCE,
    CONF_ZONES,
    CONTROL_MODES,
    DEFAULT_CONDENSING_RETURN_C,
    DEFAULT_DELTA_T_HEALTHY_MAX,
    DEFAULT_DELTA_T_HEALTHY_MIN,
    DEFAULT_DELTA_T_WINDOW,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_OVERRIDE_MINUTES,
    DEFAULT_PERIODIC_AUDIT,
    DEFAULT_PROBE_FROZEN_AFTER,
    DEFAULT_PROBE_MAX_DIVERGENCE,
    DEFAULT_PROBE_STALE_AFTER,
    DEFAULT_SHORT_CYCLE_SECONDS,
    DEFAULT_STARTUP_GRACE,
    DEFAULT_TARGET_STEP,
    DEFAULT_WEATHER_STALE_AFTER,
    DEFAULT_ZONE_TOLERANCE,
    OVERRIDE_NEXT_SCHEDULE,
    OVERRIDE_POLICIES,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    WEATHER_MODE_APPLY,
    WEATHER_MODE_SUGGEST,
    ZONE_MODE_AUTO,
    ZONE_MODES,
)
from .models import DEFAULT_SLOTS, Schedule, ScheduleSlot, WeatherCurve
from .temperature_arbiter import FIXED_MODE_FALLBACK

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Step ids — the keys of ``options.step`` in strings.json and the values of the
# two menus. ``tests/test_translations_sync.py`` asserts that this set and the
# ``async_step_*`` methods of ClimatePilotOptionsFlow stay equal.
# ---------------------------------------------------------------------------
STEP_INIT: Final = "init"
STEP_ZONES: Final = "zones"
STEP_STRATEGIES: Final = "strategies"
STEP_ZONE_MENU: Final = "zone_menu"
STEP_ZONE_DETAIL: Final = "zone_detail"
STEP_SCHEDULE: Final = "schedule"
STEP_HEAT_SOURCE: Final = "heat_source"
STEP_PROBES: Final = "probes"
STEP_DIAGNOSTICS: Final = "diagnostics"

MENU_OPTIONS: Final = [
    STEP_ZONES,
    STEP_STRATEGIES,
    STEP_HEAT_SOURCE,
    STEP_PROBES,
    STEP_DIAGNOSTICS,
]
ZONE_MENU_OPTIONS: Final = [STEP_ZONE_DETAIL, STEP_SCHEDULE]

# ---------------------------------------------------------------------------
# Weather-curve field keys.
#
# ``const.py`` deliberately carries no per-field keys for the curve: the shape
# is owned by ``models.WeatherCurve`` (frozen). These aliases keep the schema
# readable, and ``test_translations_sync`` asserts every one of them is a real
# ``WeatherCurve`` field so the two cannot drift apart.
# ---------------------------------------------------------------------------
CONF_CURVE_OUTDOOR_DESIGN: Final = "outdoor_design"
CONF_CURVE_OUTDOOR_STOP: Final = "outdoor_stop"
CONF_CURVE_SUPPLY_DESIGN: Final = "supply_design"
CONF_CURVE_SUPPLY_ECO: Final = "supply_eco"
CONF_CURVE_SUPPLY_MIN: Final = "supply_min"
CONF_CURVE_SUPPLY_MAX: Final = "supply_max"

CURVE_FIELD_KEYS: Final = (
    CONF_CURVE_OUTDOOR_DESIGN,
    CONF_CURVE_OUTDOOR_STOP,
    CONF_CURVE_SUPPLY_DESIGN,
    CONF_CURVE_SUPPLY_ECO,
    CONF_CURVE_SUPPLY_MIN,
    CONF_CURVE_SUPPLY_MAX,
)

VERIFY_KEYS: Final = (
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_VERIFY_MIN_FLOW,
)

#: Sensor roles collected next to the boiler entity (all optional). The V0.3
#: strap-on supply probe belongs here too: both flows persist exactly this tuple,
#: and ``test_translations_sync`` derives the expected label set from it, so a new
#: role cannot be added to the form without its translation following.
#:
#: Every role listed here is consumed at runtime. ``CONF_SUPPLY_TEMP_ENTITY``
#: used to be in this tuple and was removed: it was collected and persisted but
#: nothing ever read it, because the supply temperature comes from the boiler
#: climate entity's own ``temperature`` attribute. A field that accepts a value
#: and silently ignores it invites the user to believe they configured something.
BOILER_SENSOR_KEYS: Final = (
    CONF_BURNER_STATE_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SUPPLY_PROBE_ENTITY,
)

#: The subset of :data:`BOILER_SENSOR_KEYS` that is a temperature reading, and
#: therefore gets a ``device_class: temperature`` selector rather than a generic
#: sensor one. Derived, never restated, so the two cannot drift.
BOILER_TEMPERATURE_KEYS: Final = tuple(
    key for key in BOILER_SENSOR_KEYS if key != CONF_BURNER_STATE_ENTITY
)

#: The V0.3 external strap-on probe pair (docs/INTERFACES_V0.3.md section 4.5).
#: Both are optional; an installation without them is fully supported and simply
#: reports ``unknown`` for the probe entities. ``CONF_RETURN_TEMP_ENTITY`` is the
#: same physical probe the ΔT engine reads, so it is listed once, here and in
#: ``BOILER_SENSOR_KEYS``.
PROBE_ENTITY_KEYS: Final = (
    CONF_SUPPLY_PROBE_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
)

#: Every tunable V0.3 probe/delta-T threshold, in form order.
PROBE_THRESHOLD_KEYS: Final = (
    CONF_PROBE_STALE_SECONDS,
    CONF_PROBE_FROZEN_SECONDS,
    CONF_DELTA_T_WINDOW_SECONDS,
    CONF_CONDENSING_RETURN_C,
    CONF_DELTA_T_HEALTHY_MIN,
    CONF_DELTA_T_HEALTHY_MAX,
    CONF_PROBE_MAX_DIVERGENCE,
)

# Selector bounds. These are *input* guards only: the real device limits are
# applied by the climate adapter, and a zone's own range is intersected with
# what its thermostat reports.
ZONE_TEMP_MIN: Final = 5.0
ZONE_TEMP_MAX: Final = 35.0
ZONE_STEP_LOW: Final = 0.1
ZONE_STEP_HIGH: Final = 2.0
TOLERANCE_LOW: Final = 0.0
TOLERANCE_HIGH: Final = 2.0
OVERRIDE_MINUTES_MAX: Final = 1440
CURVE_OUTDOOR_LOW: Final = -40.0
CURVE_OUTDOOR_HIGH: Final = 40.0
CURVE_SUPPLY_LOW: Final = 10.0
CURVE_SUPPLY_HIGH: Final = 90.0
STALE_MINUTES_LOW: Final = 10
STALE_MINUTES_HIGH: Final = 1440
SHORT_CYCLE_LOW: Final = 10
SHORT_CYCLE_HIGH: Final = 3600
GRACE_LOW: Final = 0
GRACE_HIGH: Final = 3600
AUDIT_LOW: Final = 60
AUDIT_HIGH: Final = 86400

# V0.3 probe / delta-T thresholds. Same contract as the bounds above: these are
# *input* guards so the form cannot store a nonsense value; the engines remain
# the authority on how the stored value is used.
PROBE_STALE_LOW: Final = 60
PROBE_STALE_HIGH: Final = 86400
PROBE_FROZEN_LOW: Final = 60
PROBE_FROZEN_HIGH: Final = 86400
DELTA_T_WINDOW_LOW: Final = 300
DELTA_T_WINDOW_HIGH: Final = 86400
CONDENSING_RETURN_LOW: Final = 20.0
CONDENSING_RETURN_HIGH: Final = 70.0
DELTA_T_LOW: Final = -10.0
DELTA_T_HIGH: Final = 40.0
DIVERGENCE_LOW: Final = 0.5
DIVERGENCE_HIGH: Final = 30.0

#: ``HH:MM=温度``. The separator class deliberately accepts the full-width
#: equals sign a Chinese IME produces as well as ASCII ``=``; a decimal comma is
#: accepted for the same reason. Both are load-bearing, hence the noqa.
_SCHEDULE_ITEM = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*[=＝]\s*(?P<setpoint>-?\d+(?:[.,]\d+)?)$"  # noqa: RUF001
)
_SCHEDULE_SPLIT = re.compile(r"[\n/;]+")


class ScheduleFormatError(ValueError):
    """Raised when the free-text schedule cannot be parsed."""


# ---------------------------------------------------------------------------
# Selector factories
# ---------------------------------------------------------------------------
def climate_selector(*, multiple: bool = False) -> selector.EntitySelector:
    """An entity selector restricted to ``climate`` entities."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="climate", multiple=multiple)
    )


def sensor_selector(*, device_class: str | None = None) -> selector.EntitySelector:
    """An entity selector restricted to sensors of one device class."""
    config = selector.EntitySelectorConfig(domain="sensor")
    if device_class:
        config["device_class"] = device_class
    return selector.EntitySelector(config)


def control_mode_selector() -> selector.SelectSelector:
    """The system control-mode dropdown."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=CONTROL_MODES,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=CONF_CONTROL_MODE,
        )
    )


def weather_mode_selector() -> selector.SelectSelector:
    """The weather-compensation suggest/apply dropdown."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[WEATHER_MODE_SUGGEST, WEATHER_MODE_APPLY],
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=CONF_WEATHER_MODE,
        )
    )


def zone_mode_selector() -> selector.SelectSelector:
    """The per-room mode dropdown."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=ZONE_MODES,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=CONF_ZONE_MODE,
        )
    )


def override_policy_selector() -> selector.SelectSelector:
    """The manual-override restore policy dropdown."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=OVERRIDE_POLICIES,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key=CONF_ZONE_OVERRIDE_POLICY,
        )
    )


def number_field(
    key: str,
    current: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None = None,
    mode: selector.NumberSelectorMode = selector.NumberSelectorMode.BOX,
) -> tuple[vol.Marker, selector.NumberSelector]:
    """Build a number field that shows the stored value and falls back to a default."""
    value = _as_float(current)
    if value is None:
        value = default
    config = selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=mode)
    if unit:
        config["unit_of_measurement"] = unit
    return (
        vol.Required(key, default=value, description={"suggested_value": value}),
        selector.NumberSelector(config),
    )


def _optional_entity_field(
    key: str, current: Any, entity_selector: selector.EntitySelector
) -> tuple[vol.Marker, Any]:
    """Build an optional entity field; blank input becomes ``None``."""
    kwargs: dict[str, Any] = {}
    if current:
        kwargs["default"] = current
        kwargs["description"] = {"suggested_value": current}
    return vol.Optional(key, **kwargs), vol.Any(None, entity_selector)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Entity validation helpers
# ---------------------------------------------------------------------------
def error_key_for_climate(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return an error key when ``entity_id`` cannot be used as a heat source."""
    state = hass.states.get(entity_id)
    if state is None:
        return "boiler_not_found"
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return "boiler_unavailable"
    if not state.attributes.get(ATTR_HVAC_MODES):
        # Without hvac_modes there is no way to know whether "off" is safe to
        # send, so the entity must not be accepted as a controllable heat
        # source (design doc 6.3 / 14).
        return "no_hvac_modes"
    return None


def zone_selection_errors(
    hass: HomeAssistant, entity_ids: list[str], boiler_entity: str | None
) -> dict[str, str]:
    """Validate a room selection, returning ``{field: error_key}``."""
    if len(set(entity_ids)) != len(entity_ids):
        return {CONF_ZONES: "zone_duplicate"}
    if boiler_entity and boiler_entity in entity_ids:
        return {CONF_ZONES: "zone_is_boiler"}
    for entity_id in entity_ids:
        if hass.states.get(entity_id) is None:
            return {CONF_ZONES: "zone_not_found"}
    return {}


# ---------------------------------------------------------------------------
# Per-zone configuration
# ---------------------------------------------------------------------------
def zone_id_from_entity(entity_id: str) -> str:
    """Derive a stable zone id from a climate entity id."""
    object_id = entity_id.split(".", 1)[-1] or entity_id
    cleaned = re.sub(r"[^a-z0-9_]+", "_", object_id.lower()).strip("_")
    return cleaned or "zone"


def _unique_zone_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def default_zone_config(hass: HomeAssistant, entity_id: str, taken_ids: set[str]) -> dict[str, Any]:
    """Build the initial per-zone options block for ``entity_id``.

    The shape is the contract the coordinator parses (``_build_zones`` /
    ``Schedule.from_dict``); keep it exactly as documented in
    docs/INTERFACES.md and echoed by the Lead's interface note.
    """
    state = hass.states.get(entity_id)
    attributes: Mapping[str, Any] = state.attributes if state is not None else {}

    zone_id = _unique_zone_id(zone_id_from_entity(entity_id), taken_ids)
    name = str(attributes.get("friendly_name") or "").strip()
    if not name:
        name = zone_id.replace("_", " ").title()

    step = _as_float(attributes.get("target_temp_step"))
    if step is None or step <= 0:
        step = DEFAULT_TARGET_STEP

    return {
        CONF_ZONE_ID: zone_id,
        CONF_ZONE_ENTITY: entity_id,
        CONF_ZONE_NAME: name,
        CONF_ZONE_MODE: ZONE_MODE_AUTO,
        CONF_ZONE_MIN_TEMP: float(DEFAULT_MIN_TEMP),
        CONF_ZONE_MAX_TEMP: float(DEFAULT_MAX_TEMP),
        CONF_ZONE_STEP: float(step),
        CONF_ZONE_TOLERANCE: float(DEFAULT_ZONE_TOLERANCE),
        CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_NEXT_SCHEDULE,
        CONF_ZONE_OVERRIDE_MINUTES: int(DEFAULT_OVERRIDE_MINUTES),
        CONF_ZONE_COLD_ADJUSTMENT: False,
        CONF_ZONE_AUX_LOAD: False,
        CONF_ZONE_ACTION_ENTITY: None,
        CONF_ZONE_SCHEDULE: Schedule(slots=DEFAULT_SLOTS).to_dict(),
        CONF_ZONE_MODES: dict(FIXED_MODE_FALLBACK),
    }


def zone_entries(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the mutable zone dicts of a merged configuration."""
    raw = config.get(CONF_ZONES) or []
    return [zone for zone in raw if isinstance(zone, dict)]


def find_zone(config: Mapping[str, Any], zone_id: str) -> dict[str, Any] | None:
    """Return the mutable options dict of ``zone_id`` (or ``None``)."""
    for zone in zone_entries(config):
        if str(zone.get(CONF_ZONE_ID) or "") == zone_id:
            return zone
    return None


def zone_entity_ids(config: Mapping[str, Any]) -> list[str]:
    """Return the configured climate entity ids, in order."""
    return [
        str(zone[CONF_ZONE_ENTITY]) for zone in zone_entries(config) if zone.get(CONF_ZONE_ENTITY)
    ]


def zone_choices(config: Mapping[str, Any]) -> list[selector.SelectOptionDict]:
    """Return ``SelectSelector`` options, one per configured room."""
    return [
        selector.SelectOptionDict(
            value=str(zone.get(CONF_ZONE_ID) or ""),
            label=str(zone.get(CONF_ZONE_NAME) or zone.get(CONF_ZONE_ID) or ""),
        )
        for zone in zone_entries(config)
        if zone.get(CONF_ZONE_ID)
    ]


# ---------------------------------------------------------------------------
# Schedule text <-> models.Schedule
# ---------------------------------------------------------------------------
def parse_schedule_text(text: str | None) -> tuple[ScheduleSlot, ...]:
    """Parse ``HH:MM=温度`` items separated by ``/``, ``;`` or newlines.

    Returns the slots sorted by time; on a repeated time the last entry wins.
    Empty text resets to :data:`models.DEFAULT_SLOTS`, because
    ``Schedule.from_dict`` treats "no slots" as "use the default schedule" — the
    flow must not write a shape the model would silently reinterpret.

    Raises:
        ScheduleFormatError: when an item is malformed or its time is invalid.
    """
    raw = (text or "").strip()
    if not raw:
        return DEFAULT_SLOTS

    parsed: dict[dt_time, float] = {}
    for item in _SCHEDULE_SPLIT.split(raw):
        item = item.strip()
        if not item:
            continue
        match = _SCHEDULE_ITEM.match(item)
        if match is None:
            raise ScheduleFormatError(item)
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise ScheduleFormatError(item)
        parsed[dt_time(hour, minute)] = float(match.group("setpoint").replace(",", "."))

    if not parsed:
        raise ScheduleFormatError(raw)

    return tuple(ScheduleSlot(at=at, setpoint=parsed[at]) for at in sorted(parsed))


def schedule_config(slots: tuple[ScheduleSlot, ...]) -> dict[str, Any]:
    """Wrap slots in the ``CONF_ZONE_SCHEDULE`` shape (``{"slots": [...]}``)."""
    return Schedule(slots=tuple(slots)).to_dict()


def format_schedule_text(slots: tuple[ScheduleSlot, ...]) -> str:
    """Render slots back into editable ``HH:MM=温度`` text."""
    return " / ".join(f"{slot.at.strftime('%H:%M')}={slot.setpoint:g}" for slot in slots)


def zone_schedule_text(zone: Mapping[str, Any]) -> str:
    """Return the editable schedule text of one zone options block."""
    schedule = Schedule.from_dict(zone.get(CONF_ZONE_SCHEDULE))
    return format_schedule_text(schedule.sorted_slots())


# ---------------------------------------------------------------------------
# Weather curve
# ---------------------------------------------------------------------------
def curve_defaults(config: Mapping[str, Any]) -> dict[str, float]:
    """Return the current curve as a plain dict (model defaults when unset)."""
    return WeatherCurve.from_dict(config.get(CONF_WEATHER_CURVE)).to_dict()


def curve_schema(config: Mapping[str, Any]) -> vol.Schema:
    """Number fields for the linear weather-compensation curve."""
    curve = curve_defaults(config)
    outdoor = {"minimum": CURVE_OUTDOOR_LOW, "maximum": CURVE_OUTDOOR_HIGH, "step": 0.5}
    supply = {"minimum": CURVE_SUPPLY_LOW, "maximum": CURVE_SUPPLY_HIGH, "step": 0.5}
    fields: dict[Any, Any] = {}
    for key, bounds, mode in (
        (CONF_CURVE_OUTDOOR_DESIGN, outdoor, selector.NumberSelectorMode.BOX),
        (CONF_CURVE_OUTDOOR_STOP, outdoor, selector.NumberSelectorMode.BOX),
        (CONF_CURVE_SUPPLY_DESIGN, supply, selector.NumberSelectorMode.SLIDER),
        (CONF_CURVE_SUPPLY_ECO, supply, selector.NumberSelectorMode.SLIDER),
        (CONF_CURVE_SUPPLY_MIN, supply, selector.NumberSelectorMode.SLIDER),
        (CONF_CURVE_SUPPLY_MAX, supply, selector.NumberSelectorMode.SLIDER),
    ):
        marker, field = number_field(
            key,
            curve.get(key),
            default=float(curve.get(key, 0.0)),
            unit="°C",
            mode=mode,
            **bounds,
        )
        fields[marker] = field
    return vol.Schema(fields)


def curve_from_input(user_input: Mapping[str, Any]) -> dict[str, float]:
    """Extract the curve from a submitted form, keeping model field names."""
    curve = curve_defaults({})
    for key in CURVE_FIELD_KEYS:
        value = _as_float(user_input.get(key))
        if value is not None:
            curve[key] = value
    return curve


def validate_curve(curve: Mapping[str, Any]) -> str | None:
    """Return ``invalid_curve`` when the curve cannot produce a usable slope.

    ``WeatherCurve`` clamps to ``[supply_min, supply_max]``, so a design or eco
    point outside that window would be silently flattened — reject it instead of
    storing a curve that does not mean what the user typed.
    """
    outdoor_design = _as_float(curve.get(CONF_CURVE_OUTDOOR_DESIGN))
    outdoor_stop = _as_float(curve.get(CONF_CURVE_OUTDOOR_STOP))
    supply_design = _as_float(curve.get(CONF_CURVE_SUPPLY_DESIGN))
    supply_eco = _as_float(curve.get(CONF_CURVE_SUPPLY_ECO))
    supply_min = _as_float(curve.get(CONF_CURVE_SUPPLY_MIN))
    supply_max = _as_float(curve.get(CONF_CURVE_SUPPLY_MAX))

    if None in (
        outdoor_design,
        outdoor_stop,
        supply_design,
        supply_eco,
        supply_min,
        supply_max,
    ):
        return "invalid_curve"

    assert outdoor_design is not None and outdoor_stop is not None
    assert supply_design is not None and supply_eco is not None
    assert supply_min is not None and supply_max is not None

    if outdoor_stop <= outdoor_design:
        return "invalid_curve"
    if supply_max <= supply_min:
        return "invalid_curve"
    if not supply_min <= supply_design <= supply_max:
        return "invalid_curve"
    if not supply_min <= supply_eco <= supply_max:
        return "invalid_curve"
    return None


# ---------------------------------------------------------------------------
# Whole-form schemas shared with the config flow
# ---------------------------------------------------------------------------
def boiler_schema(config: Mapping[str, Any]) -> vol.Schema:
    """Heat-source entity plus the optional diagnostic sensors and probes.

    ``CONF_RETURN_TEMP_ENTITY`` appears once, in :data:`PROBE_ENTITY_KEYS`: it is
    the same physical strap-on return probe the ΔT engine reads, so collecting it
    twice under two different roles would let the two disagree.
    """
    boiler = config.get(CONF_BOILER_ENTITY)
    boiler_kwargs: dict[str, Any] = {}
    if boiler:
        boiler_kwargs = {
            "default": boiler,
            "description": {"suggested_value": boiler},
        }
    fields: dict[Any, Any] = {vol.Required(CONF_BOILER_ENTITY, **boiler_kwargs): climate_selector()}

    # The burner sensor is not a temperature reading, so it gets a plain sensor
    # selector; every other role is a °C probe.
    marker, field = _optional_entity_field(
        CONF_BURNER_STATE_ENTITY, config.get(CONF_BURNER_STATE_ENTITY), sensor_selector()
    )
    fields[marker] = field

    for key in BOILER_TEMPERATURE_KEYS:
        marker, field = _optional_entity_field(
            key, config.get(key), sensor_selector(device_class="temperature")
        )
        fields[marker] = field
    return vol.Schema(fields)


def verify_schema(config: Mapping[str, Any]) -> vol.Schema:
    """The three device-semantics attestations (default: not confirmed)."""
    return vol.Schema(
        {
            vol.Required(key, default=bool(config.get(key, False))): selector.BooleanSelector()
            for key in VERIFY_KEYS
        }
    )


def outdoor_schema(config: Mapping[str, Any]) -> vol.Schema:
    """Outdoor temperature source and its staleness threshold."""
    marker, field = _optional_entity_field(
        CONF_OUTDOOR_TEMP_ENTITY,
        config.get(CONF_OUTDOOR_TEMP_ENTITY),
        sensor_selector(device_class="temperature"),
    )
    stale, stale_field = number_field(
        CONF_OUTDOOR_STALE_MINUTES,
        config.get(CONF_OUTDOOR_STALE_MINUTES),
        default=float(DEFAULT_WEATHER_STALE_AFTER),
        minimum=STALE_MINUTES_LOW,
        maximum=STALE_MINUTES_HIGH,
        step=5,
        unit="min",
    )
    return vol.Schema({marker: field, stale: stale_field})


def probes_schema(config: Mapping[str, Any]) -> vol.Schema:
    """The seven V0.3 probe/delta-T thresholds (docs/INTERFACES_V0.3.md 4.5).

    Grouped by what they tune: how long a reading stays believable (stale,
    frozen), then how the ΔT verdicts are drawn (window, condensing return,
    healthy band, pair divergence).
    """
    stale, stale_field = number_field(
        CONF_PROBE_STALE_SECONDS,
        config.get(CONF_PROBE_STALE_SECONDS),
        default=float(DEFAULT_PROBE_STALE_AFTER),
        minimum=PROBE_STALE_LOW,
        maximum=PROBE_STALE_HIGH,
        step=30,
        unit="s",
    )
    frozen, frozen_field = number_field(
        CONF_PROBE_FROZEN_SECONDS,
        config.get(CONF_PROBE_FROZEN_SECONDS),
        default=float(DEFAULT_PROBE_FROZEN_AFTER),
        minimum=PROBE_FROZEN_LOW,
        maximum=PROBE_FROZEN_HIGH,
        step=60,
        unit="s",
    )
    window, window_field = number_field(
        CONF_DELTA_T_WINDOW_SECONDS,
        config.get(CONF_DELTA_T_WINDOW_SECONDS),
        default=float(DEFAULT_DELTA_T_WINDOW),
        minimum=DELTA_T_WINDOW_LOW,
        maximum=DELTA_T_WINDOW_HIGH,
        step=300,
        unit="s",
    )
    condensing, condensing_field = number_field(
        CONF_CONDENSING_RETURN_C,
        config.get(CONF_CONDENSING_RETURN_C),
        default=float(DEFAULT_CONDENSING_RETURN_C),
        minimum=CONDENSING_RETURN_LOW,
        maximum=CONDENSING_RETURN_HIGH,
        step=1,
        unit="°C",
        mode=selector.NumberSelectorMode.SLIDER,
    )
    healthy_min, healthy_min_field = number_field(
        CONF_DELTA_T_HEALTHY_MIN,
        config.get(CONF_DELTA_T_HEALTHY_MIN),
        default=float(DEFAULT_DELTA_T_HEALTHY_MIN),
        minimum=DELTA_T_LOW,
        maximum=DELTA_T_HIGH,
        step=0.5,
        unit="°C",
        mode=selector.NumberSelectorMode.SLIDER,
    )
    healthy_max, healthy_max_field = number_field(
        CONF_DELTA_T_HEALTHY_MAX,
        config.get(CONF_DELTA_T_HEALTHY_MAX),
        default=float(DEFAULT_DELTA_T_HEALTHY_MAX),
        minimum=DELTA_T_LOW,
        maximum=DELTA_T_HIGH,
        step=0.5,
        unit="°C",
        mode=selector.NumberSelectorMode.SLIDER,
    )
    divergence, divergence_field = number_field(
        CONF_PROBE_MAX_DIVERGENCE,
        config.get(CONF_PROBE_MAX_DIVERGENCE),
        default=float(DEFAULT_PROBE_MAX_DIVERGENCE),
        minimum=DIVERGENCE_LOW,
        maximum=DIVERGENCE_HIGH,
        step=0.5,
        unit="°C",
    )
    return vol.Schema(
        {
            stale: stale_field,
            frozen: frozen_field,
            window: window_field,
            condensing: condensing_field,
            healthy_min: healthy_min_field,
            healthy_max: healthy_max_field,
            divergence: divergence_field,
        }
    )


def validate_delta_t_range(config: Mapping[str, Any]) -> str | None:
    """Return ``invalid_delta_t_range`` when the healthy band is inverted.

    ``healthy_min >= healthy_max`` would make every settled ΔT either "low flow"
    or "high flow" and never "ok", so the verdict would be permanently wrong
    rather than merely misconfigured. Rejected at the form, exactly like the
    weather curve's own range check.
    """
    healthy_min = _as_float(config.get(CONF_DELTA_T_HEALTHY_MIN))
    healthy_max = _as_float(config.get(CONF_DELTA_T_HEALTHY_MAX))
    if healthy_min is None or healthy_max is None:
        return "invalid_delta_t_range"
    if healthy_min >= healthy_max:
        return "invalid_delta_t_range"
    return None


def diagnostics_schema(config: Mapping[str, Any]) -> vol.Schema:
    """Short-cycle threshold and recovery timings."""
    short_cycle, short_cycle_field = number_field(
        CONF_SHORT_CYCLE_SECONDS,
        config.get(CONF_SHORT_CYCLE_SECONDS),
        default=float(DEFAULT_SHORT_CYCLE_SECONDS),
        minimum=SHORT_CYCLE_LOW,
        maximum=SHORT_CYCLE_HIGH,
        step=10,
        unit="s",
    )
    grace, grace_field = number_field(
        CONF_STARTUP_GRACE,
        config.get(CONF_STARTUP_GRACE),
        default=float(DEFAULT_STARTUP_GRACE),
        minimum=GRACE_LOW,
        maximum=GRACE_HIGH,
        step=10,
        unit="s",
    )
    audit, audit_field = number_field(
        CONF_PERIODIC_AUDIT,
        config.get(CONF_PERIODIC_AUDIT),
        default=float(DEFAULT_PERIODIC_AUDIT),
        minimum=AUDIT_LOW,
        maximum=AUDIT_HIGH,
        step=60,
        unit="s",
    )
    return vol.Schema({short_cycle: short_cycle_field, grace: grace_field, audit: audit_field})


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------
class ClimatePilotOptionsFlow(OptionsFlowWithReload):
    """Edit rooms, strategies, heat source and diagnostics of the one entry.

    ``OptionsFlowWithReload`` (with the default ``automatic_reload = True``)
    makes Home Assistant reload the entry whenever the saved options actually
    change, so ``__init__.py`` must not register an update listener.
    """

    def __init__(self) -> None:
        """Initialise per-run scratch state (no hass access is possible here)."""
        self._working: dict[str, Any] | None = None
        self._zone_id: str | None = None

    # -- configuration access ------------------------------------------
    @property
    def config(self) -> dict[str, Any]:
        """The merged configuration this flow edits.

        A deepcopy is essential: a step may abort or fail validation, and the
        entry's own mapping must never be mutated as a side effect.
        """
        if self._working is None:
            entry = self.config_entry
            self._working = deepcopy({**dict(entry.data), **dict(entry.options)})
        return self._working

    def _save(self) -> ConfigFlowResult:
        """Persist the whole merged configuration into ``entry.options``."""
        assert self._working is not None
        return self.async_create_entry(title="", data=deepcopy(self._working))

    # -- menu ----------------------------------------------------------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the settings menu."""
        return self.async_show_menu(step_id=STEP_INIT, menu_options=MENU_OPTIONS)

    # -- zones ---------------------------------------------------------
    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add or remove the coordinated room thermostats."""
        config = self.config
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = [e for e in (user_input.get(CONF_ZONES) or []) if e]
            errors = zone_selection_errors(self.hass, selected, config.get(CONF_BOILER_ENTITY))
            if not errors:
                self._replace_zones(selected)
                return self._save()

        current = zone_entity_ids(config)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ZONES,
                    default=current,
                    description={"suggested_value": current},
                ): vol.Any(None, climate_selector(multiple=True))
            }
        )
        return self.async_show_form(step_id=STEP_ZONES, data_schema=schema, errors=errors)

    def _replace_zones(self, selected: list[str]) -> None:
        """Keep the options of surviving rooms and default the new ones."""
        assert self._working is not None
        existing = {
            str(zone[CONF_ZONE_ENTITY]): zone
            for zone in zone_entries(self._working)
            if zone.get(CONF_ZONE_ENTITY)
        }
        taken: set[str] = set()
        zones: list[dict[str, Any]] = []
        for entity_id in selected:
            zone = existing.get(entity_id)
            if zone is None:
                zone = default_zone_config(self.hass, entity_id, taken)
            taken.add(str(zone.get(CONF_ZONE_ID) or ""))
            zones.append(zone)
        self._working[CONF_ZONES] = zones

    # -- strategies ----------------------------------------------------
    async def async_step_strategies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the room whose strategy is edited next."""
        choices = zone_choices(self.config)
        if not choices:
            # Nothing to tune yet — send the user where rooms are added.
            return await self.async_step_zones()

        if user_input is not None:
            self._zone_id = str(user_input[CONF_ZONE_ID])
            return await self.async_step_zone_menu()

        schema = vol.Schema(
            {
                vol.Required(CONF_ZONE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=choices,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id=STEP_STRATEGIES, data_schema=schema)

    async def async_step_zone_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between the strategy fields and the daily schedule."""
        if self._selected_zone() is None:
            return await self.async_step_strategies()
        return self.async_show_menu(step_id=STEP_ZONE_MENU, menu_options=ZONE_MENU_OPTIONS)

    async def async_step_zone_detail(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit name, mode, range, step, tolerance and override policy."""
        zone = self._selected_zone()
        if zone is None:
            return await self.async_step_strategies()

        errors: dict[str, str] = {}
        if user_input is not None:
            minimum = float(user_input[CONF_ZONE_MIN_TEMP])
            maximum = float(user_input[CONF_ZONE_MAX_TEMP])
            if minimum >= maximum:
                errors["base"] = "invalid_range"
            else:
                zone.update(
                    {
                        CONF_ZONE_NAME: str(user_input[CONF_ZONE_NAME]).strip()
                        or str(zone.get(CONF_ZONE_ID)),
                        CONF_ZONE_MODE: str(user_input[CONF_ZONE_MODE]),
                        CONF_ZONE_MIN_TEMP: minimum,
                        CONF_ZONE_MAX_TEMP: maximum,
                        CONF_ZONE_STEP: float(user_input[CONF_ZONE_STEP]),
                        CONF_ZONE_TOLERANCE: float(user_input[CONF_ZONE_TOLERANCE]),
                        CONF_ZONE_OVERRIDE_POLICY: str(user_input[CONF_ZONE_OVERRIDE_POLICY]),
                        CONF_ZONE_OVERRIDE_MINUTES: int(user_input[CONF_ZONE_OVERRIDE_MINUTES]),
                        CONF_ZONE_COLD_ADJUSTMENT: bool(user_input[CONF_ZONE_COLD_ADJUSTMENT]),
                        CONF_ZONE_AUX_LOAD: bool(user_input[CONF_ZONE_AUX_LOAD]),
                    }
                )
                return self._save()

        return self.async_show_form(
            step_id=STEP_ZONE_DETAIL,
            data_schema=self._zone_detail_schema(zone),
            errors=errors,
        )

    def _zone_detail_schema(self, zone: Mapping[str, Any]) -> vol.Schema:
        """Fields of one room's strategy editor."""
        min_temp, min_field = number_field(
            CONF_ZONE_MIN_TEMP,
            zone.get(CONF_ZONE_MIN_TEMP),
            default=float(DEFAULT_MIN_TEMP),
            minimum=ZONE_TEMP_MIN,
            maximum=ZONE_TEMP_MAX,
            step=0.5,
            unit="°C",
            mode=selector.NumberSelectorMode.SLIDER,
        )
        max_temp, max_field = number_field(
            CONF_ZONE_MAX_TEMP,
            zone.get(CONF_ZONE_MAX_TEMP),
            default=float(DEFAULT_MAX_TEMP),
            minimum=ZONE_TEMP_MIN,
            maximum=ZONE_TEMP_MAX,
            step=0.5,
            unit="°C",
            mode=selector.NumberSelectorMode.SLIDER,
        )
        step_key, step_field = number_field(
            CONF_ZONE_STEP,
            zone.get(CONF_ZONE_STEP),
            default=float(DEFAULT_TARGET_STEP),
            minimum=ZONE_STEP_LOW,
            maximum=ZONE_STEP_HIGH,
            step=0.1,
            unit="°C",
        )
        tolerance, tolerance_field = number_field(
            CONF_ZONE_TOLERANCE,
            zone.get(CONF_ZONE_TOLERANCE),
            default=float(DEFAULT_ZONE_TOLERANCE),
            minimum=TOLERANCE_LOW,
            maximum=TOLERANCE_HIGH,
            step=0.05,
            unit="°C",
        )
        minutes, minutes_field = number_field(
            CONF_ZONE_OVERRIDE_MINUTES,
            zone.get(CONF_ZONE_OVERRIDE_MINUTES),
            default=float(DEFAULT_OVERRIDE_MINUTES),
            minimum=0,
            maximum=OVERRIDE_MINUTES_MAX,
            step=5,
            unit="min",
        )
        return vol.Schema(
            {
                vol.Required(
                    CONF_ZONE_NAME,
                    default=str(zone.get(CONF_ZONE_NAME) or ""),
                    description={"suggested_value": zone.get(CONF_ZONE_NAME)},
                ): selector.TextSelector(),
                vol.Required(
                    CONF_ZONE_MODE,
                    default=str(zone.get(CONF_ZONE_MODE) or ZONE_MODE_AUTO),
                ): zone_mode_selector(),
                min_temp: min_field,
                max_temp: max_field,
                step_key: step_field,
                tolerance: tolerance_field,
                vol.Required(
                    CONF_ZONE_OVERRIDE_POLICY,
                    default=str(zone.get(CONF_ZONE_OVERRIDE_POLICY) or OVERRIDE_NEXT_SCHEDULE),
                ): override_policy_selector(),
                minutes: minutes_field,
                vol.Required(
                    CONF_ZONE_COLD_ADJUSTMENT,
                    default=bool(zone.get(CONF_ZONE_COLD_ADJUSTMENT, False)),
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_ZONE_AUX_LOAD,
                    default=bool(zone.get(CONF_ZONE_AUX_LOAD, False)),
                ): selector.BooleanSelector(),
            }
        )

    # -- schedule ------------------------------------------------------
    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the daily schedule as ``HH:MM=温度`` text."""
        zone = self._selected_zone()
        if zone is None:
            return await self.async_step_strategies()

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                slots = parse_schedule_text(user_input.get(CONF_ZONE_SLOTS))
            except ScheduleFormatError as err:
                _LOGGER.debug("climatepilot: rejected schedule item %r", err)
                errors[CONF_ZONE_SLOTS] = "invalid_schedule"
            else:
                zone[CONF_ZONE_SCHEDULE] = schedule_config(slots)
                return self._save()

        current = zone_schedule_text(zone)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ZONE_SLOTS,
                    default=current,
                    description={"suggested_value": current},
                ): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.TEXT)
                )
            }
        )
        return self.async_show_form(step_id=STEP_SCHEDULE, data_schema=schema, errors=errors)

    def _selected_zone(self) -> dict[str, Any] | None:
        """Return the room chosen in the strategies step, if it still exists."""
        if self._zone_id is None:
            return None
        return find_zone(self.config, self._zone_id)

    # -- heat source ---------------------------------------------------
    async def async_step_heat_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the boiler, its sensors, the curve and the attestations."""
        config = self.config
        errors: dict[str, str] = {}

        if user_input is not None:
            boiler = user_input[CONF_BOILER_ENTITY]
            boiler_error = error_key_for_climate(self.hass, boiler)
            curve = curve_from_input(user_input)
            curve_error = validate_curve(curve)
            if boiler_error:
                errors[CONF_BOILER_ENTITY] = boiler_error
            if curve_error:
                errors["base"] = curve_error

            outdoor = user_input.get(CONF_OUTDOOR_TEMP_ENTITY) or None
            if outdoor is not None and self.hass.states.get(outdoor) is None:
                errors[CONF_OUTDOOR_TEMP_ENTITY] = "outdoor_not_found"

            if not errors:
                config[CONF_BOILER_ENTITY] = boiler
                for key in BOILER_SENSOR_KEYS:
                    config[key] = user_input.get(key) or None
                config[CONF_OUTDOOR_TEMP_ENTITY] = outdoor
                config[CONF_OUTDOOR_STALE_MINUTES] = float(user_input[CONF_OUTDOOR_STALE_MINUTES])
                config[CONF_WEATHER_MODE] = str(user_input[CONF_WEATHER_MODE])
                config[CONF_WEATHER_CURVE] = curve
                for key in VERIFY_KEYS:
                    config[key] = bool(user_input.get(key, False))
                return self._save()

        schema = vol.Schema(
            {
                **boiler_schema(config).schema,
                **outdoor_schema(config).schema,
                vol.Required(
                    CONF_WEATHER_MODE,
                    default=str(config.get(CONF_WEATHER_MODE) or WEATHER_MODE_SUGGEST),
                ): weather_mode_selector(),
                **curve_schema(config).schema,
                **verify_schema(config).schema,
            }
        )
        return self.async_show_form(step_id=STEP_HEAT_SOURCE, data_schema=schema, errors=errors)

    # -- probes --------------------------------------------------------
    async def async_step_probes(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the V0.3 probe and delta-T thresholds."""
        config = self.config

        if user_input is not None:
            candidate = {key: float(user_input[key]) for key in PROBE_THRESHOLD_KEYS}
            error = validate_delta_t_range(candidate)
            if error:
                # Re-render with the submitted values still selected, so the user
                # can see which pair was rejected instead of retyping everything.
                return self.async_show_form(
                    step_id=STEP_PROBES,
                    data_schema=probes_schema({**config, **candidate}),
                    errors={"base": error},
                )
            config.update(candidate)
            return self._save()

        return self.async_show_form(step_id=STEP_PROBES, data_schema=probes_schema(config))

    # -- diagnostics ---------------------------------------------------
    async def async_step_diagnostics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the short-cycle threshold and the recovery timings."""
        config = self.config
        if user_input is not None:
            config[CONF_SHORT_CYCLE_SECONDS] = float(user_input[CONF_SHORT_CYCLE_SECONDS])
            config[CONF_STARTUP_GRACE] = float(user_input[CONF_STARTUP_GRACE])
            config[CONF_PERIODIC_AUDIT] = float(user_input[CONF_PERIODIC_AUDIT])
            return self._save()

        return self.async_show_form(
            step_id=STEP_DIAGNOSTICS, data_schema=diagnostics_schema(config)
        )


__all__ = [
    "BOILER_SENSOR_KEYS",
    "BOILER_TEMPERATURE_KEYS",
    "CURVE_FIELD_KEYS",
    "MENU_OPTIONS",
    "PROBE_ENTITY_KEYS",
    "PROBE_THRESHOLD_KEYS",
    "STEP_DIAGNOSTICS",
    "STEP_HEAT_SOURCE",
    "STEP_INIT",
    "STEP_PROBES",
    "STEP_SCHEDULE",
    "STEP_STRATEGIES",
    "STEP_ZONES",
    "STEP_ZONE_DETAIL",
    "STEP_ZONE_MENU",
    "VERIFY_KEYS",
    "ZONE_MENU_OPTIONS",
    "ClimatePilotOptionsFlow",
    "ScheduleFormatError",
    "climate_selector",
    "control_mode_selector",
    "curve_defaults",
    "curve_from_input",
    "curve_schema",
    "default_zone_config",
    "diagnostics_schema",
    "error_key_for_climate",
    "find_zone",
    "format_schedule_text",
    "number_field",
    "outdoor_schema",
    "parse_schedule_text",
    "probes_schema",
    "schedule_config",
    "validate_curve",
    "validate_delta_t_range",
    "verify_schema",
    "weather_mode_selector",
    "zone_choices",
    "zone_entity_ids",
    "zone_entries",
    "zone_schedule_text",
    "zone_selection_errors",
]
