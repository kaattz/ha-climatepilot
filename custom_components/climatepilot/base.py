"""Shared entity base classes for ClimatePilot.

Every platform module (``sensor``, ``select``, ``number``, ...) builds on these
so device registration, unique ids and availability stay consistent.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_NAME, DOMAIN, MANUFACTURER, MODEL
from .models import ZoneState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .coordinator import ClimatePilotCoordinator


def _parent_device_id(hass: Any, identifier: tuple[str, str], config_entry_id: str) -> str | None:
    """Resolve a device identifier to its registry id, across HA versions.

    ``via_device_id`` wants a registry id, and the lookup helper only exists on
    newer releases; older ones resolve through ``async_get_device``.
    """
    helper = getattr(dr, "async_get_device_id_by_identifier", None)
    if helper is not None:
        try:
            return helper(hass, identifier, config_entry_id=config_entry_id)
        except Exception:
            return None

    registry = dr.async_get(hass)
    entry = registry.async_get_device(identifiers={identifier})
    return getattr(entry, "id", None) if entry is not None else None


def _supports_via_device_id() -> bool:
    """Return True when this Home Assistant accepts ``DeviceInfo['via_device_id']``.

    The two forms are mutually exclusive across releases, which is the awkward
    part: ``via_device`` (an identifier tuple) was **removed** in favour of
    ``via_device_id`` (a registry id). Verified against real releases:

    ==========  ==============  =================
    HA release  ``via_device``  ``via_device_id``
    ==========  ==============  =================
    2026.2.3    present         absent
    2026.9.4    absent          present
    ==========  ==============  =================

    So the correct key has to be chosen per version rather than migrated once:
    passing ``via_device`` on current HA raises (aborting registration of every
    zone entity), and passing ``via_device_id`` on older HA is an unexpected
    keyword that fails the same way.

    Detected from the device registry's own signature rather than a version
    number, because the signature is the contract actually being relied upon -
    a version comparison breaks on backports and on the dev/rc channel.
    """
    try:
        parameters = inspect.signature(dr.DeviceRegistry.async_get_or_create).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    if "via_device_id" in parameters:
        return True
    if "via_device" in parameters:
        return False
    # Neither name is explicit: fall back to what DeviceInfo declares, which is
    # the same contract expressed from the other side.
    return "via_device_id" in getattr(dr.DeviceInfo, "__annotations__", {})


class ClimatePilotEntity(CoordinatorEntity["ClimatePilotCoordinator"]):
    """Base entity attached to the ClimatePilot system device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ClimatePilotCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.system_id}_{key}"
        self._key = key

    @property
    def device_info(self) -> DeviceInfo:
        """Return the system-level device this entity belongs to."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.system_id)},
            name=self.coordinator.name or DEFAULT_NAME,
            manufacturer=MANUFACTURER,
            model=MODEL,
            entry_type=None,
        )


class ClimatePilotZoneEntity(ClimatePilotEntity):
    """Base entity for entities owned by a single heating zone."""

    def __init__(
        self,
        coordinator: ClimatePilotCoordinator,
        zone_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator, f"{zone_id}_{key}")
        self._zone_id = zone_id

    @property
    def zone(self) -> ZoneState:
        """Return the live zone state.

        Raises:
            KeyError: when the zone was removed from the options.
        """
        return self.coordinator.zones[self._zone_id]

    @property
    def device_info(self) -> DeviceInfo:
        """Zones are sub-devices of the system device.

        The parent link is expressed with whichever key this Home Assistant
        accepts: ``via_device_id`` (registry id) on current releases, or the
        older ``via_device`` (identifier tuple) on releases that predate it.
        Passing the wrong one is not a cosmetic problem - ``via_device`` raises
        on current HA, and ``via_device_id`` is an unexpected keyword on older
        HA - and either failure aborts registration of every zone entity.
        """
        zone = self.coordinator.zones.get(self._zone_id)
        name = zone.name if zone else self._zone_id
        identifier = (DOMAIN, f"{self.coordinator.system_id}_{self._zone_id}")

        if _supports_via_device_id():
            device_info = DeviceInfo(
                identifiers={identifier},
                name=name,
                manufacturer=MANUFACTURER,
                model="Heating Zone",
            )
            # ``config_entry_id`` scopes the lookup so a device owned by another
            # entry cannot be linked by accident; both it and the entry itself
            # are read defensively, because a missing parent must degrade to
            # "no parent" rather than abort registration of every entity.
            entry = getattr(self.coordinator, "entry", None)
            entry_id = getattr(entry, "entry_id", None)
            if entry_id:
                parent_id = _parent_device_id(
                    self.hass,
                    (DOMAIN, self.coordinator.system_id),
                    entry_id,
                )
                if parent_id is not None:
                    device_info["via_device_id"] = parent_id
            return device_info

        # Older Home Assistant: the identifier tuple is still the supported form.
        return DeviceInfo(
            identifiers={identifier},
            name=name,
            manufacturer=MANUFACTURER,
            model="Heating Zone",
            via_device=(DOMAIN, self.coordinator.system_id),
        )

    @property
    def available(self) -> bool:
        """A zone entity is available while its climate entity reports state."""
        if not super().available:
            return False
        zone = self.coordinator.zones.get(self._zone_id)
        return zone is not None


class ClimatePilotDiagnosticEntity(ClimatePilotEntity):
    """Base entity for diagnostic entities."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
