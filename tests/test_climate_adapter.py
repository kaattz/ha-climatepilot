"""Unit tests for the Home Assistant climate adapter.

These tests run against a minimal ``hass`` double instead of a booted Home
Assistant: the adapter is a thin, defensive wrapper, and the interesting
behaviour is entirely in how it reacts to absent, unavailable and lying
entities.  No Home Assistant fixture plugin is required.

Reference devices are the real production entities the integration targets:

* ``climate.livingroom_floorheating`` -- KNX room thermostat,
  ``hvac_modes ["heat", "off"]``, 16..26 C in 0.5 C steps.
* ``climate.rinnai_heating_control`` -- boiler, ``supported_features = 1``
  (target temperature only, **no** ``hvac_action``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.climatepilot.climate_adapter import (
    ClimateAdapter,
    ClimateSnapshot,
)
from custom_components.climatepilot.const import (
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DEFAULT_TARGET_STEP,
    DEFAULT_ZONE_TOLERANCE,
    FEEDBACK_UNAVAILABLE,
    FEEDBACK_UNCERTAIN,
    FEEDBACK_VALID,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    ZONE_MODE_AUTO,
    ZONE_MODE_ECO,
)

UTC_NOW = datetime(2026, 2, 3, 12, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Minimal Home Assistant doubles
# ---------------------------------------------------------------------------


class FakeStates:
    """Stand-in for ``hass.states``."""

    def __init__(self, entities: dict[str, object] | None = None) -> None:
        self._entities = dict(entities or {})

    def get(self, entity_id: str) -> object | None:
        return self._entities.get(entity_id)

    def set(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        self._entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            state=state,
            attributes=dict(attributes or {}),
            last_updated=UTC_NOW,
            last_changed=UTC_NOW,
        )

    def drop(self, entity_id: str) -> None:
        self._entities.pop(entity_id, None)


class FakeServices:
    """Stand-in for ``hass.services`` recording every service call."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.async_call = AsyncMock(side_effect=error)
        self.calls: list[tuple[str, str, dict, bool]] = []

    def record(self) -> None:
        self.calls = [
            (
                call.args[0],
                call.args[1],
                call.args[2],
                call.kwargs.get("blocking", False),
            )
            for call in self.async_call.await_args_list
        ]


