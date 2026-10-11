"""Real-coordinator smoke tests.

Every other test in this suite exercises an engine, a platform or an adapter in
isolation. These build an actual :class:`ClimatePilotCoordinator` on a **real**
Home Assistant instance and run complete evaluation cycles through it.

That distinction matters: three defects reached this repository that no unit
test caught, because each lived in the *wiring* rather than in any single module

* ``HeatSourceConfig.from_dict`` did not exist, so the coordinator's constructor
  raised ``AttributeError`` the moment a config entry was set up;
* ``_apply_capabilities`` bypassed the P0 range intersection, silently widening
  a user's configured room limits back to the device range;
* the coordinator did not pass ``config_entry`` to ``DataUpdateCoordinator``,
  a pattern Home Assistant reports and removes in 2026.8.

All three are only visible when the real object is constructed and driven.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.const import (
    CONF_AUX_LOAD,
    CONF_BOILER_ENTITY,
    CONF_BURNER_STATE_ENTITY,
    CONF_CONTROL_MODE,
    CONF_HEAT_SOURCE,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_SYSTEM_NAME,
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_MIN_FLOW,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_WEATHER_CURVE,
    CONF_WEATHER_MODE,
    CONF_ZONES,
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    FEEDBACK_UNAVAILABLE,
    WEATHER_MODE_APPLY,
)
from custom_components.climatepilot.coordinator import (
    ClimatePilotCoordinator,
)

LIVING = "climate.livingroom_floorheating"
MASTER = "climate.master_bedroom_floorheating"
GUEST = "climate.guest_bedroom_floorheating"
BOILER = "climate.rinnai_heating_control"
BURNER = "sensor.heating_burning_state"
OUTDOOR = "sensor.heweather_outside_temperature"

T0 = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Real Home Assistant instance
# ---------------------------------------------------------------------------


@pytest.fixture()
async def hass(tmp_path):
    """A real, minimal HomeAssistant with the frame helper installed.

    A genuine ``HomeAssistant`` is used rather than a mock so the coordinator's
    real wiring is exercised - the defects this module exists to catch (missing
    ``from_dict``, an unsettable property, a ContextVar-only config entry) only
    appear against the real objects.

    ``climate.set_temperature`` / ``set_hvac_mode`` are registered with a spy
    handler so writes are observable without loading the climate integration.
    """
    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)

    recorded: list[tuple[str, str, dict]] = []

    async def _spy(call):
        recorded.append((call.domain, call.service, dict(call.data)))

    instance.services.async_register("climate", "set_temperature", _spy)
    instance.services.async_register("climate", "set_hvac_mode", _spy)
    instance._climatepilot_calls = recorded
    try:
        yield instance
    finally:
        await instance.async_stop()


def set_state(hass, entity_id, state, attributes=None):
    hass.states.async_set(entity_id, state, attributes or {})


def install_entities(hass):
    """Install the production entity set."""
    hass.states.async_set(
        LIVING,
        "heat",
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 16.0,
            "max_temp": 26.0,
            "target_temp_step": 0.5,
            "current_temperature": 21.0,
            "temperature": 19.0,
            "hvac_action": "heating",
            "supported_features": 385,
        },
    )
    hass.states.async_set(
        MASTER,
        "heat",
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 16.0,
            "max_temp": 26.0,
            "target_temp_step": 0.5,
            "current_temperature": 20.5,
            "temperature": 19.0,
            "hvac_action": "heating",
            "supported_features": 385,
        },
    )
    hass.states.async_set(
        GUEST,
        "off",
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 16.0,
            "max_temp": 26.0,
            "target_temp_step": 0.5,
            "current_temperature": 19.0,
            "temperature": None,
            "hvac_action": "off",
            "supported_features": 385,
        },
    )
    hass.states.async_set(
        BOILER,
        "heat",
        {
            "hvac_modes": ["heat", "auto", "dry", "off"],
            "min_temp": 40,
            "max_temp": 82,
            "target_temp_step": 1,
            "current_temperature": None,
            "temperature": 40,
            "hvac_action": "heating",
            "supported_features": 1,
        },
    )
    hass.states.async_set(BURNER, "Standby", {})
    hass.states.async_set(
        OUTDOOR,
        "8.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )


def make_entry(options: dict) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "smoke-entry"
    entry.version = 1
    entry.minor_version = 1
    entry.title = "ClimatePilot"
    entry.data = {CONF_SYSTEM_NAME: "我的家"}
    entry.options = options
    return entry


def full_options(**overrides) -> dict:
    options = {
        CONF_SYSTEM_NAME: "我的家",
        CONF_BOILER_ENTITY: BOILER,
        CONF_BURNER_STATE_ENTITY: BURNER,
        CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
        CONF_OUTDOOR_STALE_MINUTES: 90,
        CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE,
        CONF_WEATHER_MODE: WEATHER_MODE_APPLY,
        CONF_WEATHER_CURVE: {
            "outdoor_design": 0.0,
            "supply_design": 45.0,
            "outdoor_stop": 15.0,
            "supply_eco": 40.0,
            "supply_min": 35.0,
            "supply_max": 55.0,
        },
        CONF_HEAT_SOURCE: {"min_on_time": 300, "pump_overrun": 180},
        CONF_AUX_LOAD: {"enabled": True, "delta": 0.5, "max_temperature": 24.0},
        CONF_VERIFY_HEATING_ONLY: False,
        CONF_VERIFY_SUPPLY_TEMP: False,
        CONF_VERIFY_MIN_FLOW: False,
        CONF_ZONES: [
            {
                "zone_id": "living_room",
                "entity_id": LIVING,
                "name": "客厅地暖",
                "min_temp": 16.0,
                "max_temp": 26.0,
                "target_step": 0.5,
                "tolerance": 0.2,
                "schedule": {"slots": [{"at": "06:00:00", "setpoint": 22.0, "days": []}]},
            },
            {
                "zone_id": "master_bedroom",
                "entity_id": MASTER,
                "name": "主卧地暖",
                "min_temp": 16.0,
                "max_temp": 26.0,
                "schedule": {"slots": [{"at": "06:00:00", "setpoint": 22.0, "days": []}]},
            },
        ],
    }
    options.update(overrides)
    return options


def build(hass, **overrides):
    """Construct a real coordinator over a real HomeAssistant."""
    install_entities(hass)
    entry = make_entry(full_options(**overrides))
    coordinator = ClimatePilotCoordinator(hass, entry)
    # Keep tests off the real event loop plumbing.
    coordinator.async_shutdown_listeners = MagicMock()
    return coordinator, entry


async def run_cycle(coordinator, when=T0):
    """Run one full evaluation cycle with the clock pinned to ``when``."""
    with (
        patch.object(coordinator, "_async_restore_state", AsyncMock()),
        patch.object(coordinator, "_async_schedule_save", MagicMock()),
        patch.object(coordinator, "_now", return_value=when),
    ):
        await coordinator._async_update_data()


def pin_clock(coordinator, when=T0):
    """Pin the coordinator's clock so grace/override maths is deterministic."""
    coordinator._now = lambda: when


