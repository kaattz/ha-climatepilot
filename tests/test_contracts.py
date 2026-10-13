"""Cross-module consistency checks.

The individual suites prove that each module behaves correctly in isolation. This
module asserts the properties that only exist *between* modules - the contracts
that silently rot when one side is edited and the other is not:

* every ``_attr_translation_key`` shipped in a platform exists in
  ``strings.json`` (otherwise Home Assistant renders an empty name);
* every state a sensor can emit is covered by a ``state`` translation;
* the pure engines stay importable without Home Assistant;
* no module hardcodes a device_id where an entity_id belongs;
* the reason codes and sources quoted in the docs really exist in ``const``.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "climatepilot"

sys.path.insert(0, str(ROOT))


def load_strings() -> dict:
    return json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))


def platform_files() -> list[Path]:
    return [
        p
        for p in sorted(COMPONENT.glob("*.py"))
        if p.name
        in {
            "sensor.py",
            "binary_sensor.py",
            "select.py",
            "button.py",
            "number.py",
            "switch.py",
            "climate.py",
        }
    ]


# ---------------------------------------------------------------------------
# Translation coverage
# ---------------------------------------------------------------------------


def test_every_shipped_translation_key_exists_in_strings():
    """A missing key means Home Assistant shows an empty entity name."""
    strings = load_strings()
    covered = strings.get("entity", {})
    missing: list[tuple[str, str, str]] = []

    for path in platform_files():
        platform = path.stem
        for key in re.findall(
            r'_attr_translation_key\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8")
        ):
            if key not in covered.get(platform, {}):
                missing.append((platform, key, path.name))

    assert not missing, f"translation keys missing from strings.json: {missing}"


def test_no_orphan_entity_translation_keys():
    """The reverse direction: a key documented but never implemented.

    ``test_every_shipped_translation_key_exists_in_strings`` catches the case
    where a platform ships a key with no translation. This catches the mirror
    image - a translation left behind after its entity was renamed or removed.
    Home Assistant ignores such a key silently, so without this test it survives
    indefinitely and the *next* reader trusts a label for an entity that does
    not exist.

    The comparison is deliberately exact and two-way: an entity key must exist in
    strings.json (upward) *and* strings.json must not describe an entity key no
    platform ships (downward).
    """
    strings = load_strings()
    documented = strings.get("entity", {})

    implemented: dict[str, set[str]] = {}
    for path in platform_files():
        keys = set(
            re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
        )
        if keys:
            implemented[path.stem] = keys

    orphans = {
        platform: sorted(set(documented.get(platform, {})) - implemented.get(platform, set()))
        for platform in documented
    }
    orphans = {platform: keys for platform, keys in orphans.items() if keys}
    assert not orphans, f"entity translation keys with no implementing entity: {orphans}"

    # A platform dictionary with no module at all is also an orphan (e.g. the
    # section was kept after the platform file was deleted).
    stray_platforms = sorted(set(documented) - set(implemented))
    assert not stray_platforms, (
        f"strings.json declares entity sections for platforms that ship no module: {stray_platforms}"
    )


def test_state_translations_cover_the_constants_actually_used():
    """Sensor states are enum values; each needs a ``state`` translation."""
    strings = load_strings()

    from custom_components.climatepilot.const import (
        CONTROL_MODES,
        FSM_STATES,
        SOURCE_AUX_LOAD,
        SOURCE_COLD_ADJUSTMENT,
        SOURCE_FALLBACK,
        SOURCE_FIXED_MODE,
        SOURCE_MANUAL,
        SOURCE_SCHEDULE,
        ZONE_MODES,
    )

    entities = strings["entity"]

    # select.control_mode <- CONTROL_MODES
    documented = set(entities["select"]["control_mode"]["state"])
    assert set(CONTROL_MODES) <= documented

    # select.zone_mode <- ZONE_MODES
    documented = set(entities["select"]["zone_mode"]["state"])
    assert set(ZONE_MODES) <= documented

    # sensor.heating_state <- FSM_STATES
    documented = set(entities["sensor"]["heating_state"]["state"])
    assert set(FSM_STATES) <= documented, sorted(set(FSM_STATES) - documented)

    # sensor.setpoint_source <- SOURCE_*
    documented = set(entities["sensor"]["setpoint_source"]["state"])
    sources = {
        SOURCE_MANUAL,
        SOURCE_FIXED_MODE,
        SOURCE_SCHEDULE,
        SOURCE_COLD_ADJUSTMENT,
        SOURCE_AUX_LOAD,
        SOURCE_FALLBACK,
    }
    assert sources <= documented, sorted(sources - documented)


def test_v02_state_translations_match_the_constants_exactly():
    """The V0.2 enum sensors must be translated *exactly*, not merely covered.

    A typo in one of these literal keys is invisible in code review but shows up
    in the UI as the raw code (``check_hydraulics_or_balancing``), so the
    documented set is compared both ways against ``const`` rather than by subset.
    """
    from custom_components.climatepilot.const import (
        ADVICE_CHECK_HYDRAULICS,
        ADVICE_CHECK_SCHEDULE,
        ADVICE_LOWER_SUPPLY,
        ADVICE_NONE,
        ADVICE_RAISE_SUPPLY,
        COMFORT_OK,
        COMFORT_OVERHEATING,
        COMFORT_UNDERHEATING,
        COMFORT_UNKNOWN,
        QUALITY_FAIR,
        QUALITY_GOOD,
        QUALITY_POOR,
        QUALITY_UNKNOWN,
    )

    expected = {
        "comfort_verdict": {
            COMFORT_OK,
            COMFORT_OVERHEATING,
            COMFORT_UNDERHEATING,
            COMFORT_UNKNOWN,
        },
        "comfort_advice": {
            ADVICE_LOWER_SUPPLY,
            ADVICE_RAISE_SUPPLY,
            ADVICE_CHECK_HYDRAULICS,
            ADVICE_CHECK_SCHEDULE,
            ADVICE_NONE,
        },
        "cycling_quality": {QUALITY_GOOD, QUALITY_FAIR, QUALITY_POOR, QUALITY_UNKNOWN},
    }

    for name, path in (
        ("strings.json", COMPONENT / "strings.json"),
        ("en.json", COMPONENT / "translations" / "en.json"),
        ("zh-Hans.json", COMPONENT / "translations" / "zh-Hans.json"),
    ):
        entity = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        for key, literals in expected.items():
            documented = set(entity[key].get("state", {}))
            assert documented == literals, (
                f"{name}: sensor.{key} state keys differ from const.py: "
                f"missing={sorted(literals - documented)}, "
                f"unknown={sorted(documented - literals)}"
            )


def test_v03_state_translations_match_the_constants_exactly():
    """The V0.3 probe enum sensors must be translated *exactly*.

    Same reasoning as the V0.2 counterpart: these state keys are raw enum
    values emitted by the entities, so a typo shows up in the UI as the raw
    code (``raise_supply_curve``) rather than as an error. The comparison is
    two-way against ``const`` in all three files.

    ``delta_t_quality`` is checked against ``PROBE_QUALITIES`` rather than a
    hand-written list so the ordering/severity list in ``const`` stays the
    single source of truth.
    """
    from custom_components.climatepilot.const import (
        CALIBRATION_INSUFFICIENT_DATA,
        CALIBRATION_LOWER_CURVE,
        CALIBRATION_NONE,
        CALIBRATION_RAISE_CURVE,
        CONDENSING_LIKELY,
        CONDENSING_UNKNOWN,
        CONDENSING_UNLIKELY,
        HYDRAULIC_HIGH_FLOW,
        HYDRAULIC_LOW_FLOW,
        HYDRAULIC_OK,
        HYDRAULIC_UNBALANCED,
        HYDRAULIC_UNKNOWN,
        PROBE_QUALITIES,
    )

    expected = {
        "delta_t_quality": set(PROBE_QUALITIES),
        "condensing_regime": {CONDENSING_LIKELY, CONDENSING_UNLIKELY, CONDENSING_UNKNOWN},
        "hydraulic_verdict": {
            HYDRAULIC_OK,
            HYDRAULIC_LOW_FLOW,
            HYDRAULIC_HIGH_FLOW,
            HYDRAULIC_UNBALANCED,
            HYDRAULIC_UNKNOWN,
        },
        "curve_calibration_advice": {
            CALIBRATION_NONE,
            CALIBRATION_RAISE_CURVE,
            CALIBRATION_LOWER_CURVE,
            CALIBRATION_INSUFFICIENT_DATA,
        },
    }

    for name, path in (
        ("strings.json", COMPONENT / "strings.json"),
        ("en.json", COMPONENT / "translations" / "en.json"),
        ("zh-Hans.json", COMPONENT / "translations" / "zh-Hans.json"),
    ):
        entity = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
        for key, literals in expected.items():
            documented = set(entity[key].get("state", {}))
            assert documented == literals, (
                f"{name}: sensor.{key} state keys differ from const.py: "
                f"missing={sorted(literals - documented)}, "
                f"unknown={sorted(documented - literals)}"
            )


def test_v03_entity_keys_match_the_english_name_slug_contract():
    """``slugify(英文名) == translation_key`` for the 7 V0.3 probe entities.

    ``docs/INTERFACES_V0.3.md`` freezes this because the entity_id slug is
    derived from the *English* name even under a Chinese UI, while the static
    dashboard YAML has to hardcode those ids. If the two owners each derive the
    slug their own way they drift (which is exactly what happened in V0.2), so
    the equality is asserted here rather than assumed on both sides.
    """
    from homeassistant.util import slugify

    frozen = {
        "return_temperature": "Return temperature",
        "delta_t": "Delta T",
        "delta_t_quality": "Delta T quality",
        "condensing_regime": "Condensing regime",
        "hydraulic_verdict": "Hydraulic verdict",
        "curve_calibration_advice": "Curve calibration advice",
        "probe_fault": "Probe fault",
    }

    for name in ("strings.json", "en.json"):
        path = COMPONENT / name if name == "strings.json" else COMPONENT / "translations" / name
        entity = json.loads(path.read_text(encoding="utf-8"))["entity"]
        for key, expected_name in frozen.items():
            platform = "sensor" if key != "probe_fault" else "binary_sensor"
            actual = entity[platform][key]["name"]
            assert actual == expected_name, (
                f"{name}: entity.{platform}.{key} name is {actual!r}, "
                f"expected {expected_name!r} (frozen by INTERFACES_V0.3.md §3)"
            )
            assert slugify(actual) == key, (
                f"{name}: slugify({actual!r}) = {slugify(actual)!r} != {key!r}"
            )


def test_translations_have_the_same_key_shape_as_strings():
    """en and zh-Hans must mirror the source key tree, not its text."""

    def shape(node):
        if isinstance(node, dict):
            return {key: shape(value) for key, value in node.items()}
        return type(node).__name__

    base = shape(load_strings())
    for name in ("en.json", "zh-Hans.json"):
        data = json.loads((COMPONENT / "translations" / name).read_text(encoding="utf-8"))
        assert shape(data) == base, f"{name} does not mirror strings.json"


def test_chinese_translation_is_actually_chinese():
    """A copy of the English file would pass a shape check but help nobody."""
    zh = json.loads((COMPONENT / "translations" / "zh-Hans.json").read_text(encoding="utf-8"))
    en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))

    assert zh["config"]["step"]["user"]["title"] != en["config"]["step"]["user"]["title"]
    assert zh["entity"] != en["entity"]

    # Spot-check that user-visible Chinese text is present.
    assert any("\u4e00" <= char <= "\u9fff" for char in zh["config"]["step"]["user"]["title"])


# ---------------------------------------------------------------------------
# Architecture boundaries
# ---------------------------------------------------------------------------

PURE_ENGINES = [
    "const.py",
    "models.py",
    "temperature_arbiter.py",
    "zone_coordinator.py",
    "aux_load.py",
    "weather_compensator.py",
    "heat_source.py",
    "diagnostics_engine.py",
]


@pytest.mark.parametrize("module", PURE_ENGINES)
def test_pure_engines_do_not_import_homeassistant(module):
    """The engines must stay testable without booting Home Assistant."""
    tree = ast.parse((COMPONENT / module).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "homeassistant"]
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "homeassistant":
                offenders.append(node.module)
    assert not offenders, f"{module} imports Home Assistant: {offenders}"


@pytest.mark.parametrize("module", PURE_ENGINES)
def test_pure_engines_never_read_the_wall_clock(module):
    """Time must be injected so behaviour is reproducible under test."""
    source = (COMPONENT / module).read_text(encoding="utf-8")
    forbidden = ["datetime.now(", "datetime.utcnow(", "datetime.today(", "time.time("]
    found = [token for token in forbidden if token in source]
    assert not found, f"{module} reads the wall clock: {found}"


@pytest.mark.parametrize("module", PURE_ENGINES)
def test_pure_engines_import_without_homeassistant(module):
    """Prove the boundary: import the module with HA forcibly unavailable.

    This is the empirical version of the AST check above - it does not grep for
    imports, it makes ``import homeassistant`` raise and then imports the engine.
    """
    import importlib
    import types

    class _Blocker(types.ModuleType):
        def __getattr__(self, name):  # pragma: no cover - guard
            raise ImportError("homeassistant is intentionally unavailable")

    saved = {
        key: value
        for key, value in sys.modules.items()
        if key == "homeassistant" or key.startswith("homeassistant.")
    }
    for key in saved:
        del sys.modules[key]
    sys.modules["homeassistant"] = _Blocker("homeassistant")
    try:
        importlib.import_module(f"custom_components.climatepilot.{module[:-3]}")
    finally:
        for key in list(sys.modules):
            if key == "homeassistant" or key.startswith("homeassistant."):
                del sys.modules[key]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# Configuration hygiene
# ---------------------------------------------------------------------------


def test_no_module_uses_device_id_as_a_target():
    """Design doc 6.3 / HA best practice: always address entities by entity_id."""
    offenders = []
    for path in COMPONENT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r"target\s*=\s*\{[^}]*device_id", source, re.DOTALL):
            offenders.append(path.name)
    assert not offenders, f"device_id used as a service target: {offenders}"


def test_only_the_adapter_calls_climate_services():
    """Exactly one module may talk to the climate integration."""
    callers = set()
    for path in COMPONENT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r'async_call\(\s*["\']climate["\']', source) or re.search(
            r'["\']climate\.[a-z_]+["\']', source
        ):
            callers.add(path.name)
    assert callers <= {"climate_adapter.py"}, (
        f"these modules call climate services directly: {sorted(callers)}"
    )


def test_no_enum_constant_collides_with_a_reserved_ha_state():
    """Enum values must not collide with Home Assistant's reserved states.

    Home Assistant treats ``"unavailable"`` and ``"unknown"`` specially: an
    entity reporting ``"unavailable"`` is rendered as genuinely unavailable -
    greyed out, indistinguishable from a broken integration - and a test that
    reads entity states sees the same string it would for a dead entity.
    (``"unknown"`` is milder: it means "available, value not known".)

    This is not hypothetical. ``PROBE_UNAVAILABLE`` was originally
    ``"unavailable"``, so ``sensor.<system>_delta_t_quality`` reported the
    reserved state whenever a probe was missing, and "no probe is reporting"
    became indistinguishable from "ClimatePilot is dead" - the exact
    distinction that sensor exists to draw.

    The check is deliberately narrow: it rejects only ``"unavailable"`` in
    constants published as *entity state values*, and explicitly allows
    ``"unknown"``.

    **Why ``"unknown"`` is allowed - do not "fix" this.** The two reserved
    strings are not equivalent:

    * ``unavailable`` means *the entity does not exist / is broken*.
    * ``unknown`` means *the entity is fine, its value is not determined*.

    The verdict constants want the second meaning: ``CONDENSING_UNKNOWN`` says
    "the boiler's condensing regime is not determinable", which is exactly what
    Home Assistant renders for ``"unknown"``. Rewriting them to something like
    ``"condensing_unknown"`` to dodge the reserved word would be cargo-culting
    this guard, and would make those sensors look broken to any consumer that
    treats ``unknown`` as "no data yet". The concrete harm that motivated this
    test - losing the difference between "no probe is reporting" and
    "ClimatePilot is dead" - only applies to ``unavailable``.
    """
    from homeassistant.const import STATE_UNAVAILABLE

    import custom_components.climatepilot.const as const

    #: Values deliberately published as entity states.
    published_state_constants = [
        name
        for name in vars(const)
        if name.isupper()
        and (
            name.startswith("PROBE_")
            or name.startswith("QUALITY_")
            or name.startswith("CONDENSING_")
            or name.startswith("HYDRAULIC_")
            or name.startswith("CALIBRATION_")
            or name.startswith("COMFORT_")
            or name.startswith("ADVICE_")
            or name.startswith("SOURCE_")
            or name.startswith("FSM_")
            or name.startswith("CONTROL_MODE_")
            or name.startswith("ZONE_MODE_")
        )
    ]
    assert published_state_constants, "no state constants found - check is vacuous"

    offenders = [
        (name, getattr(const, name))
        for name in published_state_constants
        if getattr(const, name) == STATE_UNAVAILABLE
    ]
    assert not offenders, (
        "these constants publish HA's reserved 'unavailable' state and would make "
        f"the entity look broken rather than probe-less: {offenders}"
    )


def test_unknown_verdicts_are_allowed_to_use_the_reserved_unknown_state():
    """Pin the deliberate allowance, so the guard above cannot be over-tightened.

    A future reader who sees the neighbour test reject a reserved HA state may
    "finish the job" by also banning ``"unknown"`` - and would thereby break
    verdicts whose correct meaning *is* Home Assistant's ``unknown``: "the
    entity is fine, the value is not determined".

    The ban is specific to ``unavailable`` (entity missing/broken). This test
    states that positively, so over-tightening fails loudly with an explanation
    instead of quietly changing the meaning of half a dozen sensors.
    """
    import custom_components.climatepilot.const as const

    #: Verdicts that intentionally publish the reserved "unknown" state.
    intentional = {
        "QUALITY_UNKNOWN": const.QUALITY_UNKNOWN,
        "CONDENSING_UNKNOWN": const.CONDENSING_UNKNOWN,
        "HYDRAULIC_UNKNOWN": const.HYDRAULIC_UNKNOWN,
        "COMFORT_UNKNOWN": const.COMFORT_UNKNOWN,
    }
    for name, value in intentional.items():
        assert value == "unknown", (
            f"{name} must stay 'unknown': it means 'available but not determined', "
            f"which is precisely how Home Assistant renders that state. Got {value!r}."
        )

    # ...and none of them may drift onto the genuinely-broken reserved state.
    assert const.PROBE_UNAVAILABLE != "unavailable"
    assert const.PROBE_UNAVAILABLE != "unknown"


def test_probe_qualities_are_distinct_and_non_empty():
    """The quality ladder must be a usable total order over real values."""
    import custom_components.climatepilot.const as const

    qualities = list(const.PROBE_QUALITIES)
    assert len(qualities) == len(set(qualities))
    assert const.PROBE_VALID in qualities
    assert const.PROBE_UNAVAILABLE in qualities


def test_manifest_declares_the_expected_identity():
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "climatepilot"
    assert manifest["name"] == "ClimatePilot"
    assert manifest["config_flow"] is True
    assert manifest["version"]


def test_reason_codes_referenced_in_docs_exist_in_const():
    """The documentation promises users reason codes; they must be real."""
    import custom_components.climatepilot.const as const

    known = {value for name, value in vars(const).items() if name.startswith("REASON_")}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    # Codes quoted in the README's explainability section.
    quoted = set(re.findall(r"`([a-z_]+)`", readme))
    claimed = {
        code
        for code in quoted
        if code
        in {
            "manual_override",
            "schedule_transition",
            "cold_adjustment",
            "range_clamp",
            "boiler_min_flow_unverified",
            "heat_source_semantics_unverified",
        }
    }
    assert claimed <= known, sorted(claimed - known)


def test_strings_services_match_the_registered_services():
    """A service documented but not registered (or vice versa) confuses users."""
    import custom_components.climatepilot.const as const

    strings = load_strings()
    documented = set(strings.get("services", {}))
    registered = {
        const.SERVICE_RESUME_AUTO,
        const.SERVICE_SET_SUPPLY_TEMPERATURE,
        const.SERVICE_REASSESS,
    }
    assert documented == registered, (documented, registered)


def test_services_yaml_exists_and_matches_the_registered_services():
    """``services.yaml`` must exist and describe exactly the registered services.

    Home Assistant logs ``Failed to load services.yaml for integration: ...`` on
    every startup when the file is missing, even though the services themselves
    still register and work. That was a real defect: the integration shipped
    three services and no ``services.yaml``, so a healthy install produced an
    ERROR line in the log that looked like a broken integration.

    The file is UI metadata (Developer Tools and automation editors read it),
    while validation lives in the voluptuous schema in ``__init__.py`` - so the
    two can drift apart silently. This pins the names; the field lists are
    checked for matching keys so a renamed service cannot leave a stale
    description behind.
    """
    import yaml

    import custom_components.climatepilot.const as const

    path = COMPONENT / "services.yaml"
    assert path.is_file(), "services.yaml is missing; Home Assistant logs an error on every startup"

    described = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(described, dict), "services.yaml must be a mapping"

    registered = {
        const.SERVICE_RESUME_AUTO,
        const.SERVICE_SET_SUPPLY_TEMPERATURE,
        const.SERVICE_REASSESS,
    }
    assert set(described) == registered, (
        f"services.yaml describes {sorted(set(described))}, code registers {sorted(registered)}"
    )

    # Every entry needs the fields HA's UI relies on.
    for name, spec in described.items():
        assert isinstance(spec, dict), name
        assert spec.get("name"), f"{name} has no display name"
        assert "fields" in spec, f"{name} has no fields key"


def test_services_yaml_fields_match_the_registered_schemas():
    """The documented fields must match the schema's fields.

    A field added to the schema but not documented (or documented after being
    removed) makes the automation editor suggest inputs the service rejects.

    Schemas reference module-level constants (``vol.Optional(CONF_ZONE_ID)``)
    rather than literals, so the constants are resolved through their
    declarations before comparing.
    """
    import yaml

    import custom_components.climatepilot.const as const

    source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    described = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    #: Constant name -> its string value. Schemas reference constants from both
    #: our own ``const`` module and Home Assistant's (``ATTR_TEMPERATURE``), so
    #: both namespaces are folded into one lookup.
    constants = {
        name: value
        for name, value in vars(const).items()
        if name.isupper() and isinstance(value, str)
    }
    from homeassistant.const import __dict__ as hass_const

    constants.update(
        {
            name: value
            for name, value in hass_const.items()
            if name.isupper() and isinstance(value, str)
        }
    )

    def resolve(token: str) -> str:
        """Map a schema token to the actual field name it carries."""
        return constants.get(token, token)

    for service in described:
        # Anchor on the *handler function name*, which is unique per service and
        # immediately precedes its schema. Matching on the SERVICE_* constant
        # alone is ambiguous: every registration mentions every other one
        # nearby, so a greedy/non-greedy scan can pair a service with its
        # neighbour's schema.
        handler = f"_async_handle_{service}"
        block = re.search(
            rf"{handler},.*?vol\.Schema\(\s*\{{(.*?)\}}\s*,",
            source,
            re.DOTALL,
        )
        if block is None:
            # ``reassess`` registers an empty schema.
            block = re.search(
                rf"{handler},.*?vol\.Schema\(\{{\}}(.*?)\)",
                source,
                re.DOTALL,
            )
        assert block is not None, f"could not locate the schema for {service}"

        schema_fields = {
            resolve(token)
            for token in re.findall(
                r"vol\.(?:Required|Optional)\((?:\w+\.)?(\w+)\)", block.group(1)
            )
        }
        documented_fields = set(described[service].get("fields") or {})
        assert documented_fields == schema_fields, (
            f"{service}: services.yaml documents {sorted(documented_fields)}, "
            f"schema uses {sorted(schema_fields)}"
        )
