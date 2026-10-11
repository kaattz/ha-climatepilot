"""Unit tests for the ClimatePilot options flow (task E3).

The options flow is driven directly against a mock ``hass`` and a real
``ConfigEntry`` (HA 2026.2 ``OptionsFlow`` resolves both lazily from
``self.hass`` / ``self.handler``), so no Home Assistant bootstrap is required.

Every submission is first coerced by the schema the flow itself returned —
exactly what ``data_entry_flow.FlowManager`` does for a real UI submission.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigEntry

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_config_flow import (
    BOILER,
    LIVING,
    OUTDOOR,
    MockHass,
    build_hass,
    make_entry,
)

from custom_components.climatepilot import options_flow as of
from custom_components.climatepilot.const import (
    CONF_BOILER_ENTITY,
    CONF_CONTROL_MODE,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_PERIODIC_AUDIT,
    CONF_SHORT_CYCLE_SECONDS,
    CONF_STARTUP_GRACE,
    CONF_SYSTEM_NAME,
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
    CONTROL_MODE_OBSERVE,
    DEFAULT_PERIODIC_AUDIT,
    DEFAULT_SHORT_CYCLE_SECONDS,
    DEFAULT_STARTUP_GRACE,
    DEFAULT_WEATHER_STALE_AFTER,
    OVERRIDE_FIXED_DURATION,
    OVERRIDE_INDEFINITE,
    OVERRIDE_NEXT_SCHEDULE,
    WEATHER_MODE_APPLY,
    WEATHER_MODE_SUGGEST,
    ZONE_MODE_AUTO,
    ZONE_MODE_COMFORT,
)
from custom_components.climatepilot.models import (
    DEFAULT_SLOTS,
    Schedule,
)
from custom_components.climatepilot.options_flow import (
    CONF_CURVE_OUTDOOR_DESIGN,
    CONF_CURVE_OUTDOOR_STOP,
    CONF_CURVE_SUPPLY_DESIGN,
    CONF_CURVE_SUPPLY_ECO,
    CONF_CURVE_SUPPLY_MAX,
    CONF_CURVE_SUPPLY_MIN,
    ScheduleFormatError,
    parse_schedule_text,
)

CURVE = {
    CONF_CURVE_OUTDOOR_DESIGN: 0.0,
    CONF_CURVE_OUTDOOR_STOP: 15.0,
    CONF_CURVE_SUPPLY_DESIGN: 45.0,
    CONF_CURVE_SUPPLY_ECO: 40.0,
    CONF_CURVE_SUPPLY_MIN: 35.0,
    CONF_CURVE_SUPPLY_MAX: 55.0,
}


def zone_block(entity_id: str = LIVING, zone_id: str = "living_room") -> dict[str, Any]:
    """A zone options block shaped like the one the wizard writes."""
    return {
        CONF_ZONE_ID: zone_id,
        CONF_ZONE_ENTITY: entity_id,
        CONF_ZONE_NAME: "Living Room",
        CONF_ZONE_MODE: ZONE_MODE_AUTO,
        CONF_ZONE_MIN_TEMP: 16.0,
        CONF_ZONE_MAX_TEMP: 26.0,
        CONF_ZONE_STEP: 0.5,
        CONF_ZONE_TOLERANCE: 0.2,
        CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_NEXT_SCHEDULE,
        CONF_ZONE_OVERRIDE_MINUTES: 120,
        CONF_ZONE_COLD_ADJUSTMENT: False,
        CONF_ZONE_AUX_LOAD: False,
        CONF_ZONE_ACTION_ENTITY: None,
        CONF_ZONE_SCHEDULE: Schedule(slots=DEFAULT_SLOTS).to_dict(),
        CONF_ZONE_MODES: {"comfort": 22.0, "eco": 19.0, "away": 16.0},
    }


def entry_with(**options: Any) -> ConfigEntry:
    """A config entry with the wizard's standard data + the given options."""
    base: dict[str, Any] = {
        CONF_SYSTEM_NAME: "Home",
        CONF_BOILER_ENTITY: BOILER,
        CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
        CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE,
        CONF_WEATHER_CURVE: {**CURVE, "night_offset": 0.0},
        CONF_ZONES: [zone_block()],
    }
    base.update(options)
    return make_entry(**base)