def calls_to(hass, domain):
    """Service calls recorded by the spy handler, as (service, data) pairs."""
    return [
        (service, data)
        for called_domain, service, data in getattr(hass, "_climatepilot_calls", [])
        if called_domain == domain
    ]


# ---------------------------------------------------------------------------
# Construction - the wiring bugs unit tests missed
# ---------------------------------------------------------------------------


async def test_coordinator_constructs_with_a_full_configuration(hass):
    """Regression: HeatSourceConfig.from_dict was missing, breaking setup."""
    coordinator, _ = build(hass)
    assert coordinator.system_id == "smoke-entry"
    assert coordinator.name == "我的家"
    assert set(coordinator.zones) == {"living_room", "master_bedroom"}
    assert coordinator.state_machine.capabilities is not None
    # The config entry must be attached, not inferred from a ContextVar.
    assert coordinator.config_entry is not None


async def test_coordinator_constructs_with_an_empty_configuration(hass):
    """A minimal entry (no zones, no heat source) must not crash either."""
    entry = make_entry({CONF_SYSTEM_NAME: "空系统"})
    coordinator = ClimatePilotCoordinator(hass, entry)
    assert coordinator.zones == {}
    assert coordinator.state_machine.capabilities is None
    assert coordinator.active_zone_count == 0


async def test_coordinator_constructs_when_entities_are_missing(hass):
    """A configured entity that no longer exists must degrade, not raise."""
    entry = make_entry(full_options())
    coordinator = ClimatePilotCoordinator(hass, entry)
    assert coordinator.boiler_entity == BOILER
    assert coordinator.zones["living_room"].feedback_quality == FEEDBACK_UNAVAILABLE


