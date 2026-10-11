"""Config flow for ClimatePilot — the one-time system wizard.

The wizard walks the operator through the launch checklist of design doc 10.1
in seven steps::

    user -> boiler -> verify -> outdoor -> weather_curve -> zones -> control_mode

Rooms are optional here (they can be added later from the options flow), but
everything that *gates a write* is mandatory: the heat-source entity is
validated for ``hvac_modes``, and the three device-semantics attestations are
collected as explicit checkboxes that default to **off**. While any of them is
unconfirmed the integration stays in suggest-only mode (design doc 6.3 / 11).

The resulting entry keeps the system name in ``entry.data`` and the whole
configuration in ``entry.options``; the coordinator reads
``{**entry.data, **entry.options}``, so both halves are visible to it.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BOILER_ENTITY,
    CONF_CONTROL_MODE,
    CONF_OUTDOOR_STALE_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_SYSTEM_NAME,
    CONF_WEATHER_CURVE,
    CONF_ZONE_ID,
    CONF_ZONES,
    CONTROL_MODE_OBSERVE,
    DEFAULT_NAME,
    DOMAIN,
)
from .options_flow import (
    BOILER_SENSOR_KEYS,
    VERIFY_KEYS,
    ClimatePilotOptionsFlow,
    boiler_schema,
    climate_selector,
    control_mode_selector,
    curve_from_input,
    curve_schema,
    default_zone_config,
    error_key_for_climate,
    outdoor_schema,
    validate_curve,
    verify_schema,
    zone_selection_errors,
)

_LOGGER = logging.getLogger(__name__)

#: Step ids of this flow — the keys of ``config.step`` in strings.json.
STEP_USER: Final = "user"
STEP_BOILER: Final = "boiler"
STEP_VERIFY: Final = "verify"
STEP_OUTDOOR: Final = "outdoor"
STEP_WEATHER_CURVE: Final = "weather_curve"
STEP_ZONES: Final = "zones"
STEP_CONTROL_MODE: Final = "control_mode"


class ClimatePilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single ClimatePilot heating system."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow's draft configuration."""
        self._config: dict[str, Any] = {}

    # -- helpers -------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name the system; refuse a second instance."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            name = str(user_input[CONF_SYSTEM_NAME]).strip() or DEFAULT_NAME
            self._config[CONF_SYSTEM_NAME] = name
            # A fixed unique id keeps the entry addressable by domain even
            # though only one instance may exist.
            await self.async_set_unique_id(DOMAIN)
            return await self.async_step_boiler()

        schema = vol.Schema(
            {vol.Required(CONF_SYSTEM_NAME, default=DEFAULT_NAME): (selector.TextSelector())}
        )
        return self.async_show_form(step_id=STEP_USER, data_schema=schema)

    async def async_step_boiler(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the heat-source climate entity, its sensors and pipe probes.

        Every sensor role in :data:`BOILER_SENSOR_KEYS` is optional, including the
        V0.3 strap-on supply/return probes: an installation without them is fully
        supported and simply reports ``unknown`` for the probe entities.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            boiler = user_input[CONF_BOILER_ENTITY]
            error = error_key_for_climate(self.hass, boiler)
            if error:
                errors[CONF_BOILER_ENTITY] = error
            else:
                self._config[CONF_BOILER_ENTITY] = boiler
                for key in BOILER_SENSOR_KEYS:
                    self._config[key] = user_input.get(key) or None
                return await self.async_step_verify()

        return self.async_show_form(
            step_id=STEP_BOILER,
            data_schema=boiler_schema(self._config),
            errors=errors,
        )

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the three device-semantics attestations."""
        if user_input is not None:
            for key in VERIFY_KEYS:
                self._config[key] = bool(user_input.get(key, False))
            return await self.async_step_outdoor()

        return self.async_show_form(
            step_id=STEP_VERIFY,
            data_schema=verify_schema(self._config),
            # Advisory, not an error: leaving these unchecked is a supported
            # (suggest-only) configuration, so the form must not block.
            description_placeholders={
                "unverified_count": str(sum(1 for key in VERIFY_KEYS if not self._config.get(key)))
            },
        )

    async def async_step_outdoor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the outdoor temperature source and its staleness window."""
        errors: dict[str, str] = {}

        if user_input is not None:
            outdoor = user_input.get(CONF_OUTDOOR_TEMP_ENTITY) or None
            if outdoor is not None and self.hass.states.get(outdoor) is None:
                errors[CONF_OUTDOOR_TEMP_ENTITY] = "outdoor_not_found"
            else:
                self._config[CONF_OUTDOOR_TEMP_ENTITY] = outdoor
                self._config[CONF_OUTDOOR_STALE_MINUTES] = float(
                    user_input[CONF_OUTDOOR_STALE_MINUTES]
                )
                return await self.async_step_weather_curve()

        return self.async_show_form(
            step_id=STEP_OUTDOOR,
            data_schema=outdoor_schema(self._config),
            errors=errors,
        )

    async def async_step_weather_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Define the two-point linear weather-compensation curve."""
        errors: dict[str, str] = {}

        if user_input is not None:
            curve = curve_from_input(user_input)
            error = validate_curve(curve)
            if error:
                errors["base"] = error
            else:
                self._config[CONF_WEATHER_CURVE] = curve
                return await self.async_step_zones()

        return self.async_show_form(
            step_id=STEP_WEATHER_CURVE,
            data_schema=curve_schema(self._config),
            errors=errors,
        )

    async def async_step_zones(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Select the room thermostats (zero rooms is allowed)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = [e for e in (user_input.get(CONF_ZONES) or []) if e]
            errors = zone_selection_errors(
                self.hass, selected, self._config.get(CONF_BOILER_ENTITY)
            )
            if not errors:
                self._config[CONF_ZONES] = selected
                return await self.async_step_control_mode()

        current = [str(entity) for entity in self._config.get(CONF_ZONES) or []]
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ZONES,
                    default=current,
                    description={"suggested_value": current},
                ): vol.Any(None, climate_selector(multiple=True))
            }
        )
        return self.async_show_form(step_id=STEP_ZONES, data_schema=schema, errors=errors)

    def _build_zones(self) -> list[dict[str, Any]]:
        """Turn the selected climate entities into per-zone option blocks."""
        taken: set[str] = set()
        zones: list[dict[str, Any]] = []
        for entity_id in self._config.get(CONF_ZONES) or []:
            zone = default_zone_config(self.hass, str(entity_id), taken)
            taken.add(str(zone[CONF_ZONE_ID]))
            zones.append(zone)
        return zones

    async def async_step_control_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the initial control mode and finish."""
        if user_input is not None:
            self._config[CONF_CONTROL_MODE] = str(user_input[CONF_CONTROL_MODE])
            return self._create_entry()

        schema = vol.Schema(
            {vol.Required(CONF_CONTROL_MODE, default=CONTROL_MODE_OBSERVE): control_mode_selector()}
        )
        return self.async_show_form(step_id=STEP_CONTROL_MODE, data_schema=schema)

    @callback
    def _create_entry(self) -> ConfigFlowResult:
        """Create the entry, keeping the name in data and the rest in options."""
        name = str(self._config.get(CONF_SYSTEM_NAME) or DEFAULT_NAME)
        options = dict(self._config)
        # ``CONF_ZONES`` holds bare entity ids while the wizard runs; the entry
        # stores the full per-zone option blocks the coordinator parses.
        options[CONF_ZONES] = self._build_zones()
        return self.async_create_entry(
            title=name,
            data={CONF_SYSTEM_NAME: name},
            options=options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow that edits this entry."""
        return ClimatePilotOptionsFlow()


__all__ = [
    "STEP_BOILER",
    "STEP_CONTROL_MODE",
    "STEP_OUTDOOR",
    "STEP_USER",
    "STEP_VERIFY",
    "STEP_WEATHER_CURVE",
    "STEP_ZONES",
    "ClimatePilotConfigFlow",
]