def make_options_flow(entry: ConfigEntry, hass: MockHass) -> of.ClimatePilotOptionsFlow:
    """Create an options flow wired the way ``OptionsFlowManager`` wires it."""
    # ``OptionsFlow.config_entry`` resolves the entry through
    # ``hass.config_entries.async_get_known_entry(handler)``.
    if entry not in hass.config_entries.entries:
        hass.config_entries.entries.append(entry)
    flow = of.ClimatePilotOptionsFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.handler = entry.entry_id
    flow.flow_id = "options-1"
    flow.context = {"source": SOURCE_USER, "entry_id": entry.entry_id}
    # ``super().__init__`` is not called by HA either; keep the scratch state.
    flow._working = None
    flow._zone_id = None
    return flow


async def render(flow: of.ClimatePilotOptionsFlow, step: str) -> dict[str, Any]:
    """Ask ``step`` for its form / menu without submitting anything."""
    result = await getattr(flow, f"async_step_{step}")(None)
    assert result["type"] in ("form", "menu"), f"{step}: got {result['type']}"
    assert result["step_id"] == step
    if result["type"] == "form":
        assert isinstance(result["data_schema"], vol.Schema)
    return result


async def submit(
    flow: of.ClimatePilotOptionsFlow, step: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Submit ``payload`` to ``step``, coerced by the very form it rendered."""
    form = await render(flow, step)
    validated = form["data_schema"](dict(payload))
    return await getattr(flow, f"async_step_{step}")(validated)


def expect_form(result: dict[str, Any], step: str) -> dict[str, Any]:
    assert result["type"] == "form", f"expected form {step}, got {result}"
    assert result["step_id"] == step
    return result


def expect_saved(result: dict[str, Any]) -> dict[str, Any]:
    """A completing options step creates the entry with the merged options."""
    assert result["type"] == "create_entry", result
    return result["data"]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# menu + zone selection
# ---------------------------------------------------------------------------
def test_init_shows_every_top_level_menu() -> None:
    """The init menu must mirror ``MENU_OPTIONS`` exactly, in order.

    Derived from the module constant rather than a hardcoded list: the V0.2
    version spelled the four entries out and silently went stale when V0.3
    added ``probes``. Order is asserted too because the menu is a user-facing
    sequence.
    """
    flow = make_options_flow(entry_with(), build_hass())
    result = run(render(flow, of.STEP_INIT))
    assert result["type"] == "menu"
    assert result["menu_options"] == of.MENU_OPTIONS
    assert set(of.MENU_OPTIONS) == {
        of.STEP_ZONES,
        of.STEP_STRATEGIES,
        of.STEP_HEAT_SOURCE,
        of.STEP_PROBES,
        of.STEP_DIAGNOSTICS,
    }


def test_probes_step_exposes_the_v03_thresholds() -> None:
    """The V0.3 probe step must offer every tuning key from const.py."""
    flow = make_options_flow(entry_with(), build_hass())
    form = run(render(flow, of.STEP_PROBES))
    assert form["type"] == "form"
    fields = {getattr(marker, "schema", marker) for marker in form["data_schema"].schema}
    assert fields == {
        "probe_stale_seconds",
        "probe_frozen_seconds",
        "delta_t_window_seconds",
        "condensing_return_c",
        "delta_t_healthy_min",
        "delta_t_healthy_max",
        "probe_max_divergence",
    }


def test_probes_step_rejects_an_inverted_healthy_band() -> None:
    """``delta_t_healthy_min >= delta_t_healthy_max`` must be refused."""
    flow = make_options_flow(entry_with(), build_hass())
    result = run(
        submit(
            flow,
            of.STEP_PROBES,
            {
                "probe_stale_seconds": 900,
                "probe_frozen_seconds": 3600,
                "delta_t_window_seconds": 3600,
                "condensing_return_c": 55,
                "delta_t_healthy_min": 15,
                "delta_t_healthy_max": 5,
                "probe_max_divergence": 5,
            },
        )
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_delta_t_range"}


def test_options_flow_reloads_the_entry_automatically() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    assert flow.automatic_reload is True


def test_config_view_merges_data_and_options() -> None:
    entry = entry_with()
    flow = make_options_flow(entry, build_hass())
    config = flow.config
    assert config[CONF_SYSTEM_NAME] == "Home"
    assert config[CONF_BOILER_ENTITY] == BOILER
    assert len(config[CONF_ZONES]) == 1


def test_config_view_is_a_copy_and_never_mutates_the_entry() -> None:
    entry = entry_with()
    flow = make_options_flow(entry, build_hass())
    flow.config[CONF_CONTROL_MODE] = "coordinate"
    assert entry.options[CONF_CONTROL_MODE] == CONTROL_MODE_OBSERVE


def test_zones_step_shows_the_current_selection() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(render(flow, of.STEP_ZONES))
    assert result["data_schema"]({})[CONF_ZONES] == [LIVING]


def test_adding_a_room_creates_a_default_block() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: [LIVING, "climate.bedroom"]})))
    assert [zone[CONF_ZONE_ENTITY] for zone in data[CONF_ZONES]] == [
        LIVING,
        "climate.bedroom",
    ]
    bedroom = data[CONF_ZONES][1]
    assert bedroom[CONF_ZONE_ID] == "bedroom"
    assert bedroom[CONF_ZONE_NAME] == "Bedroom"
    assert bedroom[CONF_ZONE_STEP] == 0.5
    assert Schedule.from_dict(bedroom[CONF_ZONE_SCHEDULE]).slots == DEFAULT_SLOTS


def test_removing_a_room_keeps_the_other_blocks_intact() -> None:
    flow = make_options_flow(
        entry_with(
            **{
                CONF_ZONES: [
                    zone_block(),
                    zone_block("climate.bedroom", "bedroom"),
                ]
            }
        ),
        build_hass(),
    )
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: ["climate.bedroom"]})))
    assert len(data[CONF_ZONES]) == 1
    assert data[CONF_ZONES][0][CONF_ZONE_ID] == "bedroom"


def test_room_edits_survive_a_zone_list_resave() -> None:
    """Re-saving the list must not reset a room's customised strategy."""
    hass = build_hass()
    flow = make_options_flow(entry_with(), hass)
    flow._zone_id = "living_room"
    run(
        submit(
            flow,
            of.STEP_ZONE_DETAIL,
            {
                CONF_ZONE_NAME: "Lounge",
                CONF_ZONE_MODE: ZONE_MODE_COMFORT,
                CONF_ZONE_MIN_TEMP: 16.0,
                CONF_ZONE_MAX_TEMP: 26.0,
                CONF_ZONE_STEP: 0.5,
                CONF_ZONE_TOLERANCE: 0.2,
                CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_NEXT_SCHEDULE,
                CONF_ZONE_OVERRIDE_MINUTES: 120,
                CONF_ZONE_COLD_ADJUSTMENT: False,
                CONF_ZONE_AUX_LOAD: False,
            },
        )
    )
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: [LIVING]})))
    assert data[CONF_ZONES][0][CONF_ZONE_NAME] == "Lounge"
    assert data[CONF_ZONES][0][CONF_ZONE_MODE] == ZONE_MODE_COMFORT