class FakeHass:
    """The smallest object the adapter can run against."""

    def __init__(
        self,
        entities: dict[str, object] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.states = FakeStates(entities)
        self.services = FakeServices(error)


def make_state(
    *,
    state: str = "heat",
    temperature: float | None = 25.0,
    current_temperature: float | None = 27.0,
    hvac_action: str | None = "off",
    min_temp: float | None = 16.0,
    max_temp: float | None = 26.0,
    target_step: float | None = 0.5,
    hvac_modes: list[str] | None = None,
    last_updated: datetime | None = UTC_NOW,
    last_changed: datetime | None = None,
) -> SimpleNamespace:
    """Build a fake state object with the given attributes."""
    attributes: dict[str, object] = {}
    if temperature is not None:
        attributes["temperature"] = temperature
    if current_temperature is not None:
        attributes["current_temperature"] = current_temperature
    if hvac_action is not None:
        attributes["hvac_action"] = hvac_action
    if min_temp is not None:
        attributes["min_temp"] = min_temp
    if max_temp is not None:
        attributes["max_temp"] = max_temp
    if target_step is not None:
        attributes["target_temp_step"] = target_step
    if hvac_modes is not None:
        attributes["hvac_modes"] = hvac_modes
    return SimpleNamespace(
        state=state,
        attributes=attributes,
        last_updated=last_updated,
        **({} if last_changed is None else {"last_changed": last_changed}),
    )


KNX_ROOM = "climate.livingroom_floorheating"
BOILER = "climate.rinnai_heating_control"


def knx_room_state(**overrides) -> SimpleNamespace:
    """Return a state object mirroring the real KNX room thermostat."""
    data = {
        "state": "heat",
        "temperature": 25.0,
        "current_temperature": 27.0,
        "hvac_action": "off",
        "min_temp": 16.0,
        "max_temp": 26.0,
        "target_step": 0.5,
        "hvac_modes": ["heat", "off"],
        "last_changed": UTC_NOW,
    }
    data.update(overrides)
    return make_state(**data)


def boiler_state(**overrides) -> SimpleNamespace:
    """Return the Rinnai boiler as ``supported_features = 1`` reports it."""
    data = {
        "state": "heat",
        "temperature": 40,
        "current_temperature": None,
        "hvac_action": None,
        "min_temp": 40,
        "max_temp": 82,
        "target_step": 1,
        "hvac_modes": ["heat", "auto", "dry", "off"],
        "last_changed": UTC_NOW,
    }
    data.update(overrides)
    return make_state(**data)


def make_adapter(**entities) -> tuple[ClimateAdapter, FakeServices]:
    """Return an adapter over a fresh hass double plus its service recorder."""
    hass = FakeHass(entities)
    return ClimateAdapter(hass), hass.services


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def test_snapshot_missing_entity_does_not_raise() -> None:
    adapter, _ = make_adapter()

    snap = adapter.snapshot("climate.does_not_exist")

    assert isinstance(snap, ClimateSnapshot)
    assert snap.entity_id == "climate.does_not_exist"
    assert snap.exists is False
    assert snap.state is None
    assert snap.hvac_modes == ()
    assert snap.supports_action is False
    assert snap.temperature is None
    assert snap.last_updated is None


def test_snapshot_empty_entity_id_does_not_raise() -> None:
    adapter, _ = make_adapter()

    snap = adapter.snapshot("")

    assert snap.exists is False
    assert isinstance(snap, ClimateSnapshot)


def test_snapshot_reads_real_knx_thermostat() -> None:
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})

    snap = adapter.snapshot(KNX_ROOM)

    assert snap.exists is True
    assert snap.state == "heat"
    assert snap.hvac_action == "off"
    assert snap.temperature == 25.0
    assert snap.current_temperature == 27.0
    assert snap.min_temp == 16.0
    assert snap.max_temp == 26.0
    assert snap.target_step == 0.5
    assert snap.hvac_modes == ("heat", "off")
    assert snap.last_updated == UTC_NOW
    assert snap.supports_action is True


def test_snapshot_boiler_without_hvac_action() -> None:
    """supported_features = 1 -> no hvac_action; the adapter must tolerate it."""
    adapter, _ = make_adapter(**{BOILER: boiler_state()})

    snap = adapter.snapshot(BOILER)

    assert snap.exists is True
    assert snap.state == "heat"
    assert snap.hvac_action is None
    assert snap.supports_action is False
    # 40/82/1 must survive as-is, not be replaced by zone defaults.
    assert snap.min_temp == 40.0
    assert snap.max_temp == 82.0
    assert snap.target_step == 1.0
    assert snap.current_temperature is None
    assert snap.temperature == 40.0


def test_snapshot_unavailable_never_reports_action_support() -> None:
    adapter, _ = make_adapter(
        **{
            KNX_ROOM: knx_room_state(
                state=STATE_UNAVAILABLE,
                hvac_action="off",
                temperature=None,
                current_temperature=None,
            )
        }
    )

    snap = adapter.snapshot(KNX_ROOM)

    assert snap.exists is True
    assert snap.state == STATE_UNAVAILABLE
    assert snap.supports_action is False
    assert snap.temperature is None


