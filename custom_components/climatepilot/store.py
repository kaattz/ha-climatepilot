"""Persistent state for a ClimatePilot system.

Only data that survives a restart belongs here: manual overrides and their
expiry, pending/last-confirmed writes, the weather compensator's last applied
value, the heat-source state machine and the burner statistics.  Live device
values are always re-read from Home Assistant instead of being restored
(design doc 9.1, 11).

:class:`ClimatePilotStore` **subclasses**
:class:`homeassistant.helpers.storage.Store` rather than wrapping one.  That is
deliberate: ``Store._async_load_data`` dispatches ``self._async_migrate_func``
(by argument count) whenever the stored version or minor version differs, so
only a subclass can actually hook version migration.  A wrapper's migrate
function would be dead code.

Layout follows the Home Assistant storage conventions::

    {
        "version": 1,
        "minor_version": 1,
        "key": "climatepilot.state.<system_id>",
        "data": { "version": 1, "system_id": "...", "saved_at": "...", ... }
    }

Three layers of defence keep a bad storage file from breaking the integration:

* :meth:`ClimatePilotStore._async_migrate_func` rejects payloads this build
  cannot understand -- a foreign ``system_id``, an unsupported version, a
  non-object -- by raising :class:`StoreDataInvalid` instead of handing the
  coordinator a half-restored state;
* :meth:`ClimatePilotStore.async_load` converts *every* failure into ``{}``, so
  config-entry setup always completes and simply starts cold;
* every rejection is logged at ERROR level, so a silent cold start cannot hide
  a real storage problem (design doc 11, "HA 重启").
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY_PREFIX, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

#: Lowest payload version this integration can still read.
MIN_SUPPORTED_VERSION = 1


class StoreDataInvalid(HomeAssistantError):
    """Raised when persisted state exists but cannot be understood.

    Raising this from the migrate hook makes ``Store`` re-raise it out of
    ``async_load``, where :meth:`ClimatePilotStore.async_load` turns it into a
    cold start.  It is never safe to resume with a subset of the previous state
    while pretending it is complete.
    """


class ClimatePilotStore(Store[dict[str, Any]]):
    """Per-system persistent state, keyed by ``climatepilot.state.<system_id>``."""

    def __init__(self, hass: HomeAssistant, system_id: str) -> None:
        """Create the store for ``system_id`` (normally the config entry id)."""
        super().__init__(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}.{system_id}",
        )
        self.system_id = system_id

    async def async_load(self) -> dict[str, Any]:
        """Load the persisted payload, or ``{}`` when there is nothing usable.

        This method **never raises**.  ``{}`` means "start cold": either nothing
        was ever saved, or what was saved could not be trusted (corrupt file, a
        foreign ``system_id``, a version this build cannot read).  Starting cold
        is the safe outcome -- the coordinator re-reads the real device values
        and stays in its recovery grace period (design doc 11, "HA 重启") --
        whereas raising here would abort config-entry setup.

        Every rejection is logged at ERROR level so a silent cold start cannot
        hide a real storage problem.
        """
        try:
            raw = await super().async_load()
        except StoreDataInvalid as err:
            _LOGGER.error(
                "climatepilot: %s could not be read (%s); starting without restored state",
                self.key,
                err,
            )
            return {}
        except Exception:
            _LOGGER.exception(
                "climatepilot: failed to read %s; starting without restored state",
                self.key,
            )
            return {}

        if raw is None:
            return {}
        if not isinstance(raw, dict):
            _LOGGER.error(
                "climatepilot: %s holds %s, expected an object; ignoring it",
                self.key,
                type(raw).__name__,
            )
            return {}
        return dict(raw)

    async def async_save(self, data: dict[str, Any]) -> None:
        """Persist ``data``, stamping the schema version and timestamp.

        A save failure is logged rather than raised: losing persisted state
        degrades the next restart, but must never abort a running coordinator or
        an unload (design doc 11).
        """
        payload = self._prepare(data)
        try:
            await super().async_save(payload)
        except Exception:
            _LOGGER.exception("climatepilot: failed to save state to %s", self.key)

    async def async_remove(self) -> None:
        """Delete the persisted state entirely."""
        try:
            await super().async_remove()
        except Exception:
            _LOGGER.exception("climatepilot: failed to remove state %s", self.key)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _prepare(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON-safe copy of ``data`` with the schema header."""
        source = data if isinstance(data, dict) else {}
        payload: dict[str, Any] = {
            "version": STORAGE_VERSION,
            "system_id": self.system_id,
            "saved_at": dt_util.utcnow().isoformat(),
        }
        for key, value in source.items():
            if key in payload:
                # Never let a caller overwrite the schema header with its own
                # (possibly stale) values.
                continue
            payload[key] = value
        return payload

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate a stored payload to the current schema version.

        Invoked by ``Store._async_load_data`` whenever the stored version or
        minor version differs from this build's.  Only payloads this integration
        actually wrote are accepted; anything else raises
        :class:`StoreDataInvalid` so the coordinator starts cold instead of
        trusting unverified data.
        """
        if old_major_version < MIN_SUPPORTED_VERSION:
            raise StoreDataInvalid(
                f"{self.key!r} was written by an unsupported version {old_major_version}"
            )
        if old_major_version > STORAGE_VERSION:
            _LOGGER.warning(
                "climatepilot: %s was written by a newer version (%s > %s); "
                "starting from a cold state",
                self.key,
                old_major_version,
                STORAGE_VERSION,
            )
            raise StoreDataInvalid(
                f"{self.key!r} was written by a newer version {old_major_version}"
            )
        if not isinstance(old_data, dict):
            raise StoreDataInvalid(
                f"{self.key!r} holds {type(old_data).__name__}, expected an object"
            )

        stored_system_id = old_data.get("system_id")
        if stored_system_id is not None and stored_system_id != self.system_id:
            raise StoreDataInvalid(f"{self.key!r} was written for system {stored_system_id!r}")

        # v1 is current: re-stamp so the header always matches the schema.
        return self._prepare(dict(old_data))