def _edit_zone(flow: of.ClimatePilotOptionsFlow, *, name: str, mode: str) -> dict[str, Any]:
    """Select the only room and save a minimal strategy edit."""
    flow._zone_id = "living_room"
    return run(
        submit(
            flow,
            of.STEP_ZONE_DETAIL,
            {
                CONF_ZONE_NAME: name,
                CONF_ZONE_MODE: mode,
                CONF_ZONE_MIN_TEMP: 16.0,
                CONF_ZONE_MAX_TEMP: 26.0,
                CONF_ZONE_STEP: 0.5,
                CONF_ZONE_TOLERANCE: 0.2,
                CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_NEXT_SCHEDULE,
                CONF_ZONE_OVERRIDE_MINUTES: 120,
                CONF_ZONE_COLD_ADJUSTMENT: False,
                CONF_ZONE_AUX_LOAD: False,
            },
        )
    )


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ([LIVING, LIVING], "zone_duplicate"),
        ([BOILER], "zone_is_boiler"),
        (["climate.ghost"], "zone_not_found"),
    ],
)
def test_zone_selection_errors(selected: list[str], expected: str) -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(submit(flow, of.STEP_ZONES, {CONF_ZONES: selected}))
    assert result["errors"] == {CONF_ZONES: expected}


def test_zones_may_be_emptied_completely() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: []})))
    assert data[CONF_ZONES] == []


