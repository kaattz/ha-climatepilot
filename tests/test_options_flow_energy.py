"""The energy step: two optional fields, and empty means the feature is off.

Kept in its own step rather than appended to ``heat_source``, which already
carries sixteen fields across three unrelated concerns (the boiler, the outdoor
source, and the compensation curve). Gas metering is a fourth concern with its own
subject - "what did the house consume" rather than "how should the boiler be
driven" - and mixing them would make an already long form harder to read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_platform_entities import _schema_invalid_class

from custom_components.climatepilot import const as c
from custom_components.climatepilot import options_flow as of

#: Every schema-rejection class this process may raise. Home Assistant 2026.9
#: installs probatio as ``voluptuous`` at import, and depending on import order both
#: libraries' ``MultipleInvalid`` classes can be reachable and unrelated - so
#: asserting one of them by name makes the test pass or fail by file ordering.
#: Reused from test_platform_entities rather than reimplemented, so the two cannot
#: disagree about which classes count.
SCHEMA_INVALID = _schema_invalid_class()

ROOT = Path(__file__).resolve().parents[1]
STRINGS = ROOT / "custom_components" / "climatepilot" / "strings.json"
EN = ROOT / "custom_components" / "climatepilot" / "translations" / "en.json"
ZH = ROOT / "custom_components" / "climatepilot" / "translations" / "zh-Hans.json"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_the_gas_config_keys_exist() -> None:
    assert c.CONF_GAS_METER_ENTITY == "gas_meter_entity"
    assert c.CONF_DEGREE_DAY_BASE_C == "degree_day_base_c"


def test_the_default_base_temperature_is_the_conventional_one() -> None:
    """15.5 C is a convention, so the exact value matters less than it being stated.

    Pinned so a future change is a deliberate act: shifting the base silently
    redefines every stored ``gas_per_degree_day`` figure, which would break
    comparability with earlier months without any error being raised.
    """
    assert c.DEFAULT_DEGREE_DAY_BASE_C == 15.5


def test_the_display_units_are_ascii_safe_and_fixed() -> None:
    """Statistics generation is suppressed when a unit changes between samples.

    These strings are therefore part of the contract, not presentation: if one
    ever varied, Home Assistant would stop recording long-term statistics for
    that entity and the history would quietly stop growing.
    """
    assert c.ENERGY_UNIT_GAS == "m\u00b3"
    assert c.ENERGY_UNIT_DEGREE_DAYS == "\u00b0C\u00b7d"
    assert c.ENERGY_UNIT_GAS_PER_DEGREE_DAY == "m\u00b3/\u00b0C\u00b7d"


def test_the_energy_step_is_in_the_options_menu() -> None:
    assert of.STEP_ENERGY in of.MENU_OPTIONS


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


def test_every_energy_field_has_a_label_in_all_three_files() -> None:
    """A field with no label renders as its raw key, as supply_probe_entity did."""
    expected = {c.CONF_GAS_METER_ENTITY, c.CONF_DEGREE_DAY_BASE_C}
    for path in (STRINGS, EN, ZH):
        tree = json.loads(path.read_text(encoding="utf-8"))
        data = tree["options"]["step"][of.STEP_ENERGY]["data"]
        assert set(data) == expected, f"{path.name}: {sorted(data)}"
        for key, label in data.items():
            assert label.strip(), f"{path.name}.{key} is blank"


def test_the_energy_step_has_a_title_and_description() -> None:
    for path in (STRINGS, EN, ZH):
        tree = json.loads(path.read_text(encoding="utf-8"))
        step = tree["options"]["step"][of.STEP_ENERGY]
        assert step.get("title", "").strip(), path.name
        assert step.get("description", "").strip(), path.name


def test_the_menu_entry_for_the_energy_step_is_translated() -> None:
    for path in (STRINGS, EN, ZH):
        tree = json.loads(path.read_text(encoding="utf-8"))
        init = tree["options"]["step"]["init"]
        assert of.STEP_ENERGY in init["menu_options"], path.name
        assert init["menu_options"][of.STEP_ENERGY].strip(), path.name


def test_the_chinese_description_states_that_the_meter_is_whole_house() -> None:
    """The one thing a user must not misunderstand about this feature.

    Without it, ``m3 per degree-day`` reads as a heating efficiency figure, and the
    user would draw conclusions about the boiler from a number that also contains
    their cooking and hot water.
    """
    tree = json.loads(ZH.read_text(encoding="utf-8"))
    description = tree["options"]["step"][of.STEP_ENERGY]["description"]
    assert "热水" in description
    assert "做饭" in description or "炊事" in description or "灶" in description


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_the_gas_meter_field_is_optional() -> None:
    schema = of.energy_schema({})
    keys = {str(k) for k in schema.schema}
    assert c.CONF_GAS_METER_ENTITY in keys
    assert c.CONF_DEGREE_DAY_BASE_C in keys

    # The gas meter must be Optional so an empty selection is accepted at all;
    # the base temperature is Required because it always has a usable default.
    marker = next(k for k in schema.schema if str(k) == c.CONF_GAS_METER_ENTITY)
    assert type(marker).__name__ == "Optional"


def _field_for(key: str, config: dict | None = None):
    """Return ``(marker, value)`` for ``key`` in a freshly built energy schema.

    Defaults and suggested values live on the *key marker* in voluptuous, not on
    the validator beside it, so reading them requires both halves of the pair.
    """
    schema = of.energy_schema(config or {})
    for marker, value in schema.schema.items():
        if str(marker) == key:
            return marker, value
    raise AssertionError(f"{key} not in schema")


def _default_of(marker) -> object:
    """Resolve a marker's default, which voluptuous may store as a callable."""
    default = marker.default
    return default() if callable(default) else default