async def test_coordinator_accepts_a_real_config_entry_object(hass):
    """The coordinator must work with a genuine ConfigEntry, not just a mock.

    This is what proves ``config_entry=entry`` is accepted by the installed
    Home Assistant: a MagicMock would swallow any signature mismatch.
    """
    from homeassistant.config_entries import ConfigEntry

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="climatepilot",
        title="ClimatePilot",
        data={CONF_SYSTEM_NAME: "我的家"},
        options=full_options(),
        source="user",
        unique_id=None,
        discovery_keys={},
        subentries_data={},
    )
    install_entities(hass)
    coordinator = ClimatePilotCoordinator(hass, entry)
    assert coordinator.config_entry is entry
    assert coordinator.system_id == entry.entry_id
    assert coordinator.name == "我的家"


# ---------------------------------------------------------------------------
# Real cycles
# ---------------------------------------------------------------------------


async def test_observe_mode_never_writes(hass):
    coordinator, _ = build(hass)
    coordinator.control_mode = CONTROL_MODE_OBSERVE

    await run_cycle(coordinator)

    assert calls_to(hass, "climate") == []
    # Decisions are still computed and explainable while observing.
    assert coordinator.zone_decisions
    assert coordinator.compensation is not None


async def test_zone_writes_require_coordinate_mode_and_grace(hass):
    coordinator, _ = build(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE})
    # Startup grace has not elapsed yet.
    coordinator._started_at = T0
    pin_clock(coordinator, T0 + timedelta(seconds=1))
    assert coordinator.writes_enabled is False

    await run_cycle(coordinator, T0 + timedelta(seconds=1))

    assert calls_to(hass, "climate") == []


async def test_coordinate_mode_writes_a_clamped_setpoint(hass):
    """Past the grace period, a coordinate-mode cycle reaches the entity."""
    coordinator, _ = build(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE})
    coordinator._started_at = T0 - timedelta(hours=1)
    pin_clock(coordinator)

    await run_cycle(coordinator)

    writes = calls_to(hass, "climate")
    assert writes, "a coordinate-mode cycle must write the room setpoints"
    for service, data in writes:
        assert service == "set_temperature"
        assert 16.0 <= data["temperature"] <= 26.0, "written targets stay inside P0"


async def test_heat_source_untouched_while_semantics_unverified(hass):
    """The headline safety gate: unverified semantics means no boiler writes."""
    coordinator, _ = build(hass, **{CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE})
    coordinator._started_at = T0 - timedelta(hours=1)
    pin_clock(coordinator)

    caps = coordinator.state_machine.capabilities
    assert caps.semantics_verified is False
    assert coordinator.heat_source_enabled is False

    await run_cycle(coordinator)

    for service, data in calls_to(hass, "climate"):
        assert service != "set_hvac_mode", "the heat source must not be started or stopped"
        assert data.get("entity_id") != BOILER, (
            "no write may reach the boiler while its semantics are unverified"
        )


async def test_heat_source_is_driven_once_semantics_are_verified(hass):
    """With all three attestations the boiler is finally commanded."""
    coordinator, _ = build(
        hass,
        **{
            CONF_VERIFY_HEATING_ONLY: True,
            CONF_VERIFY_SUPPLY_TEMP: True,
            CONF_VERIFY_MIN_FLOW: True,
            CONF_CONTROL_MODE: CONTROL_MODE_COORDINATE,
        },
    )
    coordinator._started_at = T0 - timedelta(hours=1)
    pin_clock(coordinator)
    assert coordinator.state_machine.capabilities.semantics_verified is True
    assert coordinator.heat_source_enabled is True

    for offset in range(0, 600, 30):
        await run_cycle(coordinator, T0 + timedelta(seconds=offset))

    boiler_writes = [
        (service, data)
        for service, data in calls_to(hass, "climate")
        if data.get("entity_id") == BOILER
    ]
    assert boiler_writes, "a verified system must drive its heat source"
    assert coordinator.heat_decision is not None


async def test_cycle_survives_entity_outage(hass):
    """A full cycle with every entity unavailable must not raise."""
    coordinator, _ = build(hass)
    for entity in (LIVING, MASTER, BOILER, OUTDOOR):
        set_state(hass, entity, "unavailable", {})

    await run_cycle(coordinator)

    assert coordinator.unknown_zone_count == 2
    assert coordinator.active_zone_count == 0
    assert coordinator.degraded is True


# ---------------------------------------------------------------------------
# Range semantics through the real object
# ---------------------------------------------------------------------------