# ---------------------------------------------------------------------------
# strategies -> zone_menu -> zone_detail / schedule
# ---------------------------------------------------------------------------
def test_zone_menu_is_offered_per_room() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(render(flow, of.STEP_STRATEGIES))
    # ``zone_id`` is deliberately Required with no default: the user must pick a
    # room, and an empty submission has to be rejected rather than silently
    # editing an arbitrary one.
    (marker,) = result["data_schema"].schema
    assert marker.schema == CONF_ZONE_ID
    assert marker.default is vol.UNDEFINED
    with pytest.raises(vol.MultipleInvalid):
        result["data_schema"]({})

    menu = run(submit(flow, of.STEP_STRATEGIES, {CONF_ZONE_ID: "living_room"}))
    assert menu["type"] == "menu"
    assert menu["step_id"] == of.STEP_ZONE_MENU
    assert menu["menu_options"] == [of.STEP_ZONE_DETAIL, of.STEP_SCHEDULE]


def test_strategies_selector_lists_every_room_with_its_name() -> None:
    flow = make_options_flow(
        entry_with(
            **{
                CONF_ZONES: [
                    zone_block(),
                    zone_block("climate.bedroom", "bedroom"),
                ]
            }
        ),
        build_hass(),
    )
    flow.config[CONF_ZONES][0][CONF_ZONE_NAME] = "Lounge"
    result = run(render(flow, of.STEP_STRATEGIES))
    (selector_field,) = result["data_schema"].schema.values()
    assert selector_field.config["options"] == [
        {"value": "living_room", "label": "Lounge"},
        {"value": "bedroom", "label": "Living Room"},
    ]
    assert selector_field.config["multiple"] is False


def test_strategies_rejects_an_unknown_room() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    with pytest.raises(vol.MultipleInvalid):
        run(submit(flow, of.STEP_STRATEGIES, {CONF_ZONE_ID: "kitchen"}))


def test_strategies_without_rooms_falls_through_to_the_zone_list() -> None:
    flow = make_options_flow(entry_with(**{CONF_ZONES: []}), build_hass())
    result = run(flow.async_step_strategies(None))
    expect_form(result, of.STEP_ZONES)


def test_zone_menu_without_a_selection_returns_to_the_room_picker() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(flow.async_step_zone_menu(None))
    expect_form(result, of.STEP_STRATEGIES)


def test_zone_detail_defaults_come_from_the_stored_block() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    result = run(render(flow, of.STEP_ZONE_DETAIL))
    defaults = result["data_schema"]({})
    assert defaults[CONF_ZONE_NAME] == "Living Room"
    assert defaults[CONF_ZONE_MODE] == ZONE_MODE_AUTO
    assert defaults[CONF_ZONE_MIN_TEMP] == 16.0
    assert defaults[CONF_ZONE_MAX_TEMP] == 26.0
    assert defaults[CONF_ZONE_STEP] == 0.5
    assert defaults[CONF_ZONE_TOLERANCE] == 0.2
    assert defaults[CONF_ZONE_OVERRIDE_POLICY] == OVERRIDE_NEXT_SCHEDULE
    assert defaults[CONF_ZONE_OVERRIDE_MINUTES] == 120
    assert defaults[CONF_ZONE_COLD_ADJUSTMENT] is False
    assert defaults[CONF_ZONE_AUX_LOAD] is False


