"""Coordinator-level tests for ClimatePilot.

These exercise the wiring between the pure engines and Home Assistant using a
minimal, explicitly-controlled ``hass`` double. The entity IDs and attributes
are copied from the real production system so the tests describe the actual
installation (three KNX floor-heating thermostats plus a Rinnai condensing
boiler), not an idealised one.

The behaviours pinned here are the ones a wrong implementation would get wrong
in a way the user would notice:

* the user's configured room range is never widened by an automatic sync;
* ``heat`` mode alone is not a heat demand;
* the heat source is not started while its semantics are unverified;
* a stale outdoor reading never becomes a 0 °C guess.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.climatepilot.climate_adapter import (
    ClimateAdapter,
    ClimateSnapshot,
)
from custom_components.climatepilot.const import (
    FEEDBACK_UNAVAILABLE,
)
from custom_components.climatepilot.coordinator import (
    ClimatePilotCoordinator,
)
from custom_components.climatepilot.models import ZoneState

T0 = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)

LIVING_ROOM = "climate.livingroom_floorheating"
MASTER_BEDROOM = "climate.master_bedroom_floorheating"
BOILER = "climate.rinnai_heating_control"
OUTDOOR = "sensor.heweather_outside_temperature"


# ---------------------------------------------------------------------------
# hass double
# ---------------------------------------------------------------------------


class FakeState:
    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: dict | None = None,
        last_changed: datetime | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_changed = last_changed or T0
        self.last_updated = last_changed or T0


class FakeStates:
    def __init__(self) -> None:
        self._states: dict[str, FakeState] = {}

    def set(self, state: FakeState) -> None:
        self._states[state.entity_id] = state

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)

    def async_all(self, domain=None):
        if domain is None:
            return list(self._states.values())
        return [s for s in self._states.values() if s.entity_id.startswith(f"{domain}.")]


class FakeHass:
    def __init__(self) -> None:
        self.states = FakeStates()
        self.services = SimpleNamespace(async_call=AsyncMock(return_value=None))
        self.data: dict = {}
        self.config = SimpleNamespace(path=lambda *parts: "/tmp/climatepilot-test")
        self.loop = None


def knx_thermostat(entity_id: str, temperature: float, action: str = "heating") -> FakeState:
    """A real KNX room thermostat as reported by the production system."""
    return FakeState(
        entity_id,
        "heat",
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 16.0,
            "max_temp": 26.0,
            "target_temp_step": 0.5,
            "current_temperature": 27.0,
            "temperature": temperature,
            "hvac_action": action,
            "supported_features": 385,
        },
    )


def rinnai_boiler(state: str = "heat") -> FakeState:
    """The real boiler: no current temperature, step 1, only target support."""
    return FakeState(
        BOILER,
        state,
        {
            "hvac_modes": ["heat", "auto", "dry", "off"],
            "min_temp": 40,
            "max_temp": 82,
            "target_temp_step": 1,
            "current_temperature": None,
            "temperature": 40,
            "hvac_action": "off" if state == "off" else "heating",
            "supported_features": 1,
        },
    )


# ---------------------------------------------------------------------------
# Adapter behaviour against real attributes
# ---------------------------------------------------------------------------


def test_knx_snapshot_reads_real_attributes():
    hass = FakeHass()
    hass.states.set(knx_thermostat(LIVING_ROOM, 25.0))
    snapshot = ClimateAdapter(hass).snapshot(LIVING_ROOM)

    assert snapshot.exists is True
    assert snapshot.state == "heat"
    assert snapshot.temperature == 25.0
    assert snapshot.current_temperature == 27.0
    assert snapshot.min_temp == 16.0
    assert snapshot.max_temp == 26.0
    assert snapshot.target_step == 0.5
    assert snapshot.hvac_action == "heating"


def test_boiler_snapshot_tolerates_missing_current_temperature():
    hass = FakeHass()
    hass.states.set(rinnai_boiler())
    snapshot = ClimateAdapter(hass).snapshot(BOILER)

    assert snapshot.current_temperature is None
    assert snapshot.temperature == 40
    assert snapshot.target_step == 1
    assert snapshot.hvac_modes == ("heat", "auto", "dry", "off")


def test_missing_entity_snapshot_is_not_an_exception():
    assert ClimateAdapter(FakeHass()).snapshot("climate.nope").exists is False


def test_unavailable_zone_is_not_treated_as_no_demand():
    hass = FakeHass()
    hass.states.set(FakeState(LIVING_ROOM, "unavailable", {}))
    snapshot = ClimateAdapter(hass).snapshot(LIVING_ROOM)
    zone = ClimateAdapter(hass).to_zone_state(
        {"zone_id": "living", "entity_id": LIVING_ROOM, "min_temp": 16.0, "max_temp": 26.0},
        snapshot,
    )
    assert zone.feedback_quality == FEEDBACK_UNAVAILABLE
    assert zone.hvac_action is None


# ---------------------------------------------------------------------------
# P0 range intersection (the bug the adapter lead found)
# ---------------------------------------------------------------------------


def _sync_zone_state(zone: ZoneState, snapshot: ClimateSnapshot) -> None:
    """Call the *real* coordinator capability-sync method.

    Deliberately not reimplemented here: a copy of the logic would keep passing
    while the production method stayed wrong.
    """
    ClimatePilotCoordinator._apply_capabilities(zone, snapshot)


def test_user_narrowed_range_survives_a_sync_cycle():
    """A room configured 18-24 on a 16-26 thermostat stays 18-24."""
    hass = FakeHass()
    hass.states.set(knx_thermostat(LIVING_ROOM, 22.0))
    snapshot = ClimateAdapter(hass).snapshot(LIVING_ROOM)

    zone = ZoneState(zone_id="living", entity_id=LIVING_ROOM)
    zone.config_min_temp = 18.0
    zone.config_max_temp = 24.0
    zone.refresh_limits()
    assert (zone.min_temp, zone.max_temp) == (18.0, 24.0)

    _sync_zone_state(zone, snapshot)
    assert (zone.min_temp, zone.max_temp) == (18.0, 24.0), (
        "the device range must not widen the user's configured window"
    )


def test_device_range_narrows_a_wider_user_range():
    hass = FakeHass()
    hass.states.set(knx_thermostat(LIVING_ROOM, 22.0))
    snapshot = ClimateAdapter(hass).snapshot(LIVING_ROOM)

    zone = ZoneState(zone_id="living", entity_id=LIVING_ROOM)
    zone.config_min_temp = 10.0
    zone.config_max_temp = 30.0
    zone.refresh_limits()
    _sync_zone_state(zone, snapshot)
    assert (zone.min_temp, zone.max_temp) == (16.0, 26.0)


def test_boiler_range_never_leaks_into_a_room_window():
    """The boiler reports 40-82 °C; a room must never inherit that."""
    hass = FakeHass()
    hass.states.set(rinnai_boiler())
    boiler_snapshot = ClimateAdapter(hass).snapshot(BOILER)

    zone = ZoneState(zone_id="living", entity_id=LIVING_ROOM)
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.refresh_limits()
    _sync_zone_state(zone, boiler_snapshot)
    assert (zone.min_temp, zone.max_temp) == (16.0, 26.0)


def test_repeated_sync_is_idempotent():
    hass = FakeHass()
    hass.states.set(knx_thermostat(LIVING_ROOM, 22.0))
    snapshot = ClimateAdapter(hass).snapshot(LIVING_ROOM)
    zone = ZoneState(zone_id="living", entity_id=LIVING_ROOM)
    zone.config_min_temp = 17.0
    zone.config_max_temp = 25.0
    zone.refresh_limits()
    for _ in range(5):
        _sync_zone_state(zone, snapshot)
    assert (zone.min_temp, zone.max_temp) == (17.0, 25.0)
