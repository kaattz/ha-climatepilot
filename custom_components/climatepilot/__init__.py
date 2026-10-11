"""ClimatePilot integration entry point.

Owns the config entry lifecycle:

* build one :class:`ClimatePilotCoordinator` per entry and perform the first
  refresh before the platforms read it, so no entity ever observes an
  un-evaluated coordinator;
* forward the entity platforms;
* register the three user-facing services (``resume_auto``,
  ``set_supply_temperature``, ``reassess``) once, and remove them again when the
  last entry unloads;
* migrate older config entries to the current schema version.

``PLATFORMS`` is built at import time and includes ``climate`` only when the
module exists, so this file keeps working while the optional whole-home
thermostat is absent.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ZONE_ENTITY,
    CONF_ZONE_ID,
    CONTROL_MODES,
    DOMAIN,
    SERVICE_REASSESS,
    SERVICE_RESUME_AUTO,
    SERVICE_SET_SUPPLY_TEMPERATURE,
)
from .coordinator import ClimatePilotCoordinator

_LOGGER = logging.getLogger(__name__)

#: Config entry schema version owned by this module.
CONFIG_ENTRY_VERSION: int = 1
CONFIG_ENTRY_MINOR_VERSION: int = 1

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

#: Every platform module that ships with the integration. ``climate`` is
#: optional: it is only forwarded when the module is actually present.
PLATFORMS: list[Platform] = [
    Platform.SELECT,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
]

try:  # pragma: no cover - depends on which optional modules are shipped
    from . import climate as _climate_platform  # noqa: F401
except ImportError:  # pragma: no cover - optional platform
    _LOGGER.debug("ClimatePilot climate platform is not installed")
else:
    PLATFORMS.append(Platform.CLIMATE)


#: Public surface every coordinator must provide (``docs/INTERFACES.md`` 2.8).
#: Checked structurally rather than with ``isinstance`` so a wrapper or
#: subclass -- and the test doubles -- are treated the same as the real thing.
_COORDINATOR_API = (
    "zones",
    "zone_ids",
    "name",
    "async_reassess",
    "async_resume_auto",
    "async_set_supply_temperature",
)


def _is_coordinator(candidate: Any) -> bool:
    """Return True when ``candidate`` looks like a live coordinator."""
    return all(hasattr(candidate, attr) for attr in _COORDINATOR_API)


def _coordinators(hass: HomeAssistant) -> list[ClimatePilotCoordinator]:
    """Return every live coordinator."""
    return [
        candidate
        for candidate in (hass.data.get(DOMAIN) or {}).values()
        if _is_coordinator(candidate)
    ]


def _first_coordinator(hass: HomeAssistant) -> ClimatePilotCoordinator | None:
    """Return an arbitrary live coordinator, or ``None``."""
    for candidate in (hass.data.get(DOMAIN) or {}).values():
        if _is_coordinator(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Config entry lifecycle
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (no YAML configuration is supported)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one ClimatePilot heating system."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = ClimatePilotCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_setup_listeners()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await _async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one ClimatePilot heating system."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    coordinator: ClimatePilotCoordinator | None = (hass.data.get(DOMAIN) or {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        coordinator.async_shutdown_listeners()

    if not _first_coordinator(hass):
        _async_remove_services(hass)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current schema version.

    Version 0 stored the system name under a legacy key and kept every zone
    inline in ``data``.  Migrating re-homes those values into the current
    layout and bumps the version so the migration runs only once.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # The entry was written by a newer integration version: refuse rather
        # than silently rewriting settings we do not understand.
        _LOGGER.error(
            "Cannot migrate ClimatePilot entry %s from version %s: this "
            "integration only understands up to version %s",
            entry.entry_id,
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False

    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    data = dict(entry.data)
    options = dict(entry.options)

    # v0 kept the display name in ``data[CONF_SYSTEM_NAME]``; v1 reads it from
    # options first, so copy it across without dropping the original.
    legacy_name = data.get("system_name")
    if legacy_name and "system_name" not in options:
        options["system_name"] = legacy_name

    # v0 zones were a mapping keyed by zone id; v1 expects a list of dicts.
    legacy_zones = options.get("zones") or data.get("zones")
    if isinstance(legacy_zones, dict):
        migrated: list[dict[str, Any]] = []
        for zone_id, payload in legacy_zones.items():
            if not isinstance(payload, dict):
                continue
            zone = {"zone_id": zone_id, **payload}
            zone.setdefault("entity_id", payload.get("entity_id"))
            if not zone.get("entity_id"):
                _LOGGER.warning(
                    "Dropping ClimatePilot zone %s during migration: no entity_id",
                    zone_id,
                )
                continue
            migrated.append(zone)
        options["zones"] = migrated

    control_mode = options.get("control_mode") or data.get("control_mode")
    if control_mode not in CONTROL_MODES:
        # An unknown or absent mode must never silently mean "coordinate".
        options["control_mode"] = CONTROL_MODES[0]
    else:
        options["control_mode"] = control_mode

    hass.config_entries.async_update_entry(
        entry,
        data=data,
        options=options,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
    )
    _LOGGER.info("Migrated ClimatePilot entry %s to version 1", entry.entry_id)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


async def _async_handle_resume_auto(call: ServiceCall) -> None:
    """Clear manual overrides for one room, or for the whole system.

    ``zone_id`` is the documented field in ``strings.json`` and is therefore
    treated as canonical; ``entity_id`` is accepted as a convenience alias so a
    climate entity id also works.  Omitting both resumes every room
    (design doc 4.3).
    """
    target = call.data.get(CONF_ZONE_ID) or call.data.get(CONF_ZONE_ENTITY)
    for coordinator in _coordinators(call.hass):
        zone_id = None
        if target:
            zone_id = _resolve_zone_id(coordinator, target)
            if zone_id is None:
                _LOGGER.warning("resume_auto: %s does not belong to %s", target, coordinator.name)
                continue
        await coordinator.async_resume_auto(zone_id)


async def _async_handle_set_supply_temperature(call: ServiceCall) -> None:
    """Apply an explicit supply-temperature request."""
    temperature = float(call.data[ATTR_TEMPERATURE])
    for coordinator in _coordinators(call.hass):
        await coordinator.async_set_supply_temperature(temperature)


async def _async_handle_reassess(call: ServiceCall) -> None:
    """Re-run arbitration and the heat-source evaluation immediately."""
    for coordinator in _coordinators(call.hass):
        await coordinator.async_reassess()


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register the integration services exactly once."""
    if hass.services.has_service(DOMAIN, SERVICE_RESUME_AUTO):
        return

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_RESUME_AUTO,
        _async_handle_resume_auto,
        vol.Schema(
            {
                vol.Optional(CONF_ZONE_ID): cv.string,
                vol.Optional(CONF_ZONE_ENTITY): cv.string,
            },
            extra=vol.PREVENT_EXTRA,
        ),
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_SUPPLY_TEMPERATURE,
        _async_handle_set_supply_temperature,
        vol.Schema(
            {vol.Required(ATTR_TEMPERATURE): vol.Coerce(float)},
            extra=vol.PREVENT_EXTRA,
        ),
    )
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_REASSESS,
        _async_handle_reassess,
        vol.Schema({}, extra=vol.PREVENT_EXTRA),
        supports_response=SupportsResponse.OPTIONAL,
    )


def _async_remove_services(hass: HomeAssistant) -> None:
    """Remove the integration services."""
    for service in (SERVICE_RESUME_AUTO, SERVICE_SET_SUPPLY_TEMPERATURE, SERVICE_REASSESS):
        hass.services.async_remove(DOMAIN, service)


def _resolve_zone_id(coordinator: ClimatePilotCoordinator, entity_id: str) -> str | None:
    """Map a zone climate entity id onto its configured zone id."""
    for zone_id, zone in coordinator.zones.items():
        if zone.entity_id == entity_id:
            return zone_id
    # Fall back to treating the value as a zone id already.
    return entity_id if entity_id in coordinator.zones else None