def test_zone_detail_saves_every_editable_field() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_ZONE_DETAIL,
                {
                    CONF_ZONE_NAME: "Lounge",
                    CONF_ZONE_MODE: ZONE_MODE_COMFORT,
                    CONF_ZONE_MIN_TEMP: 18.0,
                    CONF_ZONE_MAX_TEMP: 24.0,
                    CONF_ZONE_STEP: 1.0,
                    CONF_ZONE_TOLERANCE: 0.3,
                    CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_FIXED_DURATION,
                    CONF_ZONE_OVERRIDE_MINUTES: 90,
                    CONF_ZONE_COLD_ADJUSTMENT: True,
                    CONF_ZONE_AUX_LOAD: True,
                },
            )
        )
    )
    zone = data[CONF_ZONES][0]
    assert zone[CONF_ZONE_NAME] == "Lounge"
    assert zone[CONF_ZONE_MODE] == ZONE_MODE_COMFORT
    assert zone[CONF_ZONE_MIN_TEMP] == 18.0
    assert zone[CONF_ZONE_MAX_TEMP] == 24.0
    assert zone[CONF_ZONE_STEP] == 1.0
    assert zone[CONF_ZONE_TOLERANCE] == 0.3
    assert zone[CONF_ZONE_OVERRIDE_POLICY] == OVERRIDE_FIXED_DURATION
    assert zone[CONF_ZONE_OVERRIDE_MINUTES] == 90
    assert zone[CONF_ZONE_COLD_ADJUSTMENT] is True
    assert zone[CONF_ZONE_AUX_LOAD] is True


def test_zone_detail_rejects_an_inverted_range() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    result = run(
        submit(
            flow,
            of.STEP_ZONE_DETAIL,
            {
                CONF_ZONE_NAME: "Living Room",
                CONF_ZONE_MODE: ZONE_MODE_AUTO,
                CONF_ZONE_MIN_TEMP: 26.0,
                CONF_ZONE_MAX_TEMP: 18.0,
                CONF_ZONE_STEP: 0.5,
                CONF_ZONE_TOLERANCE: 0.2,
                CONF_ZONE_OVERRIDE_POLICY: OVERRIDE_NEXT_SCHEDULE,
                CONF_ZONE_OVERRIDE_MINUTES: 120,
                CONF_ZONE_COLD_ADJUSTMENT: False,
                CONF_ZONE_AUX_LOAD: False,
            },
        )
    )
    assert result["errors"] == {"base": "invalid_range"}
    assert result["type"] == "form"


def test_blank_zone_name_falls_back_to_the_id() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    result = _edit_zone(flow, name="   ", mode=ZONE_MODE_AUTO)
    assert expect_saved(result)[CONF_ZONES][0][CONF_ZONE_NAME] == "living_room"


# ---------------------------------------------------------------------------
# schedule parsing
# ---------------------------------------------------------------------------
def test_schedule_form_shows_the_stored_text() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    result = run(render(flow, of.STEP_SCHEDULE))
    assert result["data_schema"]({})[CONF_ZONE_SLOTS] == (
        "06:00=22 / 09:00=19 / 17:00=22 / 23:00=20"
    )


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("06:00=22 / 09:00=19", 22.0),
        ("06:00=22\n09:00=19", 22.0),
        ("06:00=22;09:00=19", 22.0),
        ("  06:00=22 /  09:00=19  ", 22.0),
        ("6:00=22/9:00=19", 22.0),
        # Full-width equals, as produced by a Chinese IME (load-bearing: the
        # user's real-world input path is a Chinese keyboard).
        ("06:00\uff1d22 / 09:00\uff1d19", 22.0),
        ("06:00=21,5 / 09:00=19", 21.5),  # decimal comma
    ],
)
def test_schedule_separators_and_formats_are_accepted(text: str, first: float) -> None:
    slots = parse_schedule_text(text)
    assert [slot.at.strftime("%H:%M") for slot in slots] == ["06:00", "09:00"]
    assert slots[0].setpoint == first


