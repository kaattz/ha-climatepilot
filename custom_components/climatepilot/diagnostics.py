"""Diagnostics export for ClimatePilot.

Home Assistant's "Download diagnostics" must never leak anything sensitive, so
this module is deliberately conservative: it exports the *decision context* -
what the integration thought, and why - and never exports credentials, tokens or
anything from ``.storage`` verbatim.

Entity IDs are included because they are the whole point of the export (they are
what makes a support report actionable) and are not secret. Any key whose name
looks like a secret is redacted defensively, so a future option added without
thinking about this file still cannot leak through it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

#: Keys redacted wherever they appear. Names only - no values are ever listed.
TO_REDACT: set[str] = {
    "password",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "username",
    "latitude",
    "longitude",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    payload: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "title": entry.title,
        },
        # The config carries the verification attestations, so redacting keys
        # named like secrets keeps this safe even if it grows later.
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
    }

    if coordinator is None:
        payload["coordinator"] = None
        payload["note"] = "Coordinator is not loaded; runtime state unavailable."
        return payload

    capabilities = getattr(coordinator.state_machine, "capabilities", None)
    heat_decision = coordinator.heat_decision
    compensation = coordinator.compensation

    payload["coordinator"] = {
        "name": coordinator.name,
        "control_mode": coordinator.control_mode,
        "writes_enabled": coordinator.writes_enabled,
        "heat_source_enabled": coordinator.heat_source_enabled,
        "active_zones": coordinator.active_zone_count,
        "unknown_zones": coordinator.unknown_zone_count,
    }

    # --- why the heat source is or is not acting --------------------------
    payload["heat_source"] = {
        "entity_id": coordinator.boiler_entity,
        "state": coordinator.heat_state,
        "capabilities": (
            {
                "min_temp": capabilities.min_temp,
                "max_temp": capabilities.max_temp,
                "target_step": capabilities.target_step,
                "hvac_modes": list(capabilities.hvac_modes),
                "semantics_verified": capabilities.semantics_verified,
                "min_flow_verified": capabilities.min_flow_verified,
            }
            if capabilities is not None
            else None
        ),
        "decision": (
            {
                "decision": heat_decision.decision,
                "state": heat_decision.state,
                "target_mode": heat_decision.target_mode,
                "target_supply": heat_decision.target_supply,
                "wait_seconds": heat_decision.wait_seconds,
                "reason_codes": list(heat_decision.reason_codes),
            }
            if heat_decision is not None
            else None
        ),
    }

    # --- weather compensation ---------------------------------------------
    payload["weather"] = {
        "outdoor_entity_id": coordinator.outdoor_entity,
        "result": (
            {
                "suggested": compensation.suggested,
                "outdoor": compensation.outdoor,
                "quality": compensation.quality,
                "should_apply": compensation.should_apply,
                "reason_codes": list(compensation.reason_codes),
            }
            if compensation is not None
            else None
        ),
    }

    # --- per-zone explainability ------------------------------------------
    payload["zones"] = {
        zone_id: {
            "entity_id": zone.entity_id,
            "name": zone.name,
            "mode": zone.mode,
            "feedback_quality": zone.feedback_quality,
            "hvac_mode": zone.hvac_mode,
            "hvac_action": zone.hvac_action,
            "supports_action": zone.supports_action,
            "base_setpoint": zone.base_setpoint,
            "effective_setpoint": zone.effective_setpoint,
            "actual_setpoint": zone.actual_setpoint,
            "current_temperature": zone.current_temperature,
            "allowed_range": [zone.min_temp, zone.max_temp],
            "configured_range": [zone.config_min_temp, zone.config_max_temp],
            "device_range": [zone.device_min_temp, zone.device_max_temp],
            "target_step": zone.target_step,
            "tolerance": zone.tolerance,
            "source": coordinator.setpoint_source(zone_id),
            "reason_codes": coordinator.reason_codes_for(zone_id),
            "manual_override": (
                {
                    "value": zone.manual_override.value,
                    "policy": zone.manual_override.policy,
                    "created_at": zone.manual_override.created_at.isoformat(),
                    "until": (
                        zone.manual_override.until.isoformat()
                        if zone.manual_override.until
                        else None
                    ),
                }
                if zone.manual_override is not None
                else None
            ),
            "pending_command": (
                {
                    "target": zone.pending_command.target,
                    "issued_at": zone.pending_command.issued_at.isoformat(),
                    "attempts": zone.pending_command.attempts,
                    "failed": zone.pending_command.failed,
                }
                if zone.pending_command is not None
                else None
            ),
        }
        for zone_id, zone in coordinator.zones.items()
    }

    # --- burner / short-cycle diagnostics ---------------------------------
    payload["diagnostics"] = coordinator.diagnostics_snapshot

    # --- schedule definitions (so a report shows the configured intent) ----
    payload["schedules"] = {
        zone_id: coordinator.zone_schedule(zone_id).to_dict() for zone_id in coordinator.zones
    }

    return payload


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: Any
) -> dict[str, Any]:
    """Per-device diagnostics.

    Every ClimatePilot entity hangs off the system device or a zone sub-device,
    so the entry-level payload is the useful one; this simply re-exports it
    rather than inventing a second, thinner shape.
    """
    return await async_get_config_entry_diagnostics(hass, entry)
