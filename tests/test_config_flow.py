"""Unit tests for the ClimatePilot config flow (task E3).

``pytest-homeassistant-custom-component`` is not available in the verification
venv (checked with ``pip show``), so the flow is driven directly: a ``hass``
stand-in supplies the two things the flow actually touches — ``hass.states``
and ``hass.config_entries`` — and every submitted payload is first run through
the schema the flow itself returned, exactly as
``data_entry_flow.FlowManager`` does for a real UI submission.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigEntry
from homeassistant.core import State

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.climatepilot import config_flow as cf
from custom_components.climatepilot.const import (
    CONF_BOILER_ENTITY,
    CONF_CONTROL_MODE,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_SYSTEM_NAME,
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_MIN_FLOW,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_WEATHER_CURVE,
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
    CONF_ZONE_STEP,
    CONF_ZONE_TOLERANCE,
    CONF_ZONES,
    CONTROL_MODE_OBSERVE,
    DOMAIN,
    ZONE_MODE_AUTO,
)
from custom_components.climatepilot.models import Schedule
from custom_components.climatepilot.options_flow import (
    CONF_CURVE_OUTDOOR_DESIGN,
    CONF_CURVE_OUTDOOR_STOP,
    CONF_CURVE_SUPPLY_DESIGN,
    CONF_CURVE_SUPPLY_ECO,
    CONF_CURVE_SUPPLY_MAX,
    CONF_CURVE_SUPPLY_MIN,
    default_zone_config,
)

BOILER = "climate.boiler"
LIVING = "climate.living_room"
BEDROOM = "climate.bedroom"
OUTDOOR = "sensor.outdoor_temperature"

BOILER_ATTRS = {"hvac_modes": ["off", "heat"], "min_temp": 35.0, "max_temp": 60.0}
ZONE_ATTRS = {
    "hvac_modes": ["off", "heat"],
    "target_temp_step": 0.5,
    "min_temp": 16.0,
    "max_temp": 26.0,
    "friendly_name": "Living Room",
}


# ---------------------------------------------------------------------------
# Minimal hass stand-in
# ---------------------------------------------------------------------------
class _States:
    """Just enough of ``homeassistant.core.StateMachine`` for the flow."""

    def __init__(self) -> None:
        self._states: dict[str, State] = {}

    def set(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        self._states[entity_id] = State(entity_id, state, attributes)

    def get(self, entity_id: str) -> State | None:
        return self._states.get(entity_id)


class _FlowManager:
    """Stand-in for ``hass.config_entries.flow``."""

    def __init__(self) -> None:
        self.aborted: list[str] = []

    def async_progress_by_handler(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def async_abort(self, flow_id: str) -> None:
        self.aborted.append(flow_id)


class _ConfigEntries:
    """Stand-in for ``hass.config_entries``."""

    def __init__(self, entries: list[ConfigEntry] | None = None) -> None:
        self.entries = list(entries or [])
        self.flow = _FlowManager()

    def async_entries(
        self,
        domain: str | None = None,
        include_ignore: bool = True,
        include_disabled: bool = True,
    ) -> list[ConfigEntry]:
        return [e for e in self.entries if domain is None or e.domain == domain]

    def async_get_known_entry(self, entry_id: str) -> ConfigEntry:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        raise KeyError(entry_id)

    def async_entry_for_domain_unique_id(self, domain: str, unique_id: str) -> ConfigEntry | None:
        for entry in self.entries:
            if entry.domain == domain and entry.unique_id == unique_id:
                return entry
        return None


class MockHass:
    """The slice of ``HomeAssistant`` the config/options flows use."""

    def __init__(self, entries: list[ConfigEntry] | None = None) -> None:
        self.states = _States()
        self.config_entries = _ConfigEntries(entries)

    # -- fixtures ------------------------------------------------------
    def add_climate(self, entity_id: str, state: str = "heat", **attributes: Any) -> None:
        self.states.set(entity_id, state, dict(attributes))

    def add_sensor(self, entity_id: str, state: str = "7.5", **attributes: Any) -> None:
        self.states.set(entity_id, state, dict(attributes))


def make_entry(**options: Any) -> ConfigEntry:
    """Build a config entry shaped like the one the wizard creates."""
    return ConfigEntry(
        data={CONF_SYSTEM_NAME: "Home"},
        domain=DOMAIN,
        entry_id="entry-1",
        minor_version=1,
        options=options,
        source=SOURCE_USER,
        subentries_data=None,
        title="Home",
        unique_id=DOMAIN,
        version=1,
        discovery_keys=MappingProxyType({}),
    )


def build_hass(*, entries: list[ConfigEntry] | None = None) -> MockHass:
    """A hass with the standard boiler / rooms / outdoor sensor registered."""
    hass = MockHass(entries)
    hass.add_climate(BOILER, **BOILER_ATTRS)
    hass.add_climate(BEDROOM, **{**ZONE_ATTRS, "friendly_name": "Bedroom"})
    hass.add_climate(LIVING, **ZONE_ATTRS)
    hass.add_sensor(OUTDOOR, "7.5", device_class="temperature")
    return hass


def make_flow(hass: MockHass) -> cf.ClimatePilotConfigFlow:
    """Create a config flow wired to ``hass`` like the flow manager does."""
    flow = cf.ClimatePilotConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.flow_id = "flow-1"
    flow.handler = DOMAIN
    flow.context = {"source": SOURCE_USER}
    return flow


async def render(flow: cf.ClimatePilotConfigFlow, step: str) -> dict[str, Any]:
    """Ask ``step`` for its form without submitting anything."""
    result = await getattr(flow, f"async_step_{step}")(None)
    assert result["type"] == "form", f"{step}: expected a form, got {result['type']}"
    assert result["step_id"] == step
    assert isinstance(result["data_schema"], vol.Schema)
    return result


async def submit(
    flow: cf.ClimatePilotConfigFlow, step: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Submit ``payload`` to ``step`` the way ``data_entry_flow`` does.

    The form is rendered first so the submitted dict is coerced by the very
    schema the user would have seen — selector defaults, ``None`` handling and
    any ``vol.Any`` wrapper are really exercised.
    """
    form = await render(flow, step)
    validated = form["data_schema"](dict(payload))
    return await getattr(flow, f"async_step_{step}")(validated)