@pytest.mark.parametrize(
    "text",
    [
        "06:00",  # no setpoint
        "06:00=",  # empty setpoint
        "=22",  # no time
        "abc=22",
        "25:00=22",  # hour out of range
        "06:70=22",  # minute out of range
        "06:00:00=22",
        "06=22",
        "06:00=abc",
    ],
)
def test_invalid_schedule_text_is_rejected(text: str) -> None:
    with pytest.raises(ScheduleFormatError):
        parse_schedule_text(text)


def test_schedule_saves_the_models_schedule_shape() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_SCHEDULE,
                {CONF_ZONE_SLOTS: "06:30=21.5 / 22:00=18.5"},
            )
        )
    )
    stored = data[CONF_ZONES][0][CONF_ZONE_SCHEDULE]
    assert stored == {
        "slots": [
            {"at": "06:30:00", "setpoint": 21.5, "days": []},
            {"at": "22:00:00", "setpoint": 18.5, "days": []},
        ]
    }
    # ... and must feed straight into the pure engine model.
    parsed = Schedule.from_dict(stored)
    assert [slot.at.strftime("%H:%M") for slot in parsed.slots] == ["06:30", "22:00"]
    assert parsed.slots[0].setpoint == 21.5


def test_invalid_schedule_shows_the_error_on_the_field() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    result = run(submit(flow, of.STEP_SCHEDULE, {CONF_ZONE_SLOTS: "06:00"}))
    assert result["type"] == "form"
    assert result["errors"] == {CONF_ZONE_SLOTS: "invalid_schedule"}


def test_schedule_slots_are_sorted_and_deduplicated() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_SCHEDULE,
                {CONF_ZONE_SLOTS: "23:00=20 / 06:00=22 / 06:00=23"},
            )
        )
    )
    slots = data[CONF_ZONES][0][CONF_ZONE_SCHEDULE]["slots"]
    assert [slot["at"] for slot in slots] == ["06:00:00", "23:00:00"]
    assert slots[0]["setpoint"] == 23.0  # last entry wins


def test_empty_schedule_falls_back_to_the_model_defaults() -> None:
    """``Schedule.from_dict`` treats "no slots" as "use DEFAULT_SLOTS"."""
    flow = make_options_flow(entry_with(), build_hass())
    flow._zone_id = "living_room"
    data = expect_saved(run(submit(flow, of.STEP_SCHEDULE, {CONF_ZONE_SLOTS: ""})))
    stored = data[CONF_ZONES][0][CONF_ZONE_SCHEDULE]
    assert Schedule.from_dict(stored).slots == DEFAULT_SLOTS
    assert stored == Schedule(slots=DEFAULT_SLOTS).to_dict()


def test_schedule_round_trips_through_text() -> None:
    slots = parse_schedule_text("06:00=22 / 09:00=19 / 17:00=22 / 23:00=20")
    text = of.format_schedule_text(slots)
    assert parse_schedule_text(text) == slots


# ---------------------------------------------------------------------------
# heat source
# ---------------------------------------------------------------------------
def test_heat_source_form_carries_the_stored_values() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(render(flow, of.STEP_HEAT_SOURCE))
    defaults = result["data_schema"]({})
    assert defaults[CONF_BOILER_ENTITY] == BOILER
    assert defaults[CONF_OUTDOOR_TEMP_ENTITY] == OUTDOOR
    assert defaults[CONF_OUTDOOR_STALE_MINUTES] == DEFAULT_WEATHER_STALE_AFTER
    assert defaults[CONF_WEATHER_MODE] == WEATHER_MODE_SUGGEST
    assert defaults[CONF_CURVE_OUTDOOR_DESIGN] == 0.0
    assert defaults[CONF_CURVE_SUPPLY_MAX] == 55.0
    assert defaults[CONF_VERIFY_HEATING_ONLY] is False
    assert defaults[CONF_VERIFY_SUPPLY_TEMP] is False
    assert defaults[CONF_VERIFY_MIN_FLOW] is False


