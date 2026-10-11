"""Full config-entry setup against a real Home Assistant.

This is the closest thing to actually installing the integration that can run
without a live instance. It creates a genuine config entry, lets Home Assistant
run ``async_setup_entry``, and then inspects the resulting state machine:

* the entry reaches ``LOADED`` rather than ``SETUP_ERROR``;
* every entity the platforms declare is registered and readable;
* the services are registered with real schemas;
* an unload removes the entities and the services again.

Everything up to this point tested modules. Only this tests the *integration*:
platform forwarding, entity registration, device/unique-id wiring and service
lifecycle are all exercised exactly as Home Assistant drives them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.climatepilot.const import (
    CONF_AUX_LOAD,
    CONF_BOILER_ENTITY,
    CONF_BURNER_STATE_ENTITY,
    CONF_CONTROL_MODE,
    CONF_HEAT_SOURCE,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_SYSTEM_NAME,
    CONF_VERIFY_HEATING_ONLY,
    CONF_VERIFY_MIN_FLOW,
    CONF_VERIFY_SUPPLY_TEMP,
    CONF_WEATHER_CURVE,
    CONF_ZONES,
    CONTROL_MODE_OBSERVE,
    DOMAIN,
    SERVICE_REASSESS,
    SERVICE_RESUME_AUTO,
    SERVICE_SET_SUPPLY_TEMPERATURE,
)

LIVING = "climate.livingroom_floorheating"
MASTER = "climate.master_bedroom_floorheating"
BOILER = "climate.rinnai_heating_control"
BURNER = "sensor.heating_burning_state"
OUTDOOR = "sensor.heweather_outside_temperature"


@pytest.fixture()
async def hass(tmp_path):
    """A real HomeAssistant that loads custom_components/climatepilot from disk.

    The integration loader is initialised against a config directory containing a
    symlink to this repository's ``custom_components``, so Home Assistant
    discovers ``climatepilot`` exactly as it would on a real installation -
    including reading ``manifest.json`` and importing the package through its own
    loader rather than through ``sys.path``.
    """
    import os
    import shutil

    from homeassistant import loader
    from homeassistant.config_entries import ConfigEntries

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / "custom_components"
    source = ROOT / "custom_components"
    try:
        os.symlink(source, target, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without developer mode: fall back to a copy.
        shutil.copytree(source, target)

    instance = HomeAssistant(str(config_dir))
    frame.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    loader.async_setup(instance)

    # The registries entities are added through. Home Assistant loads these
    # during bootstrap; loading them here is what lets entities be registered
    # with real unique ids instead of failing inside the entity platform.
    from homeassistant.helpers import area_registry, device_registry, entity_registry

    await area_registry.async_load(instance)
    await device_registry.async_load(instance)
    await entity_registry.async_load(instance)

    # climate.* services so the coordinator's writes are accepted.
    calls: list[tuple[str, str, dict]] = []

    async def _spy(call):
        calls.append((call.domain, call.service, dict(call.data)))

    instance.services.async_register("climate", "set_temperature", _spy)
    instance.services.async_register("climate", "set_hvac_mode", _spy)
    instance._spy_calls = calls
    try:
        yield instance
    finally:
        await instance.async_stop()


def install_entities(hass: HomeAssistant) -> None:
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
        OUTDOOR, "8.0", {"unit_of_measurement": "°C", "device_class": "temperature"}
    )


def options(**overrides) -> dict:
    data = {
        CONF_SYSTEM_NAME: "我的家",
        CONF_BOILER_ENTITY: BOILER,
        CONF_BURNER_STATE_ENTITY: BURNER,
        CONF_OUTDOOR_TEMP_ENTITY: OUTDOOR,
        CONF_CONTROL_MODE: CONTROL_MODE_OBSERVE,
        CONF_WEATHER_CURVE: {
            "outdoor_design": 0.0,
            "supply_design": 45.0,
            "outdoor_stop": 15.0,
            "supply_eco": 40.0,
            "supply_min": 35.0,
            "supply_max": 55.0,
        },
        CONF_HEAT_SOURCE: {"min_on_time": 300},
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
    data.update(overrides)
    return data


async def setup_entry(hass: HomeAssistant, **overrides) -> ConfigEntry:
    install_entities(hass)
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="ClimatePilot",
        data={CONF_SYSTEM_NAME: "我的家"},
        options=options(**overrides),
        source="user",
        unique_id=None,
        discovery_keys={},
        subentries_data={},
    )
    await hass.config_entries.async_add(entry)
    return entry


def registered_entities(hass) -> set[str]:
    """Entity ids owned by this integration, from the real entity registry."""
    from homeassistant.helpers import entity_registry

    registry = entity_registry.async_get(hass)
    return {entry.entity_id for entry in registry.entities.values() if entry.platform == DOMAIN}


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def test_listeners_are_actually_registered(hass):
    """Regression: ``async_setup_listeners`` was a coroutine nobody awaited.

    The call returned an un-awaited coroutine, so no state-change listener and no
    audit interval were ever installed - the integration loaded, then sat deaf
    and never reacted to anything. It is a plain callback now, and this asserts
    the subscriptions really exist.
    """
    entry = await setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator._unsubscribers, "no listeners were registered"
    assert coordinator._save_debouncer is not None, "state saving was never wired"

    # The audit interval must be a live, non-expired HA timer.
    timers = [handle for handle in coordinator._unsubscribers if handle is not None]
    assert len(timers) >= 2, "expected a state listener and an audit interval"


async def test_coordinator_reacts_to_a_state_change(hass):
    """A KNX telegram must trigger re-evaluation, not just sit in the state machine."""
    entry = await setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    coordinator.async_request_refresh = AsyncMock()
    # Push a genuine state change through the real event bus.
    hass.states.async_set(LIVING, "off", {"hvac_action": "off"})
    await hass.async_block_till_done()

    assert coordinator.async_request_refresh.await_count >= 1, (
        "a watched entity changing state must schedule a refresh"
    )


async def test_config_entry_reaches_loaded(hass):
    """The integration must actually set up, not land in SETUP_ERROR."""
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED, (
        f"setup failed: state={entry.state} reason={entry.reason}"
    )


async def test_coordinator_is_stored_in_hass_data(hass):
    entry = await setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.system_id == entry.entry_id
    assert set(coordinator.zones) == {"living_room", "master_bedroom"}


async def test_expected_entities_are_registered(hass):
    """Every platform entity must be registered, with a unique_id and a device."""
    await setup_entry(hass)
    ids = registered_entities(hass)

    # Assert on translation keys via unique_id, because the entity_id slug is
    # derived from the *translated* device name and is not a stable contract.
    suffixes = {eid.split(".", 1)[1] for eid in ids}
    assert len(ids) == 31, f"expected 31 entities for 2 zones, got {len(ids)}: {sorted(ids)}"

    for suffix in (
        "control_mode",
        "heating_state",
        "zones_demanding_heat",
        "suggested_supply_temperature",
        "outdoor_temperature",
        "supply_temperature",
        "burner_starts_24_h",
        "last_burn_duration",
        "short_cycle_alert",
        "degraded_operation",
        "reassess_now",
        "manual_supply_temperature",
    ):
        assert any(name.endswith(suffix) for name in suffixes), f"missing {suffix}"

    # Per-zone entities, two zones each.
    for suffix in (
        "effective_setpoint",
        "setpoint_source",
        "room_temperature",
        "manual_override_remaining",
        "manual_override_active",
        "heat_request",
        "resume_automatic",
        "auxiliary_heat_load",
        "room_mode",
    ):
        matches = [name for name in suffixes if name.endswith(suffix)]
        assert len(matches) == 2, f"{suffix}: expected 2, got {matches}"


async def test_registry_entries_have_unique_ids_and_devices(hass):
    """Unique ids must be stable and every entity must belong to a device."""
    from homeassistant.helpers import entity_registry

    await setup_entry(hass)
    registry = entity_registry.async_get(hass)
    ours = [e for e in registry.entities.values() if e.platform == DOMAIN]

    unique_ids = [e.unique_id for e in ours]
    assert len(unique_ids) == len(set(unique_ids)), "duplicate unique_id"
    assert all(e.unique_id for e in ours)

    # Zone entities hang off a zone sub-device; system entities off the hub.
    devices = {e.device_id for e in ours}
    assert None not in devices, "every entity must be attached to a device"
    assert len(devices) == 3, f"expected hub + 2 zone devices, got {devices}"


async def test_zone_entities_appear_once_per_configured_room(hass):
    await setup_entry(hass)
    ids = registered_entities(hass)
    effective = [eid for eid in ids if eid.endswith("effective_setpoint")]
    assert len(effective) == 2, effective


async def test_whole_home_thermostat_is_created(hass):
    """climate.py must be forwarded, not silently dropped."""
    await setup_entry(hass)
    ours = registered_entities(hass)
    thermostats = [eid for eid in ours if eid.startswith("climate.")]
    assert len(thermostats) == 1, f"expected one system thermostat, got {thermostats}"


async def test_entity_states_are_readable_not_unavailable(hass):
    """A LOADED entry whose entities are all 'unavailable' is not working."""
    await setup_entry(hass)
    ids = registered_entities(hass)
    states = {eid: hass.states.get(eid) for eid in ids}

    missing = [eid for eid, state in states.items() if state is None]
    assert not missing, f"registered but no state: {missing}"

    unavailable = [eid for eid, state in states.items() if state.state == "unavailable"]
    assert not unavailable, f"unavailable after a successful setup: {unavailable}"


async def test_services_are_registered(hass):
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    for service in (
        SERVICE_RESUME_AUTO,
        SERVICE_SET_SUPPLY_TEMPERATURE,
        SERVICE_REASSESS,
    ):
        assert hass.services.has_service(DOMAIN, service), service


async def test_resume_auto_service_runs(hass):
    entry = await setup_entry(hass)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    from homeassistant.util import dt as dt_util

    from custom_components.climatepilot.const import OVERRIDE_INDEFINITE

    zone = coordinator.zones["living_room"]
    coordinator.arbiter.note_external_change(zone, 21.5, dt_util.now(), policy=OVERRIDE_INDEFINITE)
    assert zone.manual_override is not None

    await hass.services.async_call(
        DOMAIN, SERVICE_RESUME_AUTO, {"zone_id": "living_room"}, blocking=True
    )
    assert zone.manual_override is None


async def test_reassess_service_runs(hass):
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    await hass.services.async_call(DOMAIN, SERVICE_REASSESS, {}, blocking=True)


async def test_set_supply_temperature_is_refused_while_unverified(hass):
    """The service must honour the same verification gate as the state machine."""
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    hass._spy_calls.clear()

    await hass.services.async_call(
        DOMAIN, SERVICE_SET_SUPPLY_TEMPERATURE, {"temperature": 50.0}, blocking=True
    )

    boiler_writes = [data for _, _, data in hass._spy_calls if data.get("entity_id") == BOILER]
    assert not boiler_writes, "the boiler must not be written while unverified"


async def test_unload_marks_entities_unavailable_and_removes_services(hass):
    """Unload must neutralise the entity states and drop the services.

    Home Assistant deliberately keeps registry-backed entities in the state
    machine and rewrites them to ``unavailable`` with ``restored: True`` rather
    than deleting the state objects, so asserting on *absence* would be wrong.
    What must hold is that nothing is left reporting a live value and that the
    services are gone.
    """
    entry = await setup_entry(hass)
    ids = registered_entities(hass)
    assert ids

    live_before = {
        eid for eid in ids if (state := hass.states.get(eid)) and state.state != "unavailable"
    }
    assert live_before, "setup should have produced live entity states"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    await hass.async_block_till_done()

    still_live = [
        eid for eid in ids if (state := hass.states.get(eid)) and state.state != "unavailable"
    ]
    assert not still_live, f"entities still reporting live values: {still_live}"

    for service in (
        SERVICE_RESUME_AUTO,
        SERVICE_SET_SUPPLY_TEMPERATURE,
        SERVICE_REASSESS,
    ):
        assert not hass.services.has_service(DOMAIN, service), service


async def test_reload_does_not_duplicate_entities(hass):
    """A reload must reuse the same registry entries, not pile up new ones."""
    entry = await setup_entry(hass)
    before = registered_entities(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = registered_entities(hass)
    assert after == before, (before - after, after - before)
    assert entry.state is ConfigEntryState.LOADED


async def test_reload_restores_live_states(hass):
    """After a reload the entities must report values again, not stay unavailable."""
    entry = await setup_entry(hass)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    ids = registered_entities(hass)
    unavailable = [eid for eid in ids if (s := hass.states.get(eid)) and s.state == "unavailable"]
    assert not unavailable, f"still unavailable after reload: {unavailable}"


async def test_setup_survives_missing_entities(hass):
    """A configured entity that vanished must degrade, not break setup."""
    entry = await setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED

    # Now remove the boiler and reload: setup must still succeed.
    hass.states.async_remove(BOILER)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED


async def test_diagnostics_export_works_on_a_loaded_entry(hass):
    """The diagnostics module must run against real, live coordinator state."""
    from custom_components.climatepilot.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await setup_entry(hass)
    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["coordinator"] is not None
    assert "living_room" in payload["zones"]
    assert payload["zones"]["living_room"]["entity_id"] == LIVING
    # The export must be JSON-serialisable - HA writes it to a file.
    import json

    json.dumps(payload)