def test_snapshot_tolerates_garbage_attributes() -> None:
    adapter, _ = make_adapter()
    adapter.hass.states._entities["climate.weird"] = SimpleNamespace(
        state="heat",
        attributes={
            "temperature": "not-a-number",
            "current_temperature": float("nan"),
            "min_temp": None,
            "max_temp": "abc",
            "target_temp_step": float("inf"),
            "hvac_modes": "heat",  # a bare string instead of a list
            "hvac_action": "  ",
        },
        last_updated="not-a-datetime",
    )

    snap = adapter.snapshot("climate.weird")

    assert snap.exists is True
    assert snap.temperature is None
    assert snap.current_temperature is None
    assert snap.min_temp is None
    assert snap.max_temp is None
    assert snap.target_step is None
    assert snap.hvac_modes == ("heat",)
    assert snap.hvac_action is None
    assert snap.supports_action is False
    assert snap.last_updated is None


def test_snapshot_carries_both_timestamps() -> None:
    """last_updated drives freshness, last_changed drives manual-change detection."""
    changed = UTC_NOW - timedelta(minutes=5)
    adapter, _ = make_adapter(
        **{KNX_ROOM: knx_room_state(last_updated=UTC_NOW, last_changed=changed)}
    )

    snap = adapter.snapshot(KNX_ROOM)

    assert snap.last_updated == UTC_NOW
    assert snap.last_changed == changed


def test_snapshot_last_changed_absent_is_none() -> None:
    adapter, _ = make_adapter()
    adapter.hass.states._entities["climate.old"] = SimpleNamespace(
        state="heat",
        attributes={"temperature": 21.0},
        last_updated=UTC_NOW,
    )

    snap = adapter.snapshot("climate.old")

    assert snap.exists is True
    assert snap.last_updated == UTC_NOW
    assert snap.last_changed is None


def test_snapshot_missing_entity_has_no_timestamps() -> None:
    adapter, _ = make_adapter()

    snap = adapter.snapshot(KNX_ROOM)

    assert snap.last_updated is None
    assert snap.last_changed is None


# ---------------------------------------------------------------------------
# to_zone_state()
# ---------------------------------------------------------------------------


def test_to_zone_state_uses_device_capabilities() -> None:
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state(
        {
            "zone_id": "living_room",
            "entity_id": KNX_ROOM,
            "name": "Living Room",
            "mode": ZONE_MODE_ECO,
        },
        snap,
    )

    assert zone.zone_id == "living_room"
    assert zone.entity_id == KNX_ROOM
    assert zone.name == "Living Room"
    assert zone.mode == ZONE_MODE_ECO
    # config window defaults to DEFAULT_*; device window is what KNX reports;
    # the effective window is their intersection.
    assert zone.config_min_temp == DEFAULT_MIN_TEMP
    assert zone.config_max_temp == DEFAULT_MAX_TEMP
    assert zone.device_min_temp == 16.0
    assert zone.device_max_temp == 26.0
    assert zone.min_temp == 16.0
    assert zone.max_temp == 26.0
    assert zone.target_step == 0.5
    assert zone.tolerance == DEFAULT_ZONE_TOLERANCE
    assert zone.actual_setpoint == 25.0
    assert zone.current_temperature == 27.0
    assert zone.hvac_action == "off"
    assert zone.hvac_mode == "heat"
    assert zone.supports_action is True
    assert zone.feedback_quality == FEEDBACK_VALID


def test_to_zone_state_intersects_config_and_device_range() -> None:
    """User config narrows, device range is authoritative -> intersection."""
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})  # device 16..26
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state(
        {
            "zone_id": "bedroom",
            "entity_id": KNX_ROOM,
            "min_temp": 18.0,
            "max_temp": 30.0,
            "target_step": 0.25,
            "tolerance": 0.1,
        },
        snap,
    )

    assert zone.config_min_temp == 18.0
    assert zone.config_max_temp == 30.0
    assert zone.device_min_temp == 16.0
    assert zone.device_max_temp == 26.0
    assert zone.min_temp == 18.0  # max(18, 16)
    assert zone.max_temp == 26.0  # min(30, 26)
    assert zone.target_step == 0.25
    assert zone.tolerance == 0.1


