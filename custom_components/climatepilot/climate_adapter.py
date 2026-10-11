"""Home Assistant adapter for the ClimatePilot heating coordinator.

The engines (``temperature_arbiter``, ``zone_coordinator``, ``heat_source``)
never touch Home Assistant.  Everything that needs a live entity lives here:

* :meth:`ClimateAdapter.snapshot` reads a ``climate`` entity into an immutable
  :class:`ClimateSnapshot` (never raises, never returns ``None``);
* :meth:`ClimateAdapter.to_zone_state` folds that snapshot into a
  :class:`~.models.ZoneState` for the pure engines;
* ``async_set_*`` write a value back and report success as a boolean.

Contract notes (design doc 4.4, 4.7, 9.1, 11):

* Only ``climate.set_temperature`` and ``climate.set_hvac_mode`` are ever
  called, always by ``entity_id`` with ``blocking=True``.  KNX group addresses
  are never written directly and ``device_id`` targeting is never used: the
  coordinator needs a deterministic entity -> value mapping so it can compare a
  device echo against its own pending command (design doc 4.4).
* Every failure path returns ``False`` instead of raising.  The coordinator owns
  retry limits and backoff, and a communication error must not unwind it.
* ``unavailable`` / ``unknown`` is *unknown demand*, never "no demand"
  (design doc 11).  Feedback quality degrades accordingly.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from typing import Any

from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, callback

from .const import (
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
    ZONE_MODES,
)
from .models import ZoneState

_LOGGER = logging.getLogger(__name__)

#: Home Assistant domain whose services this adapter is allowed to call.
HVAC_DOMAIN = "climate"
SERVICE_SET_TEMPERATURE = "set_temperature"
SERVICE_SET_HVAC_MODE = "set_hvac_mode"

# Keys of a zone config entry (``const.CONF_ZONES`` list items).
CONF_ZONE_ID = "zone_id"
CONF_ZONE_ENTITY = "entity_id"
CONF_ZONE_NAME = "name"
CONF_ZONE_MODE = "mode"
CONF_ZONE_MIN_TEMP = "min_temp"
CONF_ZONE_MAX_TEMP = "max_temp"
CONF_ZONE_TARGET_STEP = "target_step"
CONF_ZONE_TOLERANCE = "tolerance"

#: States in which a climate entity carries no trustworthy feedback at all.
_NULL_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


@dataclass(frozen=True, slots=True)
class ClimateSnapshot:
    """An immutable read of one ``climate`` entity at one point in time.

    ``exists`` is ``False`` for an entity that is not (or no longer) in the
    state machine; every other field is then ``None``/empty.  Callers must check
    ``exists`` before trusting any value.

    ``last_changed`` is the last *state* transition while ``last_updated`` also
    covers attribute-only updates.  The coordinator uses ``last_changed`` to
    detect a genuine device-side setpoint change (a manual operation, design doc
    4.4) and ``last_updated`` for freshness/``is_stale``.
    """

    entity_id: str
    exists: bool
    state: str | None
    hvac_action: str | None
    temperature: float | None
    current_temperature: float | None
    min_temp: float | None
    max_temp: float | None
    target_step: float | None
    hvac_modes: tuple[str, ...]
    last_updated: datetime | None
    supports_action: bool
    last_changed: datetime | None = None


class ClimateAdapter:
    """Thin, fail-safe wrapper around the ``climate`` domain."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the adapter to a Home Assistant instance."""
        self.hass = hass

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    @callback
    def snapshot(self, entity_id: str) -> ClimateSnapshot:
        """Return the current state of ``entity_id`` as a snapshot.

        A missing entity yields ``exists=False`` rather than raising
        ``KeyError``; an entity that is not a ``climate`` entity or that carries
        malformed attributes yields a partial snapshot with ``exists=True``.
        """
        if not entity_id:
            return self._missing(entity_id)

        states = getattr(self.hass, "states", None)
        if states is None:  # pragma: no cover - defensive, hass always has states
            return self._missing(entity_id)

        state_obj = states.get(entity_id)
        if state_obj is None:
            return self._missing(entity_id)

        attributes = getattr(state_obj, "attributes", None)
        if not isinstance(attributes, Mapping):
            attributes = {}

        raw_state = getattr(state_obj, "state", None)
        state = str(raw_state) if raw_state is not None else None

        hvac_action = self._optional_str(attributes.get("hvac_action"))
        min_temp = self._optional_float(attributes.get("min_temp"))
        max_temp = self._optional_float(attributes.get("max_temp"))
        target_step = self._optional_float(attributes.get("target_temp_step"))

        return ClimateSnapshot(
            entity_id=entity_id,
            exists=True,
            state=state,
            hvac_action=hvac_action,
            temperature=self._optional_float(attributes.get("temperature")),
            current_temperature=self._optional_float(attributes.get("current_temperature")),
            min_temp=min_temp,
            max_temp=max_temp,
            target_step=target_step,
            hvac_modes=self._mode_tuple(attributes.get("hvac_modes")),
            last_updated=self._as_datetime(getattr(state_obj, "last_updated", None)),
            # ``hvac_action`` is only a real capability signal when the entity
            # advertises the mode/action attributes at all.  A device that
            # reports the attribute but no value is merely uncertain.
            supports_action=hvac_action is not None and state not in _NULL_STATES,
            last_changed=self._as_datetime(getattr(state_obj, "last_changed", None)),
        )

    def to_zone_state(self, zone_config: Mapping[str, Any], snapshot: ClimateSnapshot) -> ZoneState:
        """Merge a zone's configured options with the live device snapshot.

        Range handling follows the ``ZoneState`` P0 contract:

        * ``config_min_temp`` / ``config_max_temp`` come from the user's options,
          falling back to ``const.DEFAULT_*`` when unset;
        * ``device_min_temp`` / ``device_max_temp`` mirror what the thermostat
          reports, and stay ``None`` when the device does not report a range;
        * ``refresh_limits()`` then computes the effective ``min_temp`` /
          ``max_temp`` as the intersection.

        Only ``target_step`` and ``tolerance`` still resolve
        ``config`` -> device -> ``const.DEFAULT_*`` directly.
        """
        config: Mapping[str, Any] = zone_config if zone_config is not None else {}

        zone_id = str(config.get(CONF_ZONE_ID) or config.get("id") or snapshot.entity_id)
        entity_id = str(config.get(CONF_ZONE_ENTITY) or snapshot.entity_id)
        name = str(config.get(CONF_ZONE_NAME) or zone_id)

        config_min = self._optional_float(config.get(CONF_ZONE_MIN_TEMP))
        if config_min is None:
            config_min = DEFAULT_MIN_TEMP
        config_max = self._optional_float(config.get(CONF_ZONE_MAX_TEMP))
        if config_max is None:
            config_max = DEFAULT_MAX_TEMP

        device_min = snapshot.min_temp
        device_max = snapshot.max_temp
        if device_min is not None and device_max is not None and device_max < device_min:
            # A device reporting an inverted range must not produce an
            # unsatisfiable window; drop it and let the config range govern.
            _LOGGER.warning(
                "climatepilot: %s reports max_temp %s < min_temp %s; ignoring the device range",
                snapshot.entity_id,
                device_max,
                device_min,
            )
            device_min = None
            device_max = None

        target_step = self._first_float(
            config.get(CONF_ZONE_TARGET_STEP),
            snapshot.target_step,
            DEFAULT_TARGET_STEP,
        )
        if target_step is None or target_step <= 0:
            target_step = DEFAULT_TARGET_STEP

        tolerance = self._first_float(config.get(CONF_ZONE_TOLERANCE), DEFAULT_ZONE_TOLERANCE)
        if tolerance is None or tolerance < 0:
            tolerance = DEFAULT_ZONE_TOLERANCE

        usable = snapshot.exists and snapshot.state not in _NULL_STATES
        if not usable:
            feedback_quality = FEEDBACK_UNAVAILABLE
        elif snapshot.supports_action:
            feedback_quality = FEEDBACK_VALID
        else:
            # The setpoint may be known while the running state is not; the
            # engines must treat demand as unproven rather than absent.
            feedback_quality = FEEDBACK_UNCERTAIN

        zone = ZoneState(
            zone_id=zone_id,
            entity_id=entity_id,
            name=name,
            mode=self._zone_mode(config.get(CONF_ZONE_MODE)),
            # A snapshot only reports a confirmed value; an unread or
            # non-numeric setpoint stays ``None`` instead of being guessed.
            base_setpoint=snapshot.temperature if snapshot.temperature is not None else 20.0,
            effective_setpoint=None,
            actual_setpoint=snapshot.temperature,
            current_temperature=snapshot.current_temperature,
            feedback_quality=feedback_quality,
            hvac_action=snapshot.hvac_action,
            hvac_mode=snapshot.state,
            target_step=target_step,
            tolerance=tolerance,
            supports_action=snapshot.supports_action,
            hvac_action_entity=self._optional_str(config.get("hvac_action_entity")),
            config_min_temp=config_min,
            config_max_temp=config_max,
            device_min_temp=device_min,
            device_max_temp=device_max,
        )
        zone.refresh_limits()
        return zone

    @staticmethod
    def is_stale(snapshot: ClimateSnapshot, now: datetime, max_age: float) -> bool:
        """Return True when ``snapshot`` is older than ``max_age`` seconds.

        A snapshot without a timestamp, or one whose timestamp cannot be
        compared with ``now`` (naive vs aware, clock skew), counts as stale: the
        safe answer for a coordinator that must not act on unproven feedback.
        """
        if not snapshot.exists or snapshot.last_updated is None:
            return True
        try:
            age = (now - snapshot.last_updated).total_seconds()
        except TypeError:
            return True
        if age < 0:
            # A timestamp in the future is not evidence of freshness, but it is
            # also not evidence of staleness; surface it as "not stale" and let
            # the caller's own timeout logic decide.
            return False
        return age > max_age

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    async def async_set_temperature(self, entity_id: str, temperature: float) -> bool:
        """Write a zone setpoint via ``climate.set_temperature``.

        Returns ``True`` only when the service call was accepted.  Every refusal
        (missing/unavailable entity, non-finite value, raised exception) returns
        ``False``.
        """
        value = self._optional_float(temperature)
        if value is None:
            _LOGGER.warning(
                "climatepilot: refusing to write a non-numeric setpoint %r to %s",
                temperature,
                entity_id,
            )
            return False
        return await self._async_write(
            entity_id, SERVICE_SET_TEMPERATURE, {ATTR_TEMPERATURE: value}
        )

    async def async_set_hvac_mode(self, entity_id: str, mode: str) -> bool:
        """Write a hvac mode via ``climate.set_hvac_mode``.

        A mode the entity does not advertise in ``hvac_modes`` is rejected
        without a service call: Home Assistant would reject it anyway, and the
        coordinator's retry budget is better spent on writeable commands.
        """
        if not mode:
            _LOGGER.warning("climatepilot: refusing to write an empty hvac mode to %s", entity_id)
            return False
        return await self._async_write(
            entity_id, SERVICE_SET_HVAC_MODE, {"hvac_mode": mode}, hvac_mode=mode
        )

    async def async_set_supply_temperature(self, entity_id: str, temperature: float) -> bool:
        """Write the boiler flow setpoint.

        This is ``climate.set_temperature`` against the heat source entity; only
        the caller's verified capability check makes it a *supply* temperature
        (design doc 6.3 / 14).  The adapter deliberately does not decide that.
        """
        return await self.async_set_temperature(entity_id, temperature)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _async_write(
        self,
        entity_id: str,
        service: str,
        data: dict[str, Any],
        *,
        hvac_mode: str | None = None,
    ) -> bool:
        """Call ``climate.<service>`` by entity_id, converting errors to False."""
        if not entity_id:
            _LOGGER.warning(
                "climatepilot: refusing to call climate.%s without an entity_id",
                service,
            )
            return False

        # No ``await`` between this check and the call, so the entity cannot
        # change underneath us.
        snapshot = self.snapshot(entity_id)
        if not snapshot.exists:
            _LOGGER.warning(
                "climatepilot: cannot call climate.%s, entity %s does not exist",
                service,
                entity_id,
            )
            return False
        if snapshot.state in _NULL_STATES:
            _LOGGER.warning(
                "climatepilot: cannot call climate.%s, entity %s is %s",
                service,
                entity_id,
                snapshot.state,
            )
            return False
        if hvac_mode is not None and snapshot.hvac_modes and hvac_mode not in snapshot.hvac_modes:
            _LOGGER.warning(
                "climatepilot: %s does not advertise hvac mode %r (has %s)",
                entity_id,
                hvac_mode,
                list(snapshot.hvac_modes),
            )
            return False

        try:
            await self.hass.services.async_call(
                HVAC_DOMAIN,
                service,
                {"entity_id": entity_id, **data},
                blocking=True,
            )
        except Exception:
            _LOGGER.warning(
                "climatepilot: climate.%s failed for %s",
                service,
                entity_id,
                exc_info=True,
            )
            return False
        return True

    @staticmethod
    def _missing(entity_id: str) -> ClimateSnapshot:
        return ClimateSnapshot(
            entity_id=entity_id,
            exists=False,
            state=None,
            hvac_action=None,
            temperature=None,
            current_temperature=None,
            min_temp=None,
            max_temp=None,
            target_step=None,
            hvac_modes=(),
            last_updated=None,
            supports_action=False,
            last_changed=None,
        )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        """Coerce a real number to a finite float, or return ``None``.

        Strings and booleans are rejected on purpose: this integration never
        guesses a temperature (design doc, 铁律 4).  ``None``/``NaN``/``inf``
        from a half-broken device become ``None`` instead of poisoning an
        arithmetic path.
        """
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        result = float(value)
        if not math.isfinite(result):
            return None
        return result

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _first_float(cls, *candidates: Any) -> float | None:
        """Return the first candidate that is a usable finite float."""
        for candidate in candidates:
            value = cls._optional_float(candidate)
            if value is not None:
                return value
        return None

    @staticmethod
    def _mode_tuple(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(str(item) for item in value)
        return ()

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        return value if isinstance(value, datetime) else None

    @staticmethod
    def _zone_mode(value: Any) -> str:
        mode = ClimateAdapter._optional_str(value)
        if mode in ZONE_MODES:
            return mode
        return ZONE_MODE_AUTO
