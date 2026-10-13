"""The entity id scheme: a pinned ASCII ``cp_`` prefix.

Home Assistant's documented behaviour is to build ``entity_id`` from
``<device name>_<entity name>``. That is a good default - the device name acts
as a source tag that keeps entities from different integrations apart - but it
produces unusable ids here, because the device name is Chinese and ``slugify``
can only romanise it: "我的家" becomes "wo_de_jia".

The alternative of making the system name ASCII was rejected: it would tie every
entity id to a user-editable display string, so renaming the system would rename
all 51 entities and break anything referencing them, and ``name_by_user`` takes
priority over ``name``, so renaming the device in the UI would reintroduce
pinyin for entities created afterwards.

These tests pin the resulting scheme, and the reason each property matters is
recorded next to it - an id scheme is only "obviously fine" until someone
renames a device.
"""

from __future__ import annotations

import inspect

import pytest

from custom_components.climatepilot.base import entity_id_for

#: Every platform in the integration, so the scheme is checked across all of
#: them rather than on whichever domain happened to be written first.
SAMPLE_IDS = [
    ("sensor", "heating_state", "sensor.cp_heating_state"),
    (
        "sensor",
        "livingroom_floorheating_effective_setpoint",
        "sensor.cp_livingroom_floorheating_effective_setpoint",
    ),
    ("climate", "whole_home_heating", "climate.cp_whole_home_heating"),
    ("switch", "heating_season", "switch.cp_heating_season"),
    ("binary_sensor", "probe_fault", "binary_sensor.cp_probe_fault"),
    ("select", "control_mode", "select.cp_control_mode"),
    ("number", "manual_supply_temperature", "number.cp_manual_supply_temperature"),
    ("button", "reassess_now", "button.cp_reassess_now"),
]


@pytest.mark.parametrize(("domain", "key", "expected"), SAMPLE_IDS)
def test_entity_id_uses_the_cp_prefix(domain: str, key: str, expected: str) -> None:
    assert entity_id_for(domain, key) == expected


def test_domain_is_preserved_verbatim() -> None:
    """The domain must not be slugified or altered - HA matches on it exactly.

    ``binary_sensor`` is the case that catches a careless implementation:
    running the whole id through ``slugify`` is harmless for ``sensor`` but
    would turn the underscore-containing domain into something else if the
    slugify call were ever moved to cover it.
    """
    assert entity_id_for("binary_sensor", "probe_fault").startswith("binary_sensor.")


def test_entity_id_is_ascii_even_for_a_chinese_key() -> None:
    """Keys derived from a room name may be Chinese; the id must still be ASCII.

    Room names are user-editable free text, so a key can contain any character.
    An entity id containing non-ASCII characters is not addressable from
    templates and automations the way users write them.
    """
    result = entity_id_for("sensor", "客厅_effective_setpoint")
    assert result.isascii(), result
    assert result.startswith("sensor.cp_")


def test_entity_id_does_not_depend_on_the_system_name() -> None:
    """The whole point: renaming the system must not change any entity id.

    This is the property the device-name-derived scheme lacks, and the reason
    the prefix is pinned in code instead. If this test ever needs the
    coordinator's ``system_name`` to pass, the design has regressed.
    """
    signature = inspect.signature(entity_id_for)
    assert list(signature.parameters) == ["domain", "key"], (
        "entity_id_for must depend only on the domain and key; taking the "
        "system name would make ids change when the user renames the system"
    )


def test_prefix_is_a_module_constant() -> None:
    """The prefix is referenced by the migration, so it must be importable."""
    from custom_components.climatepilot.base import ENTITY_ID_PREFIX

    assert ENTITY_ID_PREFIX == "cp"
    assert entity_id_for("sensor", "heating_state") == f"sensor.{ENTITY_ID_PREFIX}_heating_state"