def test_to_zone_state_contradictory_config_keeps_config_window() -> None:
    """A configured range disjoint from the device range keeps the config.

    The config range is the outer bound -- it says what this room may be driven
    to. A room wired to a 40-82 C boiler entity must never report a 40 C room
    target, so ``refresh_limits`` keeps the configured 30-32 C instead of
    adopting the device window.
    """
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})  # device 16..26
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state(
        {
            "zone_id": "nonsense",
            "entity_id": KNX_ROOM,
            "min_temp": 30.0,  # above the device maximum
            "max_temp": 32.0,
        },
        snap,
    )

    assert zone.config_min_temp == 30.0
    assert zone.config_max_temp == 32.0
    assert zone.device_min_temp == 16.0
    assert zone.device_max_temp == 26.0
    # Disjoint -> the configuration is the safe authority.
    assert zone.min_temp == 30.0
    assert zone.max_temp == 32.0


def test_to_zone_state_falls_back_to_const_defaults() -> None:
    """Missing min/max/step must come from const, not from None."""
    adapter, _ = make_adapter(
        **{
            BOILER: make_state(
                state="heat",
                hvac_action=None,
                temperature=None,
                current_temperature=None,
                min_temp=None,
                max_temp=None,
                target_step=None,
                hvac_modes=["heat"],
            )
        }
    )
    snap = adapter.snapshot(BOILER)

    zone = adapter.to_zone_state({"zone_id": "bare", "entity_id": BOILER}, snap)

    assert zone.config_min_temp == DEFAULT_MIN_TEMP
    assert zone.config_max_temp == DEFAULT_MAX_TEMP
    assert zone.device_min_temp is None
    assert zone.device_max_temp is None
    assert zone.min_temp == DEFAULT_MIN_TEMP
    assert zone.max_temp == DEFAULT_MAX_TEMP
    assert zone.target_step == DEFAULT_TARGET_STEP
    assert zone.tolerance == DEFAULT_ZONE_TOLERANCE
    assert zone.round_target(20.26) == 20.5


def test_to_zone_state_device_range_alone_when_config_unset() -> None:
    """A disjoint device range cannot widen the DEFAULT_* config window.

    The Rinnai boiler reports 40-82 C. With no per-zone config the DEFAULT
    window (16-26 C) governs, and the device window is recorded for diagnostics
    but never adopted -- otherwise a mis-selected boiler entity would let a room
    be driven to 82 C.
    """
    adapter, _ = make_adapter(**{BOILER: boiler_state()})
    snap = adapter.snapshot(BOILER)

    zone = adapter.to_zone_state({"zone_id": "boiler", "entity_id": BOILER}, snap)

    assert zone.device_min_temp == 40.0
    assert zone.device_max_temp == 82.0
    # DEFAULT 16..26 and device 40..82 are disjoint. The configured window is
    # the outer bound and is NOT widened to the device window: a room slot
    # pointed at a boiler entity must never start accepting a 40 °C room
    # target. (Behaviour revised by the Lead - the configured range is the
    # authority when the two ranges do not overlap.)
    assert zone.min_temp == DEFAULT_MIN_TEMP
    assert zone.max_temp == DEFAULT_MAX_TEMP
    assert zone.target_step == 1.0


def test_to_zone_state_garbage_step_and_inverted_device_range() -> None:
    adapter, _ = make_adapter(
        **{KNX_ROOM: knx_room_state(min_temp=26.0, max_temp=16.0, target_step=0.0)}
    )
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state({"zone_id": "broken", "entity_id": KNX_ROOM}, snap)

    # An inverted device range is dropped; a zero step falls back to DEFAULT.
    assert zone.device_min_temp is None
    assert zone.device_max_temp is None
    assert zone.min_temp == DEFAULT_MIN_TEMP
    assert zone.max_temp == DEFAULT_MAX_TEMP
    assert zone.target_step == DEFAULT_TARGET_STEP
    assert zone.round_target(30.0) == 26.0


