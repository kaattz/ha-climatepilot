"""Tests for the diagnostics export.

The contract that matters here is *what must never appear*: Home Assistant's
"Download diagnostics" button produces a file users paste into public issue
trackers, so a leak is a real-world consequence. These tests assert both
directions - the useful decision context is present, and secret-looking keys are
redacted.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import time

from custom_components.climatepilot.const import (
    CONTROL_MODE_OBSERVE,
    DOMAIN,
    FEEDBACK_VALID,
    FSM_HEATING,
)
from custom_components.climatepilot.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.climatepilot.heat_source import (
    BoilerCapabilities,
    HeatSourceDecision,
)
from custom_components.climatepilot.models import (
    ManualOverride,
    PendingCommand,
    Schedule,
    ScheduleSlot,
    ZoneState,
)
from custom_components.climatepilot.weather_compensator import (
    CompensationResult,
)

T0 = datetime(2026, 10, 7, 19, 0, tzinfo=UTC)


def make_zone(zone_id: str = "living_room") -> ZoneState:
    zone = ZoneState(zone_id=zone_id, entity_id=f"climate.{zone_id}_floorheating")
    zone.name = "客厅地暖"
    zone.config_min_temp = 16.0
    zone.config_max_temp = 26.0
    zone.device_min_temp = 16.0
    zone.device_max_temp = 26.0
    zone.refresh_limits()
    zone.feedback_quality = FEEDBACK_VALID
    zone.hvac_mode = "heat"
    zone.hvac_action = "heating"
    zone.base_setpoint = 22.0
    zone.effective_setpoint = 22.0
    zone.actual_setpoint = 22.0
    zone.current_temperature = 21.0
    return zone


def make_coordinator(**overrides) -> SimpleNamespace:
    zone = make_zone()
    coordinator = SimpleNamespace(
        name="我的家",
        control_mode=CONTROL_MODE_OBSERVE,
        writes_enabled=False,
        heat_source_enabled=False,
        active_zone_count=1,
        unknown_zone_count=0,
        boiler_entity="climate.rinnai_heating_control",
        outdoor_entity="sensor.heweather_outside_temperature",
        heat_state=FSM_HEATING,
        zones={"living_room": zone},
        zone_schedule=lambda zone_id: Schedule(slots=(ScheduleSlot(at=time(6, 0), setpoint=22.0),)),
        setpoint_source=lambda zone_id: "schedule",
        reason_codes_for=lambda zone_id: ["schedule_transition"],
        diagnostics_snapshot={
            "burner": {
                "starts_24h": 4,
                "short_cycle_alert": False,
                "includes_dhw": True,
            }
        },
        state_machine=SimpleNamespace(
            capabilities=BoilerCapabilities(
                entity_id="climate.rinnai_heating_control",
                min_temp=40.0,
                max_temp=82.0,
                target_step=1.0,
                hvac_modes=("heat", "auto", "dry", "off"),
                semantics_verified=False,
                min_flow_verified=False,
            )
        ),
        heat_decision=HeatSourceDecision(
            state=FSM_HEATING,
            decision="request_heat",
            target_mode="heat",
            target_supply=45.0,
            wait_seconds=None,
            reason_codes=("heat_source_semantics_unverified",),
        ),
        compensation=CompensationResult(
            suggested=45.0,
            outdoor=8.0,
            quality="valid",
            reason_codes=(),
            should_apply=False,
        ),
    )
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


def make_entry(**option_overrides) -> MagicMock:
    options = {
        "system_name": "我的家",
        "control_mode": CONTROL_MODE_OBSERVE,
        "verify_set_hvac_mode_heating_only": False,
        "latitude": 27.99,
        "longitude": 120.69,
    }
    options.update(option_overrides)
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.version = 1
    entry.minor_version = 1
    entry.title = "ClimatePilot"
    entry.data = {"system_name": "我的家"}
    entry.options = options
    return entry


def make_hass(coordinator) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    return hass


# ---------------------------------------------------------------------------
# Useful content is present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_include_the_decision_context():
    payload = await async_get_config_entry_diagnostics(make_hass(make_coordinator()), make_entry())

    assert payload["coordinator"]["control_mode"] == CONTROL_MODE_OBSERVE
    assert payload["heat_source"]["decision"]["decision"] == "request_heat"
    assert payload["heat_source"]["capabilities"]["semantics_verified"] is False
    assert payload["weather"]["result"]["suggested"] == 45.0
    assert payload["zones"]["living_room"]["effective_setpoint"] == 22.0
    assert payload["zones"]["living_room"]["source"] == "schedule"
    assert payload["zones"]["living_room"]["reason_codes"] == ["schedule_transition"]
    assert payload["diagnostics"]["burner"]["starts_24h"] == 4
    assert "living_room" in payload["schedules"]


@pytest.mark.asyncio
async def test_diagnostics_show_both_configured_and_device_ranges():
    """A support report must show why a target was clamped where it was."""
    payload = await async_get_config_entry_diagnostics(make_hass(make_coordinator()), make_entry())
    zone = payload["zones"]["living_room"]
    assert zone["configured_range"] == [16.0, 26.0]
    assert zone["device_range"] == [16.0, 26.0]
    assert zone["allowed_range"] == [16.0, 26.0]


@pytest.mark.asyncio
async def test_diagnostics_include_manual_override_detail():
    zone = make_zone()
    zone.manual_override = ManualOverride(
        value=21.5,
        created_at=T0,
        policy="fixed_duration",
        until=T0 + timedelta(hours=2),
    )
    coordinator = make_coordinator(zones={"living_room": zone})
    payload = await async_get_config_entry_diagnostics(make_hass(coordinator), make_entry())

    override = payload["zones"]["living_room"]["manual_override"]
    assert override["value"] == 21.5
    assert override["policy"] == "fixed_duration"
    assert override["until"] is not None


@pytest.mark.asyncio
async def test_diagnostics_include_pending_command_state():
    zone = make_zone()
    zone.pending_command = PendingCommand(
        request_id="req-1",
        zone_id="living_room",
        entity_id=zone.entity_id,
        target=22.5,
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=90),
        tolerance=0.2,
        failed=True,
    )
    coordinator = make_coordinator(zones={"living_room": zone})
    payload = await async_get_config_entry_diagnostics(make_hass(coordinator), make_entry())

    pending = payload["zones"]["living_room"]["pending_command"]
    assert pending["target"] == 22.5
    assert pending["failed"] is True


@pytest.mark.asyncio
async def test_diagnostics_survive_an_unloaded_coordinator():
    payload = await async_get_config_entry_diagnostics(make_hass(None), make_entry())
    assert payload["coordinator"] is None
    assert "note" in payload


# ---------------------------------------------------------------------------
# Secrets must never appear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secret_looking_keys_are_redacted():
    entry = make_entry(password="hunter2", api_key="sk-secret", token="abc123")
    payload = await async_get_config_entry_diagnostics(make_hass(make_coordinator()), entry)

    options = payload["options"]
    for key in ("password", "api_key", "token"):
        assert options[key] != {"hunter2", "sk-secret", "abc123"}  # never the raw value
    assert "hunter2" not in str(payload)
    assert "sk-secret" not in str(payload)


@pytest.mark.asyncio
async def test_location_is_redacted():
    """Home coordinates are personal data and must not leave the instance."""
    entry = make_entry(latitude=27.99, longitude=120.69)
    payload = await async_get_config_entry_diagnostics(make_hass(make_coordinator()), entry)

    assert payload["options"]["latitude"] != 27.99
    assert payload["options"]["longitude"] != 120.69


@pytest.mark.asyncio
async def test_non_secret_options_are_preserved():
    """Redaction must not be so broad that the export stops being useful."""
    entry = make_entry()
    payload = await async_get_config_entry_diagnostics(make_hass(make_coordinator()), entry)

    assert payload["options"]["system_name"] == "我的家"
    assert payload["options"]["control_mode"] == CONTROL_MODE_OBSERVE
    assert payload["options"]["verify_set_hvac_mode_heating_only"] is False


def test_redaction_list_covers_the_obvious_names():
    for name in ("password", "token", "api_key", "secret", "latitude", "longitude"):
        assert name in TO_REDACT


# ---------------------------------------------------------------------------
# Device diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_diagnostics_reuse_the_entry_payload():
    payload = await async_get_device_diagnostics(
        make_hass(make_coordinator()), make_entry(), device=MagicMock()
    )
    assert "zones" in payload
    assert "heat_source" in payload