def test_heat_source_saves_the_attestations_and_the_curve() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_HEAT_SOURCE,
                {
                    CONF_BOILER_ENTITY: BOILER,
                    CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
                    CONF_OUTDOOR_STALE_MINUTES: 120,
                    CONF_WEATHER_MODE: WEATHER_MODE_APPLY,
                    **CURVE,
                    CONF_VERIFY_HEATING_ONLY: True,
                    CONF_VERIFY_SUPPLY_TEMP: True,
                    CONF_VERIFY_MIN_FLOW: True,
                },
            )
        )
    )
    assert data[CONF_OUTDOOR_STALE_MINUTES] == 120
    assert data[CONF_WEATHER_MODE] == WEATHER_MODE_APPLY
    assert data[CONF_VERIFY_MIN_FLOW] is True
    assert data[CONF_WEATHER_CURVE]["supply_design"] == 45.0
    assert data[CONF_WEATHER_CURVE]["night_offset"] == 0.0


def test_heat_source_rejects_an_unknown_boiler() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(
        submit(
            flow,
            of.STEP_HEAT_SOURCE,
            {
                CONF_BOILER_ENTITY: "climate.ghost",
                CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
                CONF_OUTDOOR_STALE_MINUTES: 90,
                CONF_WEATHER_MODE: WEATHER_MODE_SUGGEST,
                **CURVE,
            },
        )
    )
    assert result["errors"] == {CONF_BOILER_ENTITY: "boiler_not_found"}


def test_heat_source_rejects_a_boiler_without_hvac_modes() -> None:
    hass = build_hass()
    hass.add_climate("climate.dumb", "heat")
    flow = make_options_flow(entry_with(), hass)
    result = run(
        submit(
            flow,
            of.STEP_HEAT_SOURCE,
            {
                CONF_BOILER_ENTITY: "climate.dumb",
                CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
                CONF_OUTDOOR_STALE_MINUTES: 90,
                CONF_WEATHER_MODE: WEATHER_MODE_SUGGEST,
                **CURVE,
            },
        )
    )
    assert result["errors"] == {CONF_BOILER_ENTITY: "no_hvac_modes"}


def test_heat_source_rejects_an_unknown_outdoor_sensor() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(
        submit(
            flow,
            of.STEP_HEAT_SOURCE,
            {
                CONF_BOILER_ENTITY: BOILER,
                CONF_OUTDOOR_TEMP_ENTITY: "sensor.ghost",
                CONF_OUTDOOR_STALE_MINUTES: 90,
                CONF_WEATHER_MODE: WEATHER_MODE_SUGGEST,
                **CURVE,
            },
        )
    )
    assert result["errors"] == {CONF_OUTDOOR_TEMP_ENTITY: "outdoor_not_found"}


def test_heat_source_accepts_a_missing_outdoor_sensor() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_HEAT_SOURCE,
                {
                    CONF_BOILER_ENTITY: BOILER,
                    CONF_OUTDOOR_TEMP_ENTITY: None,
                    CONF_OUTDOOR_STALE_MINUTES: 90,
                    CONF_WEATHER_MODE: WEATHER_MODE_SUGGEST,
                    **CURVE,
                },
            )
        )
    )
    assert data[CONF_OUTDOOR_TEMP_ENTITY] is None


def test_heat_source_rejects_an_invalid_curve() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(
        submit(
            flow,
            of.STEP_HEAT_SOURCE,
            {
                CONF_BOILER_ENTITY: BOILER,
                CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
                CONF_OUTDOOR_STALE_MINUTES: 90,
                CONF_WEATHER_MODE: WEATHER_MODE_SUGGEST,
                **{**CURVE, CONF_CURVE_SUPPLY_MAX: 30.0},
            },
        )
    )
    assert result["errors"] == {"base": "invalid_curve"}