def test_to_zone_state_missing_entity_is_unavailable_not_zero_demand() -> None:
    adapter, _ = make_adapter()
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state({"zone_id": "gone", "entity_id": KNX_ROOM}, snap)

    assert snap.exists is False
    assert zone.feedback_quality == FEEDBACK_UNAVAILABLE
    assert zone.supports_action is False
    assert zone.actual_setpoint is None
    assert zone.current_temperature is None
    # Unknown demand must not be laundered into a concrete number.
    assert zone.effective_setpoint is None


@pytest.mark.parametrize("bad_state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
def test_to_zone_state_null_states_are_unavailable(bad_state: str) -> None:
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state(state=bad_state)})
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state({"zone_id": "z", "entity_id": KNX_ROOM}, snap)

    assert zone.feedback_quality == FEEDBACK_UNAVAILABLE
    assert zone.supports_action is False


def test_to_zone_state_without_hvac_action_is_uncertain() -> None:
    """A reachable device that cannot report hvac_action is uncertain, not absent."""
    adapter, _ = make_adapter(**{BOILER: boiler_state()})
    snap = adapter.snapshot(BOILER)

    zone = adapter.to_zone_state({"zone_id": "boiler", "entity_id": BOILER}, snap)

    assert zone.supports_action is False
    assert zone.feedback_quality == FEEDBACK_UNCERTAIN
    assert zone.hvac_action is None
    assert zone.actual_setpoint == 40.0


def test_to_zone_state_defaults_zone_mode_from_snapshot_entity() -> None:
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})
    snap = adapter.snapshot(KNX_ROOM)

    zone = adapter.to_zone_state({"mode": "nonsense"}, snap)

    assert zone.zone_id == KNX_ROOM
    assert zone.entity_id == KNX_ROOM
    assert zone.name == KNX_ROOM
    assert zone.mode == ZONE_MODE_AUTO


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_temperature_success_calls_service_by_entity_id() -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    result = await adapter.async_set_temperature(KNX_ROOM, 21.5)

    assert result is True
    services.record()
    assert services.calls == [
        (
            "climate",
            "set_temperature",
            {"entity_id": KNX_ROOM, "temperature": 21.5},
            True,
        )
    ]
    # Never device_id targeting, never raw group addresses.
    assert "device_id" not in services.calls[0][2]
    assert set(services.calls[0][2]) == {"entity_id", "temperature"}


@pytest.mark.asyncio
async def test_set_temperature_missing_entity_returns_false() -> None:
    adapter, services = make_adapter()

    result = await adapter.async_set_temperature(KNX_ROOM, 21.5)

    assert result is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_temperature_unavailable_returns_false() -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state(state=STATE_UNAVAILABLE)})

    result = await adapter.async_set_temperature(KNX_ROOM, 21.5)

    assert result is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_temperature_unknown_state_returns_false() -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state(state=STATE_UNKNOWN)})

    result = await adapter.async_set_temperature(KNX_ROOM, 21.5)

    assert result is False
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize("bad", [None, "21.5", float("nan"), float("inf"), True])
@pytest.mark.asyncio
async def test_set_temperature_rejects_non_numeric(bad: object) -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    result = await adapter.async_set_temperature(KNX_ROOM, bad)  # type: ignore[arg-type]

    assert result is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_temperature_service_error_returns_false() -> None:
    hass = FakeHass({KNX_ROOM: knx_room_state()}, error=RuntimeError("KNX bus is down"))
    adapter = ClimateAdapter(hass)

    result = await adapter.async_set_temperature(KNX_ROOM, 21.5)

    assert result is False
    hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_temperature_empty_entity_id_returns_false() -> None:
    adapter, services = make_adapter()

    assert await adapter.async_set_temperature("", 21.5) is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_success() -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    assert await adapter.async_set_hvac_mode(KNX_ROOM, "off") is True

    services.record()
    assert services.calls == [
        ("climate", "set_hvac_mode", {"entity_id": KNX_ROOM, "hvac_mode": "off"}, True)
    ]


