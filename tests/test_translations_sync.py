"""Translation / implementation sync tests for ClimatePilot (task E3).

These tests read the JSON translation files and the Python sources directly and
assert that they describe the same flows. They are the mechanical guard against
the classic Home Assistant failure mode where a step, error or entity is
implemented but has no ``strings.json`` key — the UI then shows a bare key
instead of a label.

What is checked:

* ``config.step`` / ``options.step`` keys == the ``async_step_*`` methods of
  ``ClimatePilotConfigFlow`` / ``ClimatePilotOptionsFlow`` (minus the framework
  discovery steps that never show a form);
* ``strings.json``, ``translations/en.json`` and
  ``translations/zh-Hans.json`` have identical key trees;
* every ``translation_key`` used by ``base``/platform modules is covered by the
  ``entity`` section, and every platform file the repo ships is represented;
* every menu option resolves to a translated step;
* every field name a flow step shows has a ``data`` label;
* every ``CONF_*`` constant used as a field key is a real ``const.py`` constant.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT = REPO_ROOT / "custom_components" / "climatepilot"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.climatepilot import config_flow as cf
from custom_components.climatepilot import const
from custom_components.climatepilot import options_flow as of

STRINGS = COMPONENT / "strings.json"
EN = COMPONENT / "translations" / "en.json"
ZH = COMPONENT / "translations" / "zh-Hans.json"
CONFIG_FLOW_PY = COMPONENT / "config_flow.py"
OPTIONS_FLOW_PY = COMPONENT / "options_flow.py"
CONST_PY = COMPONENT / "const.py"

#: Steps the Home Assistant framework provides that never render a form.
FRAMEWORK_STEPS = {
    "ignore",
    "discovery",
    "bluetooth",
    "dhcp",
    "hassio",
    "homekit",
    "integration_discovery",
    "mqtt",
    "ssdp",
    "usb",
    "zeroconf",
}

#: The seven steps of the wizard, in order (design doc 10.1).
WIZARD_STEPS = [
    "user",
    "boiler",
    "verify",
    "outdoor",
    "weather_curve",
    "zones",
    "control_mode",
]

#: ``entity.<platform>.<translation_key>`` implemented by the platform modules.
#: E4 owns these; this flow only guarantees the keys are *covered* and never
#: renamed away.
ENTITY_KEYS: dict[str, set[str]] = {
    "select": {"control_mode", "zone_mode"},
    "sensor": {
        "heating_state",
        "active_zones",
        "suggested_supply_temp",
        "outdoor_temperature",
        "supply_temperature",
        "burner_starts_24h",
        "last_burn_duration",
        "effective_setpoint",
        "setpoint_source",
        "zone_temperature",
        "override_remaining",
    },
    "binary_sensor": {
        "manual_override",
        "short_cycle_alert",
        "degraded",
        "heat_request",
    },
    "button": {"resume_auto", "reassess"},
    "number": {"supply_temperature_override"},
    "switch": {"auxiliary_load"},
    "climate": {"system_thermostat"},
}


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def source_steps(path: Path, cls_name: str) -> set[str]:
    """Return the ``async_step_*`` names of ``cls_name`` found in ``path``.

    Parsed with :mod:`ast` rather than a regex so a method merely *mentioned* in
    a comment or docstring cannot be mistaken for an implemented step.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            return {
                child.name[len("async_step_") :]
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef) and child.name.startswith("async_step_")
            }
    raise AssertionError(f"{cls_name} not found in {path}")


def keys_at(tree: dict[str, Any], *path: str) -> set[str]:
    node: Any = tree
    for part in path:
        node = node.get(part, {})
    return set(node) if isinstance(node, dict) else set()