def expect_form(result: dict[str, Any], step: str) -> dict[str, Any]:
    assert result["type"] == "form", f"expected form {step}, got {result}"
    assert result["step_id"] == step
    return result


async def walk_to_zones(flow: cf.ClimatePilotConfigFlow, hass: MockHass) -> dict[str, Any]:
    """Drive the wizard from ``user`` through to the ``zones`` form."""
    await submit(flow, cf.STEP_USER, {CONF_SYSTEM_NAME: "Home"})
    await submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER})
    await submit(flow, cf.STEP_VERIFY, {})
    await submit(
        flow,
        cf.STEP_OUTDOOR,
        {CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR, CONF_OUTDOOR_STALE_MINUTES: 90},
    )
    result = await submit(
        flow,
        cf.STEP_WEATHER_CURVE,
        {
            CONF_CURVE_OUTDOOR_DESIGN: 0.0,
            CONF_CURVE_OUTDOOR_STOP: 15.0,
            CONF_CURVE_SUPPLY_DESIGN: 45.0,
            CONF_CURVE_SUPPLY_ECO: 40.0,
            CONF_CURVE_SUPPLY_MIN: 35.0,
            CONF_CURVE_SUPPLY_MAX: 55.0,
        },
    )
    return expect_form(result, cf.STEP_ZONES)