@pytest.mark.asyncio
async def test_set_hvac_mode_not_advertised_returns_false() -> None:
    """A mode the entity does not list is refused before burning a retry."""
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    result = await adapter.async_set_hvac_mode(KNX_ROOM, "cool")

    assert result is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_empty_mode_returns_false() -> None:
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    assert await adapter.async_set_hvac_mode(KNX_ROOM, "") is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_missing_entity_returns_false() -> None:
    adapter, services = make_adapter()

    assert await adapter.async_set_hvac_mode(KNX_ROOM, "off") is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_service_error_returns_false() -> None:
    hass = FakeHass({KNX_ROOM: knx_room_state()}, error=TimeoutError())
    adapter = ClimateAdapter(hass)

    assert await adapter.async_set_hvac_mode(KNX_ROOM, "heat") is False


@pytest.mark.asyncio
async def test_set_supply_temperature_uses_set_temperature() -> None:
    adapter, services = make_adapter(**{BOILER: boiler_state()})

    assert await adapter.async_set_supply_temperature(BOILER, 45.0) is True

    services.record()
    assert services.calls == [
        ("climate", "set_temperature", {"entity_id": BOILER, "temperature": 45.0}, True)
    ]


@pytest.mark.asyncio
async def test_set_supply_temperature_unavailable_returns_false() -> None:
    adapter, services = make_adapter(**{BOILER: boiler_state(state=STATE_UNAVAILABLE)})

    assert await adapter.async_set_supply_temperature(BOILER, 45.0) is False
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_allowed_services_are_ever_called() -> None:
    """Guard against the adapter creeping into other service domains."""
    adapter, services = make_adapter(**{KNX_ROOM: knx_room_state()})

    await adapter.async_set_temperature(KNX_ROOM, 20.0)
    await adapter.async_set_hvac_mode(KNX_ROOM, "heat")
    await adapter.async_set_supply_temperature(KNX_ROOM, 45.0)

    services.record()
    assert {call[0] for call in services.calls} == {"climate"}
    assert {call[1] for call in services.calls} == {"set_temperature", "set_hvac_mode"}
    assert all(call[3] is True for call in services.calls)


# ---------------------------------------------------------------------------
# is_stale()
# ---------------------------------------------------------------------------


def test_is_stale_boundaries() -> None:
    snap = ClimateSnapshot(
        entity_id=KNX_ROOM,
        exists=True,
        state="heat",
        hvac_action="heating",
        temperature=21.0,
        current_temperature=20.0,
        min_temp=16.0,
        max_temp=26.0,
        target_step=0.5,
        hvac_modes=("heat", "off"),
        last_updated=UTC_NOW,
        supports_action=True,
    )

    assert ClimateAdapter.is_stale(snap, UTC_NOW + timedelta(seconds=299), 300) is False
    assert ClimateAdapter.is_stale(snap, UTC_NOW + timedelta(seconds=300), 300) is False
    assert ClimateAdapter.is_stale(snap, UTC_NOW + timedelta(seconds=301), 300) is True
    assert ClimateAdapter.is_stale(snap, UTC_NOW, 0) is False


def test_is_stale_without_timestamp_is_stale() -> None:
    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state(last_updated=None)})
    snap = adapter.snapshot(KNX_ROOM)

    assert snap.last_updated is None
    assert ClimateAdapter.is_stale(snap, UTC_NOW, 300) is True


def test_is_stale_missing_entity_is_stale() -> None:
    adapter, _ = make_adapter()
    snap = adapter.snapshot("climate.absent")

    assert ClimateAdapter.is_stale(snap, UTC_NOW, 300) is True


