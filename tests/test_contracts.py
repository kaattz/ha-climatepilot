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