def test_the_base_temperature_field_defaults_to_the_constant() -> None:
    marker, _ = _field_for(c.CONF_DEGREE_DAY_BASE_C)
    assert _default_of(marker) == c.DEFAULT_DEGREE_DAY_BASE_C
    # The form also pre-fills the visible control, not just the parse-time default.
    assert marker.description == {"suggested_value": c.DEFAULT_DEGREE_DAY_BASE_C}


def test_the_base_temperature_keeps_a_stored_value() -> None:
    """Reopening the form must not silently reset a configured base."""
    marker, _ = _field_for(c.CONF_DEGREE_DAY_BASE_C, {c.CONF_DEGREE_DAY_BASE_C: 18.0})
    assert _default_of(marker) == 18.0
    assert marker.description == {"suggested_value": 18.0}


def test_a_stored_gas_meter_entity_is_preselected() -> None:
    marker, _ = _field_for(c.CONF_GAS_METER_ENTITY, {c.CONF_GAS_METER_ENTITY: "sensor.gas_meter"})
    assert _default_of(marker) == "sensor.gas_meter"
    assert marker.description == {"suggested_value": "sensor.gas_meter"}


def test_a_blank_gas_meter_entity_means_off() -> None:
    """Clearing the field must mean "off", not be rejected.

    The feature is optional, and clearing the picker is the natural way to switch
    it off - so a validator demanding a value would leave no way to disable it
    without editing storage.

    "Blank" is ``None``, which is what the entity picker submits when nothing is
    selected; an omitted field is equivalent. An empty string is rejected, and
    deliberately so: measured against the six other optional entity fields in this
    integration, every one of them accepts ``None`` and rejects ``""``. Matching
    that keeps this field behaving like the rest rather than introducing a one-off
    rule.

    Asserted by validating through the schema rather than by reaching into the
    validator's attributes. Home Assistant 2026.9 replaces voluptuous with probatio
    at import, and the two implementations expose different internals - the
    behaviour is identical, so testing the behaviour keeps this passing on both.
    """
    schema = of.energy_schema({})
    base = {c.CONF_DEGREE_DAY_BASE_C: 15.5}

    assert schema({c.CONF_GAS_METER_ENTITY: None, **base})[c.CONF_GAS_METER_ENTITY] is None
    # An omitted optional field is absent from the validated output rather than
    # present as None - the flow reads it with ``.get()``, so either shape means
    # "off". Asserted as absence because that is what actually comes back on both
    # Home Assistant versions.
    assert c.CONF_GAS_METER_ENTITY not in schema(base)

    result = schema({c.CONF_GAS_METER_ENTITY: "sensor.gas_meter", **base})
    assert result[c.CONF_GAS_METER_ENTITY] == "sensor.gas_meter"

    with pytest.raises(SCHEMA_INVALID):
        schema({c.CONF_GAS_METER_ENTITY: "", **base})


def test_the_blank_handling_matches_the_other_optional_entity_fields() -> None:
    """The consistency claim above, asserted rather than asserted-in-prose.

    If a future change teaches the other optional entity fields to accept ``""``,
    this test fails and the divergence gets noticed instead of the two behaviours
    drifting apart silently - which is the failure mode that would make the field
    surprising rather than wrong.
    """
    boiler = of.boiler_schema({})
    base = {"boiler_entity": "climate.boiler"}

    for key in ("burner_state_entity", "return_temp_entity", "supply_probe_entity"):
        # Accepts None...
        assert boiler({**base, key: None})[key] is None
        # ...and rejects the empty string, exactly as the gas meter field does.
        with pytest.raises(SCHEMA_INVALID):
            boiler({**base, key: ""})


def test_the_base_temperature_is_validated_to_a_sane_range() -> None:
    """A base outside this window makes degree-days meaningless.

    Below roughly 10 C the figure ignores most of the heating season; above about
    25 C it accrues year-round, including in summer, at which point the ratio stops
    describing heating at all.

    The bounds live on the selector's own config and are enforced by Home Assistant
    when the form is rendered and submitted, so they are read back from there
    rather than by calling the selector as a function.
    """
    _, number = _field_for(c.CONF_DEGREE_DAY_BASE_C)
    config = number.config
    assert config["min"] == 10.0
    assert config["max"] == 25.0
    assert config["unit_of_measurement"] == "\u00b0C"
    assert config["min"] < c.DEFAULT_DEGREE_DAY_BASE_C < config["max"]