def test_curve_validation_rules() -> None:
    assert of.validate_curve(CURVE) is None
    assert of.validate_curve({**CURVE, CONF_CURVE_OUTDOOR_STOP: 0.0}) == "invalid_curve"
    assert of.validate_curve({**CURVE, CONF_CURVE_SUPPLY_MAX: 35.0}) == "invalid_curve"
    assert of.validate_curve({**CURVE, CONF_CURVE_SUPPLY_DESIGN: 99.0}) == "invalid_curve"
    assert of.validate_curve({}) == "invalid_curve"


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------
def test_diagnostics_defaults() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    result = run(render(flow, of.STEP_DIAGNOSTICS))
    assert result["data_schema"]({}) == {
        CONF_SHORT_CYCLE_SECONDS: float(DEFAULT_SHORT_CYCLE_SECONDS),
        CONF_STARTUP_GRACE: float(DEFAULT_STARTUP_GRACE),
        CONF_PERIODIC_AUDIT: float(DEFAULT_PERIODIC_AUDIT),
    }


def test_diagnostics_saves_the_tuning_values() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(
        run(
            submit(
                flow,
                of.STEP_DIAGNOSTICS,
                {
                    CONF_SHORT_CYCLE_SECONDS: 600,
                    CONF_STARTUP_GRACE: 300,
                    CONF_PERIODIC_AUDIT: 1800,
                },
            )
        )
    )
    assert data[CONF_SHORT_CYCLE_SECONDS] == 600.0
    assert data[CONF_STARTUP_GRACE] == 300.0
    assert data[CONF_PERIODIC_AUDIT] == 1800.0


def test_diagnostics_reflects_stored_values() -> None:
    entry = entry_with(
        **{
            CONF_SHORT_CYCLE_SECONDS: 420.0,
            CONF_STARTUP_GRACE: 60.0,
            CONF_PERIODIC_AUDIT: 900.0,
        }
    )
    flow = make_options_flow(entry, build_hass())
    result = run(render(flow, of.STEP_DIAGNOSTICS))
    defaults = result["data_schema"]({})
    assert defaults[CONF_SHORT_CYCLE_SECONDS] == 420.0
    assert defaults[CONF_STARTUP_GRACE] == 60.0
    assert defaults[CONF_PERIODIC_AUDIT] == 900.0


# ---------------------------------------------------------------------------
# saving semantics
# ---------------------------------------------------------------------------
def test_saving_preserves_unrelated_stored_keys() -> None:
    """A step must write the whole merged config, never drop other keys."""
    entry = entry_with(**{CONF_PERIODIC_AUDIT: 900.0})
    flow = make_options_flow(entry, build_hass())
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: [LIVING]})))
    assert data[CONF_PERIODIC_AUDIT] == 900.0
    assert data[CONF_SYSTEM_NAME] == "Home"
    assert data[CONF_BOILER_ENTITY] == BOILER


def test_every_saved_zone_block_stays_valid_for_the_engine() -> None:
    flow = make_options_flow(entry_with(), build_hass())
    data = expect_saved(run(submit(flow, of.STEP_ZONES, {CONF_ZONES: [LIVING, "climate.bedroom"]})))
    for zone in data[CONF_ZONES]:
        assert set(zone) == set(zone_block())
        assert isinstance(zone[CONF_ZONE_NAME], str) and zone[CONF_ZONE_NAME]
        assert Schedule.from_dict(zone[CONF_ZONE_SCHEDULE]).slots
        assert zone[CONF_ZONE_MIN_TEMP] < zone[CONF_ZONE_MAX_TEMP]
        assert zone[CONF_ZONE_OVERRIDE_POLICY] in (
            OVERRIDE_NEXT_SCHEDULE,
            OVERRIDE_FIXED_DURATION,
            OVERRIDE_INDEFINITE,
        )