def run(coro: Any) -> Any:
    """Run a coroutine to completion (no pytest-asyncio dependency needed)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------
def test_user_step_offers_the_default_name() -> None:
    flow = make_flow(build_hass())
    result = run(render(flow, cf.STEP_USER))
    assert result["data_schema"]({}) == {CONF_SYSTEM_NAME: "ClimatePilot"}


def test_second_instance_aborts_single_instance_allowed() -> None:
    hass = build_hass(entries=[make_entry()])
    flow = make_flow(hass)
    result = run(flow.async_step_user({CONF_SYSTEM_NAME: "Another"}))
    assert result["type"] == "abort"
    assert result["reason"] == "single_instance_allowed"


def test_user_step_sets_a_fixed_unique_id() -> None:
    flow = make_flow(build_hass())
    result = run(submit(flow, cf.STEP_USER, {CONF_SYSTEM_NAME: "Home"}))
    expect_form(result, cf.STEP_BOILER)
    assert flow.unique_id == DOMAIN


def test_blank_name_falls_back_to_the_default() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_USER, {CONF_SYSTEM_NAME: "   "}))
    # A blank name is normalised when the entry is finally created, not while
    # the wizard is still running.
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    run(submit(flow, cf.STEP_VERIFY, {}))
    run(submit(flow, cf.STEP_OUTDOOR, {CONF_OUTDOOR_TEMP_ENTITY: None}))
    run(
        submit(
            flow,
            cf.STEP_WEATHER_CURVE,
            {
                CONF_CURVE_OUTDOOR_DESIGN: 0.0,
                CONF_CURVE_OUTDOOR_STOP: 15.0,
                CONF_CURVE_SUPPLY_DESIGN: 45.0,
                CONF_CURVE_SUPPLY_ECO: 40.0,
                CONF_CURVE_SUPPLY_MIN: 35.0,
                CONF_CURVE_SUPPLY_MAX: 55.0,
            },
        )
    )
    run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: []}))
    result = run(submit(flow, cf.STEP_CONTROL_MODE, {CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE}))
    assert result["type"] == "create_entry"
    assert result["title"] == "ClimatePilot"
    assert result["data"][CONF_SYSTEM_NAME] == "ClimatePilot"


# ---------------------------------------------------------------------------
# boiler
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("entity_id", "state", "attributes", "expected"),
    [
        ("climate.missing", None, None, "boiler_not_found"),
        (BOILER, "unavailable", BOILER_ATTRS, "boiler_unavailable"),
        (BOILER, "unknown", BOILER_ATTRS, "boiler_unavailable"),
        (BOILER, "heat", {}, "no_hvac_modes"),
        (BOILER, "heat", {"hvac_modes": []}, "no_hvac_modes"),
    ],
)
def test_boiler_validation_errors(
    entity_id: str,
    state: str | None,
    attributes: dict[str, Any] | None,
    expected: str,
) -> None:
    hass = build_hass()
    if state is not None and attributes is not None:
        hass.add_climate(entity_id, state, **attributes)
    flow = make_flow(hass)
    result = run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: entity_id}))
    assert result["errors"] == {CONF_BOILER_ENTITY: expected}
    # A rejected boiler must not advance the wizard.
    assert CONF_BOILER_ENTITY not in flow._config


def test_boiler_accepts_optional_sensors() -> None:
    hass = build_hass()
    hass.add_sensor("sensor.burner_state", "off")
    flow = make_flow(hass)
    result = run(
        submit(
            flow,
            cf.STEP_BOILER,
            {
                CONF_BOILER_ENTITY: BOILER,
                "burner_state_entity": "sensor.burner_state",
                "supply_temp_entity": None,
                "return_temp_entity": None,
            },
        )
    )
    expect_form(result, cf.STEP_VERIFY)
    assert flow._config["burner_state_entity"] == "sensor.burner_state"
    assert flow._config["supply_temp_entity"] is None


# ---------------------------------------------------------------------------
# verify — the safety gate
# ---------------------------------------------------------------------------
def test_verify_checkboxes_default_to_not_confirmed() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    result = run(render(flow, cf.STEP_VERIFY))
    assert result["data_schema"]({}) == {
        CONF_VERIFY_HEATING_ONLY: False,
        CONF_VERIFY_SUPPLY_TEMP: False,
        CONF_VERIFY_MIN_FLOW: False,
    }
    assert result["description_placeholders"] == {"unverified_count": "3"}


def test_verify_records_attestations_and_keeps_them_unverified_by_default() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    result = run(submit(flow, cf.STEP_VERIFY, {}))
    expect_form(result, cf.STEP_OUTDOOR)
    assert flow._config[CONF_VERIFY_HEATING_ONLY] is False
    assert flow._config[CONF_VERIFY_SUPPLY_TEMP] is False
    assert flow._config[CONF_VERIFY_MIN_FLOW] is False


def test_verify_does_not_block_when_unchecked() -> None:
    """Suggest-only is a supported outcome, so the form must not raise an error."""
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    result = run(render(flow, cf.STEP_VERIFY))
    assert not result["errors"]


def test_verify_accepts_confirmed_attestations() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    result = run(
        submit(
            flow,
            cf.STEP_VERIFY,
            {
                CONF_VERIFY_HEATING_ONLY: True,
                CONF_VERIFY_SUPPLY_TEMP: True,
                CONF_VERIFY_MIN_FLOW: True,
            },
        )
    )
    assert result["step_id"] == cf.STEP_OUTDOOR
    assert flow._config[CONF_VERIFY_MIN_FLOW] is True


# ---------------------------------------------------------------------------
# outdoor
# ---------------------------------------------------------------------------
def test_outdoor_is_optional_but_validated_when_set() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    run(submit(flow, cf.STEP_VERIFY, {}))
    result = run(
        submit(
            flow,
            cf.STEP_OUTDOOR,
            {CONF_OUTDOOR_TEMP_ENTITY: "sensor.nope", CONF_OUTDOOR_STALE_MINUTES: 90},
        )
    )
    assert result["errors"] == {CONF_OUTDOOR_TEMP_ENTITY: "outdoor_not_found"}


def test_outdoor_may_be_skipped_entirely() -> None:
    flow = make_flow(build_hass())
    run(submit(flow, cf.STEP_BOILER, {CONF_BOILER_ENTITY: BOILER}))
    run(submit(flow, cf.STEP_VERIFY, {}))
    result = run(submit(flow, cf.STEP_OUTDOOR, {CONF_OUTDOOR_TEMP_ENTITY: None}))
    expect_form(result, cf.STEP_WEATHER_CURVE)
    assert flow._config[CONF_OUTDOOR_TEMP_ENTITY] is None


# ---------------------------------------------------------------------------
# weather_curve
# ---------------------------------------------------------------------------
def test_curve_defaults_match_the_model() -> None:
    flow = make_flow(build_hass())
    result = run(render(flow, cf.STEP_WEATHER_CURVE))
    assert result["data_schema"]({}) == {
        CONF_CURVE_OUTDOOR_DESIGN: 0.0,
        CONF_CURVE_OUTDOOR_STOP: 15.0,
        CONF_CURVE_SUPPLY_DESIGN: 45.0,
        CONF_CURVE_SUPPLY_ECO: 40.0,
        CONF_CURVE_SUPPLY_MIN: 35.0,
        CONF_CURVE_SUPPLY_MAX: 55.0,
    }


@pytest.mark.parametrize(
    "curve",
    [
        # outdoor_stop must be greater than outdoor_design
        {
            CONF_CURVE_OUTDOOR_DESIGN: 15.0,
            CONF_CURVE_OUTDOOR_STOP: 15.0,
            CONF_CURVE_SUPPLY_DESIGN: 45.0,
            CONF_CURVE_SUPPLY_ECO: 40.0,
            CONF_CURVE_SUPPLY_MIN: 35.0,
            CONF_CURVE_SUPPLY_MAX: 55.0,
        },
        {
            CONF_CURVE_OUTDOOR_DESIGN: 20.0,
            CONF_CURVE_OUTDOOR_STOP: 15.0,
            CONF_CURVE_SUPPLY_DESIGN: 45.0,
            CONF_CURVE_SUPPLY_ECO: 40.0,
            CONF_CURVE_SUPPLY_MIN: 35.0,
            CONF_CURVE_SUPPLY_MAX: 55.0,
        },
        # supply_max must be greater than supply_min
        {
            CONF_CURVE_OUTDOOR_DESIGN: 0.0,
            CONF_CURVE_OUTDOOR_STOP: 15.0,
            CONF_CURVE_SUPPLY_DESIGN: 40.0,
            CONF_CURVE_SUPPLY_ECO: 40.0,
            CONF_CURVE_SUPPLY_MIN: 55.0,
            CONF_CURVE_SUPPLY_MAX: 55.0,
        },
        # design point outside the clamp window would be silently flattened
        {
            CONF_CURVE_OUTDOOR_DESIGN: 0.0,
            CONF_CURVE_OUTDOOR_STOP: 15.0,
            CONF_CURVE_SUPPLY_DESIGN: 60.0,
            CONF_CURVE_SUPPLY_ECO: 40.0,
            CONF_CURVE_SUPPLY_MIN: 35.0,
            CONF_CURVE_SUPPLY_MAX: 55.0,
        },
    ],
)
def test_invalid_curve_is_rejected(curve: dict[str, float]) -> None:
    flow = make_flow(build_hass())
    result = run(submit(flow, cf.STEP_WEATHER_CURVE, curve))
    assert result["errors"] == {"base": "invalid_curve"}
    assert CONF_WEATHER_CURVE not in flow._config


def test_valid_curve_is_stored_with_model_field_names() -> None:
    flow = make_flow(build_hass())
    result = run(
        submit(
            flow,
            cf.STEP_WEATHER_CURVE,
            {
                CONF_CURVE_OUTDOOR_DESIGN: -5.0,
                CONF_CURVE_OUTDOOR_STOP: 15.0,
                CONF_CURVE_SUPPLY_DESIGN: 48.0,
                CONF_CURVE_SUPPLY_ECO: 38.0,
                CONF_CURVE_SUPPLY_MIN: 35.0,
                CONF_CURVE_SUPPLY_MAX: 55.0,
            },
        )
    )
    assert result["step_id"] == cf.STEP_ZONES
    assert flow._config[CONF_WEATHER_CURVE] == {
        "outdoor_design": -5.0,
        "outdoor_stop": 15.0,
        "supply_design": 48.0,
        "supply_eco": 38.0,
        "supply_min": 35.0,
        "supply_max": 55.0,
        "night_offset": 0.0,
    }


# ---------------------------------------------------------------------------
# zones
# ---------------------------------------------------------------------------
def test_duplicate_room_is_rejected() -> None:
    flow = make_flow(build_hass())
    result = run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: [LIVING, LIVING]}))
    assert result["errors"] == {CONF_ZONES: "zone_duplicate"}


def test_boiler_cannot_be_a_room() -> None:
    flow = make_flow(build_hass())
    flow._config[CONF_BOILER_ENTITY] = BOILER
    result = run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: [BOILER, LIVING]}))
    assert result["errors"] == {CONF_ZONES: "zone_is_boiler"}


def test_unknown_room_is_rejected() -> None:
    flow = make_flow(build_hass())
    result = run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: ["climate.ghost"]}))
    assert result["errors"] == {CONF_ZONES: "zone_not_found"}


def test_zero_rooms_is_allowed() -> None:
    flow = make_flow(build_hass())
    result = run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: []}))
    expect_form(result, cf.STEP_CONTROL_MODE)
    assert flow._config[CONF_ZONES] == []


def test_zone_field_tolerates_no_selection() -> None:
    """An untouched multi-entity selector must not blow up the form."""
    flow = make_flow(build_hass())
    result = run(render(flow, cf.STEP_ZONES))
    schema = result["data_schema"]
    assert schema({})[CONF_ZONES] == []
    assert schema({CONF_ZONES: None})[CONF_ZONES] is None


# ---------------------------------------------------------------------------
# control_mode + entry creation
# ---------------------------------------------------------------------------
def test_control_mode_defaults_to_observe() -> None:
    flow = make_flow(build_hass())
    result = run(render(flow, cf.STEP_CONTROL_MODE))
    assert result["data_schema"]({}) == {CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE}


def test_full_wizard_creates_a_complete_entry() -> None:
    hass = build_hass()
    flow = make_flow(hass)
    run(walk_to_zones(flow, hass))

    zones = run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: [LIVING, BEDROOM]}))
    expect_form(zones, cf.STEP_CONTROL_MODE)
    result = run(submit(flow, cf.STEP_CONTROL_MODE, {CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE}))

    assert result["type"] == "create_entry"
    assert result["title"] == "Home"
    assert result["data"] == {CONF_SYSTEM_NAME: "Home"}

    options = result["options"]
    assert options[CONF_SYSTEM_NAME] == "Home"
    assert options[CONF_BOILER_ENTITY] == BOILER
    assert options[CONF_OUTDOOR_TEMP_ENTITY] == OUTDOOR
    assert options[CONF_CONTROL_MODE] == CONTROL_MODE_OBSERVE
    assert options[CONF_VERIFY_HEATING_ONLY] is False
    assert options[CONF_VERIFY_SUPPLY_TEMP] is False
    assert options[CONF_VERIFY_MIN_FLOW] is False
    assert [zone[CONF_ZONE_ENTITY] for zone in options[CONF_ZONES]] == [
        LIVING,
        BEDROOM,
    ]


def test_zone_block_matches_the_coordinator_contract() -> None:
    """Every key the coordinator parses must be present and correctly typed."""
    hass = build_hass()
    flow = make_flow(hass)
    run(walk_to_zones(flow, hass))
    run(submit(flow, cf.STEP_ZONES, {CONF_ZONES: [LIVING]}))
    result = run(submit(flow, cf.STEP_CONTROL_MODE, {CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE}))

    (zone,) = result["options"][CONF_ZONES]
    assert set(zone) == {
        CONF_ZONE_ID,
        CONF_ZONE_ENTITY,
        CONF_ZONE_NAME,
        CONF_ZONE_MODE,
        CONF_ZONE_MIN_TEMP,
        CONF_ZONE_MAX_TEMP,
        CONF_ZONE_STEP,
        CONF_ZONE_TOLERANCE,
        CONF_ZONE_OVERRIDE_POLICY,
        CONF_ZONE_OVERRIDE_MINUTES,
        CONF_ZONE_COLD_ADJUSTMENT,
        CONF_ZONE_AUX_LOAD,
        CONF_ZONE_ACTION_ENTITY,
        CONF_ZONE_SCHEDULE,
        CONF_ZONE_MODES,
    }
    assert zone[CONF_ZONE_ID] == "living_room"
    assert zone[CONF_ZONE_ENTITY] == LIVING
    assert zone[CONF_ZONE_NAME] == "Living Room"
    assert zone[CONF_ZONE_MODE] == ZONE_MODE_AUTO
    assert zone[CONF_ZONE_STEP] == 0.5
    assert zone[CONF_ZONE_ACTION_ENTITY] is None
    assert zone[CONF_ZONE_MODES] == {"comfort": 22.0, "eco": 19.0, "away": 16.0}
    # The schedule must round-trip straight through models.Schedule.
    assert len(Schedule.from_dict(zone[CONF_ZONE_SCHEDULE]).slots) == 4
    assert [slot["at"] for slot in zone[CONF_ZONE_SCHEDULE]["slots"]] == [
        "06:00:00",
        "09:00:00",
        "17:00:00",
        "23:00:00",
    ]
    assert all(isinstance(slot["setpoint"], float) for slot in zone[CONF_ZONE_SCHEDULE]["slots"])
    assert all(isinstance(slot["days"], list) for slot in zone[CONF_ZONE_SCHEDULE]["slots"])


def test_zone_ids_stay_unique_for_repeated_object_ids() -> None:
    hass = build_hass()
    taken: set[str] = set()
    first = default_zone_config(hass, LIVING, taken)
    taken.add(first[CONF_ZONE_ID])
    second = default_zone_config(hass, BEDROOM, taken)
    taken.add(second[CONF_ZONE_ID])
    assert first[CONF_ZONE_ID] == "living_room"
    assert second[CONF_ZONE_ID] == "bedroom"

    hass.states.set("climate.living_room_2", "heat", dict(ZONE_ATTRS))
    third = default_zone_config(hass, "climate.living_room_2", taken)
    assert third[CONF_ZONE_ID] == "living_room_2"


def test_zone_defaults_survive_a_thermostat_without_attributes() -> None:
    hass = build_hass()
    hass.add_climate("climate.bare", "heat")
    zone = default_zone_config(hass, "climate.bare", set())
    assert zone[CONF_ZONE_STEP] == 0.5
    assert zone[CONF_ZONE_NAME] == "Bare"
    assert zone[CONF_ZONE_MIN_TEMP] == 16.0
    assert zone[CONF_ZONE_MAX_TEMP] == 26.0


def test_options_flow_is_registered_and_takes_no_config_entry() -> None:
    handler = cf.ClimatePilotConfigFlow.async_get_options_flow(make_entry())
    assert handler.__class__.__name__ == "ClimatePilotOptionsFlow"
    assert handler.automatic_reload is True


def test_config_flow_registers_itself_for_the_domain() -> None:
    from homeassistant.config_entries import HANDLERS

    assert HANDLERS.get(DOMAIN) is cf.ClimatePilotConfigFlow


def test_vol_schema_is_used_for_every_form() -> None:
    """Guard against a step returning a bare dict instead of a vol.Schema."""
    flow = make_flow(build_hass())
    result = run(render(flow, cf.STEP_USER))
    assert isinstance(result["data_schema"], vol.Schema)