def test_is_stale_naive_timestamp_is_stale_not_raising() -> None:
    adapter, _ = make_adapter(
        **{
            KNX_ROOM: knx_room_state(
                last_updated=datetime(2026, 2, 3, 12, 0)  # no tzinfo
            )
        }
    )
    snap = adapter.snapshot(KNX_ROOM)

    assert ClimateAdapter.is_stale(snap, UTC_NOW, 300) is True


def test_is_stale_future_timestamp_is_not_stale() -> None:
    adapter, _ = make_adapter(
        **{KNX_ROOM: knx_room_state(last_updated=UTC_NOW + timedelta(seconds=5))}
    )
    snap = adapter.snapshot(KNX_ROOM)

    assert ClimateAdapter.is_stale(snap, UTC_NOW, 300) is False


# ---------------------------------------------------------------------------
# cancellation semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("async_set_temperature", (KNX_ROOM, 21.5)),
        ("async_set_hvac_mode", (KNX_ROOM, "off")),
        ("async_set_supply_temperature", (KNX_ROOM, 45.0)),
    ],
)
async def test_cancellation_is_never_swallowed(method: str, args: tuple) -> None:
    """``CancelledError`` must escape so Home Assistant can shut down.

    The write guards catch bare ``Exception``, and ``asyncio.CancelledError``
    derives from ``BaseException`` -- so cancellation propagates. This test
    fails if anyone widens a catch to ``except BaseException`` / bare
    ``except:``, which would make the integration un-cancellable.
    """
    import asyncio

    hass = FakeHass({KNX_ROOM: knx_room_state()}, error=asyncio.CancelledError())
    adapter = ClimateAdapter(hass)

    with pytest.raises(asyncio.CancelledError):
        await getattr(adapter, method)(*args)


def test_cancelled_error_is_not_an_exception_subclass() -> None:
    """Documents the assumption the tests above rest on."""
    import asyncio

    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)


def test_adapter_never_catches_base_exception() -> None:
    """Structural guard: no bare ``except:`` and no ``except BaseException``."""
    import ast
    import inspect

    from custom_components.climatepilot import climate_adapter as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        assert node.type is not None, "bare 'except:' is forbidden"
        if isinstance(node.type, ast.Name):
            assert node.type.id != "BaseException"


# ---------------------------------------------------------------------------
# hygiene
# ---------------------------------------------------------------------------


def test_adapter_never_references_device_id_or_knx_group_addresses() -> None:
    """Structural guard: entity_id only, ``climate`` domain only.

    Parses the module AST instead of grepping prose, so the check cannot be
    satisfied by wording and cannot break on a comment.
    """
    import ast
    import inspect

    from custom_components.climatepilot import climate_adapter as module

    tree = ast.parse(inspect.getsource(module))

    # 1. No ``knx`` import at all -- group addresses must never be touched.
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(name == "knx" or name.startswith("knx.") for name in imported), imported

    # 2. Every service call uses a literal kwarg set and never device_id.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "device_id":
                raise AssertionError("adapter must never target device_id")

    # 3. Only the two allowed services are named anywhere in the module.
    service_constants = {
        target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id.startswith("SERVICE_")
    }
    assert service_constants == {
        "SERVICE_SET_TEMPERATURE": "set_temperature",
        "SERVICE_SET_HVAC_MODE": "set_hvac_mode",
    }


def test_snapshot_dataclass_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    adapter, _ = make_adapter(**{KNX_ROOM: knx_room_state()})
    snap = adapter.snapshot(KNX_ROOM)

    with pytest.raises(FrozenInstanceError):
        snap.state = "off"  # type: ignore[misc]


def test_adapter_is_read_only_when_hass_has_no_states() -> None:
    hass = Mock()
    hass.states = None
    adapter = ClimateAdapter(hass)

    snap = adapter.snapshot("climate.anything")

    assert snap.exists is False
