"""The six gas and degree-day sensors: stable ids, stable units, honest scope.

Three properties are asserted, each because getting it wrong is silent rather than
loud:

* **Entity ids** are ``sensor.cp_*`` like every other entity in the integration.
* **Units never vary.** Home Assistant suppresses long-term statistics when an
  entity's unit changes, so a unit that depends on the reading would make history
  stop growing with no error anywhere.
* **The whole-house scope is declared as an attribute.** Without it, ``m3 per
  degree-day`` reads as a boiler efficiency figure, when the number also contains
  the user's cooking and hot water.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_coordinator_smoke import (
    full_options,
    install_entities,
    make_entry,
    pin_clock,
)

from custom_components.climatepilot import sensor as sensor_module
from custom_components.climatepilot.const import (
    CONF_GAS_METER_ENTITY,
    ENERGY_UNIT_DEGREE_DAYS,
    ENERGY_UNIT_GAS,
    ENERGY_UNIT_GAS_PER_DEGREE_DAY,
)
from custom_components.climatepilot.coordinator import ClimatePilotCoordinator

ROOT = Path(__file__).resolve().parents[1]

GAS = "sensor.test_gas_meter"

#: The six sensors this feature adds, as (class name, expected key).
EXPECTED = {
    "GasTodaySensor": "gas_today",
    "GasYesterdaySensor": "gas_yesterday",
    "GasThisMonthSensor": "gas_this_month",
    "DegreeDaysTodaySensor": "degree_days_today",
    "DegreeDaysThisMonthSensor": "degree_days_this_month",
    "GasPerDegreeDaySensor": "gas_per_degree_day",
}


@pytest.fixture()
async def hass(tmp_path):
    """A real HomeAssistant with the production entity set installed.

    Follows the shape the other coordinator test modules use, so this file behaves
    like its siblings rather than inventing its own harness.
    """
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import frame

    instance = HomeAssistant(str(tmp_path))
    frame.async_setup(instance)

    recorded: list[tuple[str, str, dict]] = []

    async def _spy(call):
        recorded.append((call.domain, call.service, dict(call.data)))

    instance.services.async_register("climate", "set_temperature", _spy)
    instance.services.async_register("climate", "set_hvac_mode", _spy)
    instance._climatepilot_calls = recorded
    try:
        yield instance
    finally:
        await instance.async_stop()


@pytest.fixture()
def hass_factory(hass):
    """Build a coordinator, optionally with a working gas meter."""

    def build(*, gas: bool) -> ClimatePilotCoordinator:
        install_entities(hass)
        overrides = {CONF_GAS_METER_ENTITY: GAS} if gas else {}
        if gas:
            hass.states.async_set(GAS, "1000.0")
        entry = make_entry(full_options(**overrides))
        coordinator = ClimatePilotCoordinator(hass, entry)
        coordinator.async_shutdown_listeners = MagicMock()
        pin_clock(coordinator)
        return coordinator

    return build


@pytest.fixture()
def coordinator(hass_factory):
    """A coordinator with a working gas meter."""
    return hass_factory(gas=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_the_six_sensor_classes_exist() -> None:
    for name in EXPECTED:
        assert hasattr(sensor_module, name), f"{name} is missing from sensor.py"


def test_sensor_ids_use_the_pinned_prefix() -> None:
    """All six must be ``sensor.cp_<key>`` - the naming contract from V0.4."""
    from custom_components.climatepilot.base import entity_id_for

    for name, key in EXPECTED.items():
        assert entity_id_for("sensor", key) == f"sensor.cp_{key}", name


def test_no_sensor_contains_a_pinyin_fragment() -> None:
    """Regression guard for the exact problem the V0.4 rename solved."""
    from custom_components.climatepilot.base import entity_id_for

    for key in EXPECTED.values():
        entity_id = entity_id_for("sensor", key)
        for fragment in ("wo_de_jia", "ke_ting", "zhu_wo", "ci_wo", "di_nuan"):
            assert fragment not in entity_id


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_units_are_declared_and_constant(coordinator) -> None:
    """A unit that varies silently stops history; a missing one is not comparable."""
    expected_units = {
        "GasTodaySensor": ENERGY_UNIT_GAS,
        "GasYesterdaySensor": ENERGY_UNIT_GAS,
        "GasThisMonthSensor": ENERGY_UNIT_GAS,
        "DegreeDaysTodaySensor": ENERGY_UNIT_DEGREE_DAYS,
        "DegreeDaysThisMonthSensor": ENERGY_UNIT_DEGREE_DAYS,
        "GasPerDegreeDaySensor": ENERGY_UNIT_GAS_PER_DEGREE_DAY,
    }
    for name, expected in expected_units.items():
        entity = getattr(sensor_module, name)(coordinator)
        assert entity.native_unit_of_measurement == expected, name
        # Evaluated twice on purpose: a unit computed from the reading could differ
        # between calls, which is precisely what breaks statistics.
        assert entity.native_unit_of_measurement == expected, name


def test_the_degree_day_unit_is_not_a_recognised_temperature() -> None:
    """``C.d`` must not be confused with degrees Celsius.

    A degree-day is a temperature difference integrated over time; presenting it in
    plain ``C`` would invite it to be read as "how cold it is" and, worse, summed
    with actual temperatures.
    """
    assert ENERGY_UNIT_DEGREE_DAYS != "\u00b0C"
    assert ENERGY_UNIT_DEGREE_DAYS == "\u00b0C\u00b7d"


def test_no_sensor_declares_a_temperature_device_class(coordinator) -> None:
    """None of these is a temperature, so none may claim the temperature class.

    ``device_class: temperature`` triggers Home Assistant's unit conversion, which
    would try to interpret ``m3/C.d`` as a temperature and either mangle it or drop
    the entity from statistics.
    """
    for name in EXPECTED:
        entity = getattr(sensor_module, name)(coordinator)
        assert entity.device_class is None, name


# ---------------------------------------------------------------------------
# State class
# ---------------------------------------------------------------------------


def test_every_sensor_carries_a_state_class(coordinator) -> None:
    """Without one the readings are purged after the recorder retention window.

    This installation keeps 30 days, so an entity with no ``state_class`` loses its
    history before any month-over-month comparison is possible - which is the whole
    point of the feature.
    """
    for name in EXPECTED:
        entity = getattr(sensor_module, name)(coordinator)
        assert entity.state_class is not None, name


# ---------------------------------------------------------------------------
# Scope disclosure
# ---------------------------------------------------------------------------


def test_gas_sensors_declare_the_whole_house_scope(coordinator) -> None:
    """The single thing a user must not misunderstand.

    The meter also feeds domestic hot water and the hob, so these figures are not
    heating consumption. Saying so in the attributes is what stops the number being
    read as a boiler measurement.
    """
    for name in ("GasTodaySensor", "GasYesterdaySensor", "GasThisMonthSensor"):
        entity = getattr(sensor_module, name)(coordinator)
        attrs = entity.extra_state_attributes
        assert attrs.get("scope") == "whole_house", name
        assert attrs.get("includes_dhw_and_cooking") is True, name


def test_the_ratio_sensor_says_it_is_not_an_efficiency(coordinator) -> None:
    """Mirrors the V0.3 convention that stopped delta-T reading as heat power.

    ``is_power_measurement = False`` exists on the probe entities for exactly this
    reason; the ratio needs its own equivalent so a dashboard or template cannot
    quietly treat it as a boiler performance figure.
    """
    entity = sensor_module.GasPerDegreeDaySensor(coordinator)
    attrs = entity.extra_state_attributes
    assert attrs.get("is_efficiency_measurement") is False
    assert attrs.get("scope") == "whole_house"


def test_degree_day_sensors_expose_the_base_temperature(coordinator) -> None:
    """The base is a convention, so the reading is meaningless without it."""
    for name in ("DegreeDaysTodaySensor", "DegreeDaysThisMonthSensor"):
        entity = getattr(sensor_module, name)(coordinator)
        assert entity.extra_state_attributes.get("base_c") == 15.5, name


def test_degree_day_sensors_expose_coverage(coordinator) -> None:
    """A partly observed day must be qualifying, not silently trusted."""
    entity = sensor_module.DegreeDaysTodaySensor(coordinator)
    attrs = entity.extra_state_attributes
    assert "coverage" in attrs
    assert "trusted" in attrs


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def test_values_come_from_the_coordinator(coordinator) -> None:
    from datetime import UTC, datetime, timedelta

    t0 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    coordinator.note_energy(t0)
    coordinator.hass.states.async_set(GAS, "1006.0")
    coordinator.note_energy(t0 + timedelta(hours=1))

    assert sensor_module.GasTodaySensor(coordinator).native_value == pytest.approx(6.0, abs=0.01)


def test_a_missing_reading_is_unknown_not_zero(coordinator) -> None:
    """``None`` must reach Home Assistant as unknown, never as 0 m3.

    Zero claims the boiler burned nothing, which is a statement about the heating
    system; unknown says only that nothing was measured.
    """
    entity = sensor_module.GasTodaySensor(coordinator)
    assert entity.native_value is None


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


def test_every_sensor_has_a_name_in_all_three_translation_files() -> None:
    files = [
        ROOT / "custom_components" / "climatepilot" / "strings.json",
        ROOT / "custom_components" / "climatepilot" / "translations" / "en.json",
        ROOT / "custom_components" / "climatepilot" / "translations" / "zh-Hans.json",
    ]
    for path in files:
        tree = json.loads(path.read_text(encoding="utf-8"))
        entities = tree["entity"]["sensor"]
        for key in EXPECTED.values():
            assert key in entities, f"{path.name}: missing sensor.{key}"
            assert entities[key].get("name", "").strip(), f"{path.name}: {key} has no name"