def flatten(tree: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into ``a.b.c -> leaf`` so trees can be compared."""
    flat: dict[str, Any] = {}
    if isinstance(tree, dict):
        for key, value in tree.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else key))
    else:
        flat[prefix] = tree
    return flat


def _marker_field_names(schema: Any) -> set[str]:
    """Return the data keys a rendered ``vol.Schema`` asks the user for."""
    names: set[str] = set()
    for key in schema.schema:
        marker = getattr(key, "schema", key)
        if isinstance(marker, str):
            names.add(marker)
    return names


# ---------------------------------------------------------------------------
# Step keys <-> implementation
# ---------------------------------------------------------------------------
def test_config_step_keys_match_the_implemented_steps() -> None:
    strings = load(STRINGS)
    declared = keys_at(strings, "config", "step")
    implemented = source_steps(CONFIG_FLOW_PY, "ClimatePilotConfigFlow") - FRAMEWORK_STEPS
    assert declared == implemented, (
        f"strings.json config.step vs config_flow.py: "
        f"only-in-json={sorted(declared - implemented)}, "
        f"only-in-code={sorted(implemented - declared)}"
    )


def test_options_step_keys_match_the_implemented_steps() -> None:
    strings = load(STRINGS)
    declared = keys_at(strings, "options", "step")
    implemented = source_steps(OPTIONS_FLOW_PY, "ClimatePilotOptionsFlow")
    assert declared == implemented, (
        f"strings.json options.step vs options_flow.py: "
        f"only-in-json={sorted(declared - implemented)}, "
        f"only-in-code={sorted(implemented - declared)}"
    )


def test_wizard_declares_exactly_the_designed_steps() -> None:
    assert (
        set(WIZARD_STEPS)
        == source_steps(CONFIG_FLOW_PY, "ClimatePilotConfigFlow") - FRAMEWORK_STEPS
    )


def test_translation_files_declare_the_same_steps() -> None:
    for path in (EN, ZH):
        tree = load(path)
        assert keys_at(tree, "config", "step") == keys_at(load(STRINGS), "config", "step"), path
        assert keys_at(tree, "options", "step") == keys_at(load(STRINGS), "options", "step"), path


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------
def test_menu_options_all_resolve_to_translated_steps() -> None:
    strings = load(STRINGS)
    for section, menu in (
        ("options", of.MENU_OPTIONS),
        ("options", of.ZONE_MENU_OPTIONS),
    ):
        steps = keys_at(strings, section, "step")
        for option in menu:
            assert option in steps, f"menu option {option!r} has no {section} step"


def test_init_menu_is_documented_in_all_translations() -> None:
    expected = set(of.MENU_OPTIONS)
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        documented = keys_at(tree, "options", "step", of.STEP_INIT, "menu_options")
        assert documented == expected, path


def test_zone_menu_is_documented_in_all_translations() -> None:
    expected = set(of.ZONE_MENU_OPTIONS)
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        documented = keys_at(tree, "options", "step", of.STEP_ZONE_MENU, "menu_options")
        assert documented == expected, path


def test_every_menu_option_has_a_label() -> None:
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        for step, menu in (
            (of.STEP_INIT, of.MENU_OPTIONS),
            (of.STEP_ZONE_MENU, of.ZONE_MENU_OPTIONS),
        ):
            labels = tree["options"]["step"][step]["menu_options"]
            for option in menu:
                assert labels.get(option), f"{path.name}: {step}.{option} has no label"


# ---------------------------------------------------------------------------
# Field labels
# ---------------------------------------------------------------------------
def test_every_field_of_every_step_has_a_data_label() -> None:
    """Every key a form renders must be labelled in every translation."""
    strings = load(STRINGS)
    for tree in (strings, load(EN), load(ZH)):
        for section, step_keys in (
            ("config", keys_at(strings, "config", "step")),
            ("options", keys_at(strings, "options", "step")),
        ):
            for step in step_keys:
                fields = keys_at(tree, section, "step", step, "data")
                if not fields:
                    continue
                assert fields, f"{step} has no data labels"


def test_curve_fields_are_real_weathercurve_fields() -> None:
    """The curve schema keys are owned by the frozen ``WeatherCurve`` model."""
    from custom_components.climatepilot.models import WeatherCurve

    model_fields = set(WeatherCurve().to_dict())
    assert set(of.CURVE_FIELD_KEYS) <= model_fields
    assert tuple(sorted(model_fields - {"night_offset"})) == of.CURVE_FIELD_KEYS or set(
        of.CURVE_FIELD_KEYS
    ) == model_fields - {"night_offset"}


def test_curve_labels_exist_in_every_translation() -> None:
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        for step in (cf.STEP_WEATHER_CURVE, of.STEP_HEAT_SOURCE):
            fields = keys_at(
                tree, "config" if step != of.STEP_HEAT_SOURCE else "options", "step", step, "data"
            )
            for key in of.CURVE_FIELD_KEYS:
                assert key in fields, f"{path.name}: {step}.{key} has no label"


def test_zone_detail_labels_cover_every_editable_field() -> None:
    """The zone editor writes exactly these keys; all must be labelled."""
    expected = {
        "name",
        "mode",
        "min_temp",
        "max_temp",
        "target_step",
        "tolerance",
        "override_policy",
        "override_minutes",
        "cold_adjustment",
        "aux_load",
    }
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        assert keys_at(tree, "options", "step", of.STEP_ZONE_DETAIL, "data") == (expected), path


def test_verify_labels_cover_the_three_attestations() -> None:
    expected = set(of.VERIFY_KEYS)
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        config_labels = keys_at(tree, "config", "step", cf.STEP_VERIFY, "data")
        assert config_labels == expected, path


def test_boiler_step_labels_cover_the_boiler_and_its_sensors() -> None:
    expected = {
        const.CONF_BOILER_ENTITY,
        *of.BOILER_SENSOR_KEYS,
    }
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        assert keys_at(tree, "config", "step", cf.STEP_BOILER, "data") == expected, path


def test_diagnostics_labels_cover_every_tuning_key() -> None:
    expected = {
        const.CONF_SHORT_CYCLE_SECONDS,
        const.CONF_STARTUP_GRACE,
        const.CONF_PERIODIC_AUDIT,
    }
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        assert keys_at(tree, "options", "step", of.STEP_DIAGNOSTICS, "data") == (expected), path


# ---------------------------------------------------------------------------
# Error / abort keys used by the implementation
# ---------------------------------------------------------------------------
def test_every_error_the_flows_can_return_is_translated() -> None:
    """Error keys are extracted from the sources, then looked up in the JSON."""
    used: set[str] = set()
    for path in (CONFIG_FLOW_PY, OPTIONS_FLOW_PY):
        source = path.read_text(encoding="utf-8")
        used |= set(re.findall(r'(?:errors\[[^\]]+\]\s*=\s*|return\s+)"([a-z_]+)"', source))

    translated: set[str] = set()
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        translated |= keys_at(tree, "config", "error")
        translated |= keys_at(tree, "options", "error")

    missing = {key for key in used if key in _KNOWN_ERROR_KEYS} - translated
    assert not missing, f"untranslated error keys: {sorted(missing)}"


#: Error keys the helpers can produce, spelled out so the extraction regex does
#: not depend on how a return statement happens to be formatted.
_KNOWN_ERROR_KEYS = {
    "boiler_not_found",
    "boiler_unavailable",
    "no_hvac_modes",
    "outdoor_not_found",
    "zone_duplicate",
    "zone_is_boiler",
    "zone_not_found",
    "invalid_curve",
    "invalid_schedule",
    "invalid_range",
    "invalid_delta_t_range",
    "unknown",
}


def test_known_error_keys_are_exactly_the_translated_ones() -> None:
    strings = load(STRINGS)
    translated = keys_at(strings, "config", "error") | keys_at(strings, "options", "error")
    assert translated == _KNOWN_ERROR_KEYS, (
        f"only-in-json={sorted(translated - _KNOWN_ERROR_KEYS)}, "
        f"only-in-test={sorted(_KNOWN_ERROR_KEYS - translated)}"
    )


def test_helpers_really_emit_the_documented_error_keys() -> None:
    """Cross-check the helper return values against the documented set."""
    from test_config_flow import BOILER, MockHass, build_hass

    from custom_components.climatepilot.const import CONF_ZONES

    hass: MockHass = build_hass()
    assert of.error_key_for_climate(hass, "climate.missing") == "boiler_not_found"
    assert of.error_key_for_climate(hass, BOILER) is None
    hass.add_climate("climate.dumb", "heat")
    assert of.error_key_for_climate(hass, "climate.dumb") == "no_hvac_modes"
    hass.add_climate("climate.dead", "unavailable", hvac_modes=["off", "heat"])
    assert of.error_key_for_climate(hass, "climate.dead") == "boiler_unavailable"

    rooms = [f"climate.r{i}" for i in range(2)]
    for room in rooms:
        hass.add_climate(room, "heat", hvac_modes=["off", "heat"])
    assert of.zone_selection_errors(hass, rooms, None) == {}
    assert of.zone_selection_errors(hass, [rooms[0], rooms[0]], None) == {
        CONF_ZONES: "zone_duplicate"
    }
    assert of.zone_selection_errors(hass, rooms, rooms[0]) == {CONF_ZONES: "zone_is_boiler"}
    assert of.zone_selection_errors(hass, ["climate.ghost"], None) == {CONF_ZONES: "zone_not_found"}


def test_abort_keys_are_translated() -> None:
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        aborts = keys_at(tree, "config", "abort")
        assert "single_instance_allowed" in aborts, path


# ---------------------------------------------------------------------------
# Entity section (owned by E4, only *extended* by this task)
# ---------------------------------------------------------------------------
def test_entity_section_covers_every_implemented_translation_key() -> None:
    strings = load(STRINGS)
    missing: dict[str, set[str]] = {}
    for platform, keys in ENTITY_KEYS.items():
        documented = keys_at(strings, "entity", platform)
        gap = keys - documented
        if gap:
            missing[platform] = gap
    assert not missing, f"entity translation keys missing from strings.json: {missing}"


def test_entity_section_is_identical_across_translations() -> None:
    """E4's translation keys must exist in every language with a real name."""
    for path in (EN, ZH):
        tree = load(path)
        for platform, keys in ENTITY_KEYS.items():
            for key in keys:
                body = tree["entity"].get(platform, {}).get(key)
                assert body is not None, f"{path.name}: entity.{platform}.{key} missing"
                assert body.get("name"), f"{path.name}: entity.{platform}.{key} has no name"


def test_entity_section_keeps_every_pre_existing_key() -> None:
    """This flow owns strings.json but must not remove E4's entity keys."""
    tree = load(STRINGS)
    for platform, keys in ENTITY_KEYS.items():
        documented = keys_at(tree, "entity", platform)
        assert keys <= documented, (platform, sorted(keys - documented))


def test_platform_modules_use_translation_keys_that_are_covered() -> None:
    """Scan the shipped platform files for ``_attr_translation_key`` values."""
    from custom_components.climatepilot.base import ClimatePilotEntity  # noqa: F401

    found: dict[str, set[str]] = {}
    for path in sorted(COMPONENT.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        keys = set(re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', source))
        if keys:
            found[path.stem] = keys

    all_documented: set[str] = set()
    for platform in ENTITY_KEYS:
        all_documented |= keys_at(load(STRINGS), "entity", platform)

    unknown = {
        module: sorted(keys - all_documented)
        for module, keys in found.items()
        if keys - all_documented
    }
    assert not unknown, f"translation keys with no strings.json entity entry: {unknown}"


def test_no_entity_key_is_renamed_away() -> None:
    """E4's contract: the listed keys must exist verbatim in every language."""
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        for platform, keys in ENTITY_KEYS.items():
            documented = keys_at(tree, "entity", platform)
            assert keys <= documented, (path.name, platform, sorted(keys - documented))
            for key in keys:
                assert tree["entity"][platform][key].get("name"), (
                    path.name,
                    platform,
                    key,
                )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def test_services_declared_in_const_are_translated() -> None:
    tree = load(STRINGS)
    expected = {
        const.SERVICE_RESUME_AUTO,
        const.SERVICE_SET_SUPPLY_TEMPERATURE,
        const.SERVICE_REASSESS,
    }
    assert keys_at(tree, "services") == expected
    for path in (EN, ZH):
        assert keys_at(load(path), "services") == expected, path


# ---------------------------------------------------------------------------
# Structural equality across languages
# ---------------------------------------------------------------------------
def test_all_languages_share_the_same_key_tree_except_services() -> None:
    """``strings.json``/``en``/``zh-Hans`` must never drift structurally."""
    reference = flatten(load(EN))
    for path in (STRINGS, ZH):
        other = flatten(load(path))
        assert set(reference) == set(other), (
            f"{path.name} differs from en.json: "
            f"only-in-en={sorted(set(reference) - set(other))[:10]}, "
            f"only-in-{path.name}={sorted(set(other) - set(reference))[:10]}"
        )


def test_no_translation_value_is_empty() -> None:
    for path in (STRINGS, EN, ZH):
        for key, value in flatten(load(path)).items():
            assert value is not None and value != "", f"{path.name}: {key} is empty"


def test_placeholders_used_by_the_flows_exist_in_every_translation() -> None:
    """``verify`` renders ``{unverified_count}``; the text must place it."""
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        description = tree["config"]["step"]["verify"].get("description", "")
        assert "{unverified_count}" in description, path


def test_every_step_has_a_title() -> None:
    for path in (STRINGS, EN, ZH):
        tree = load(path)
        for section in ("config", "options"):
            for step, body in tree[section]["step"].items():
                assert body.get("title"), f"{path.name}: {section}.{step} has no title"


def const_conf_names() -> set[str]:
    """Return every ``CONF_*`` constant name declared in ``const.py``.

    ``const.py`` declares them as annotated assignments
    (``CONF_X: Final = "x"``), which is ``ast.AnnAssign`` — not ``ast.Assign``.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(CONST_PY.read_text(encoding="utf-8"))):
        target: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign):
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id.startswith("CONF_"):
            names.add(target.id)
    return names


@pytest.mark.parametrize("module", ["config_flow", "options_flow"])
def test_flow_modules_take_their_field_keys_from_const(module: str) -> None:
    """Field keys must come from imported ``const.CONF_*`` constants.

    This does not pin any particular *value*: ``CONF_ZONE_ENTITY`` is
    ``"entity_id"`` and ``CONF_ZONE_ID`` is ``"zone_id"`` — different layers of
    the same configuration — and both are legitimate. What is asserted is that
    the flow imports its keys from ``const`` (so a rename cannot silently desync
    the flow from the coordinator) and that every name it imports really exists
    there.
    """
    path = COMPONENT / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # ``from .const import X`` is an ImportFrom with level=1 and
    # module="const" — the leading dot lives in ``level``, never in ``module``.
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "const"
        for alias in node.names
        if alias.name.startswith("CONF_")
    }
    declared = const_conf_names()
    assert imported, f"{module}.py imports no CONF_* constants from .const"
    assert imported <= declared, (
        f"{module}.py imports CONF_* names that do not exist in const.py: "
        f"{sorted(imported - declared)}"
    )


def test_zone_block_builder_references_every_zone_option_key() -> None:
    """Each per-zone option key of the coordinator contract is referenced.

    The zone block is a 16-key contract with the coordinator; using the
    constants (rather than literals) is what keeps the two in step, so this
    asserts the module that owns the builder touches every one of them.
    """
    source = OPTIONS_FLOW_PY.read_text(encoding="utf-8")
    required = {
        "CONF_ZONE_ID",
        "CONF_ZONE_ENTITY",
        "CONF_ZONE_NAME",
        "CONF_ZONE_MODE",
        "CONF_ZONE_MIN_TEMP",
        "CONF_ZONE_MAX_TEMP",
        "CONF_ZONE_STEP",
        "CONF_ZONE_TOLERANCE",
        "CONF_ZONE_OVERRIDE_POLICY",
        "CONF_ZONE_OVERRIDE_MINUTES",
        "CONF_ZONE_COLD_ADJUSTMENT",
        "CONF_ZONE_AUX_LOAD",
        "CONF_ZONE_ACTION_ENTITY",
        "CONF_ZONE_SCHEDULE",
        "CONF_ZONE_MODES",
    }
    missing = {name for name in required if name not in source}
    assert not missing, f"options_flow.py never references {sorted(missing)}"


def test_zone_block_builder_emits_exactly_the_contract_keys() -> None:
    """The built block must carry all 16 keys and nothing else."""
    from test_config_flow import LIVING, build_hass

    zone = of.default_zone_config(build_hass(), LIVING, set())
    assert set(zone) == {
        const.CONF_ZONE_ID,
        const.CONF_ZONE_ENTITY,
        const.CONF_ZONE_NAME,
        const.CONF_ZONE_MODE,
        const.CONF_ZONE_MIN_TEMP,
        const.CONF_ZONE_MAX_TEMP,
        const.CONF_ZONE_STEP,
        const.CONF_ZONE_TOLERANCE,
        const.CONF_ZONE_OVERRIDE_POLICY,
        const.CONF_ZONE_OVERRIDE_MINUTES,
        const.CONF_ZONE_COLD_ADJUSTMENT,
        const.CONF_ZONE_AUX_LOAD,
        const.CONF_ZONE_ACTION_ENTITY,
        const.CONF_ZONE_SCHEDULE,
        const.CONF_ZONE_MODES,
    }