async def test_user_narrowed_range_survives_real_cycles(hass):
    """Regression: the sync must not widen a user's configured room limits."""
    coordinator, _ = build(hass)
    zone = coordinator.zones["living_room"]
    zone.config_min_temp = 18.0
    zone.config_max_temp = 24.0
    zone.refresh_limits()
    assert (zone.min_temp, zone.max_temp) == (18.0, 24.0)

    for offset in range(3):
        await run_cycle(coordinator, T0 + timedelta(seconds=offset * 60))

    assert (zone.min_temp, zone.max_temp) == (18.0, 24.0)


async def test_zone_state_reflects_real_device_values(hass):
    coordinator, _ = build(hass)
    await run_cycle(coordinator)

    zone = coordinator.zones["living_room"]
    assert zone.actual_setpoint == 19.0
    assert zone.current_temperature == 21.0
    assert zone.hvac_action == "heating"
    assert zone.target_step == 0.5
    assert zone.feedback_quality == "valid"


async def test_weather_compensation_runs_in_a_real_cycle(hass):
    coordinator, _ = build(hass)
    await run_cycle(coordinator)

    compensation = coordinator.compensation
    assert compensation is not None
    assert compensation.outdoor == 8.0
    # 0 degrees -> 45, 15 degrees -> 40, so 8 sits between them.
    assert compensation.suggested is not None
    assert 40.0 <= compensation.suggested <= 45.0


async def test_stale_outdoor_reading_never_becomes_zero(hass):
    """Design doc 7: an unusable reading keeps the last confirmed value."""
    coordinator, _ = build(hass)
    await run_cycle(coordinator)
    first = coordinator.compensation.suggested
    assert first is not None

    set_state(hass, OUTDOOR, "unavailable", {})
    await run_cycle(coordinator, T0 + timedelta(minutes=1))

    # It must not suddenly believe it is 0 degrees outside.
    assert coordinator.compensation.suggested == first


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def test_resume_auto_clears_overrides(hass):
    coordinator, _ = build(hass)
    zone = coordinator.zones["living_room"]
    coordinator.arbiter.note_external_change(zone, 21.5, T0, policy="indefinite")
    assert zone.manual_override is not None

    with patch.object(coordinator, "_async_save_state", AsyncMock()):
        await coordinator.async_resume_auto("living_room")
    assert zone.manual_override is None


async def test_set_zone_mode_to_auto_clears_the_override(hass):
    coordinator, _ = build(hass)
    zone = coordinator.zones["master_bedroom"]
    coordinator.arbiter.note_external_change(zone, 20.0, T0, policy="indefinite")
    await coordinator.async_set_zone_mode("master_bedroom", "eco")
    assert zone.manual_override is None
    assert zone.mode == "eco"


async def test_supply_request_refused_while_unverified(hass):
    """A manual supply request must respect the same gate as the FSM."""
    coordinator, _ = build(hass)
    await coordinator.async_set_supply_temperature(50.0)
    assert calls_to(hass, "climate") == []


async def test_aux_load_slot_is_released_when_disabled(hass):
    coordinator, _ = build(hass)
    zone = coordinator.zones["living_room"]
    zone.aux_load_enabled = True
    coordinator.aux_state.zone_switch_at["living_room"] = T0
    coordinator.aux_state.active = True

    await coordinator.async_set_zone_aux_load("living_room", False)
    assert zone.aux_load_enabled is False
    assert "living_room" not in coordinator.aux_state.zone_switch_at
    assert coordinator.aux_state.active is False


async def test_reconfigure_preserves_a_manual_override(hass):
    """Changing options must not silently discard the user's takeover."""
    coordinator, entry = build(hass)
    zone = coordinator.zones["living_room"]
    coordinator.arbiter.note_external_change(zone, 21.5, T0, policy="indefinite")

    new_options = full_options()
    new_options[CONF_SYSTEM_NAME] = "改名后的系统"
    entry.options = new_options
    await coordinator.async_reconfigure()

    assert coordinator.name == "改名后的系统"
    assert coordinator.zones["living_room"].manual_override is not None
    assert coordinator.zones["living_room"].manual_override.value == 21.5


async def test_state_payload_is_serialisable_and_complete(hass):
    coordinator, _ = build(hass)
    coordinator.arbiter.note_external_change(
        coordinator.zones["living_room"], 21.5, T0, policy="fixed_duration"
    )
    payload = coordinator._state_payload()

    assert set(payload) >= {
        "zones",
        "aux_load",
        "zone_coordinator",
        "heat_source",
        "weather",
        "burner",
        "control_mode",
    }
    import json

    json.dumps(payload)  # must be JSON-serialisable for Store
    assert payload["zones"]["living_room"]["manual_override"]["value"] == 21.5
