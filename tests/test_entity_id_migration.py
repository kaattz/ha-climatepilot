"""Migration of entity ids created before the pinned ``cp_`` prefix.

Why a migration exists at all: Home Assistant keys the entity registry by
``unique_id`` and pins the ``entity_id`` stored beside it, so changing the naming
code does not rename anything that already exists. Deleting the config entry and
reinstalling does not help either - the device id survives the delete, so the new
entity id is derived from the *old* device name and the pinyin prefix comes back.

The migration derives the new id from the **unique_id**, not from the old name:

    unique_id  <entry_id>_living_room_effective_setpoint
    new id     sensor.cp_living_room_effective_setpoint

That matters because the old names came from translated English strings that no
longer match the new keys (``combustion_quality`` -> ``cycling_quality``), so a
name-based mapping table would have to be maintained and would silently miss any
entity whose translation changed. The unique_id has been stable since V0.1.
"""

from __future__ import annotations

import pytest


class FakeEntry:
    """A registry row: only the fields the migration reads."""

    def __init__(self, entity_id: str, unique_id: str, platform: str = "climatepilot") -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.platform = platform


class FakeRegistry:
    """Records renames instead of performing them."""

    def __init__(self, entries: list[FakeEntry]) -> None:
        self._entries = entries
        self.updated: list[tuple[str, str]] = []

    @property
    def entities(self):
        return self._entries

    def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
        self.updated.append((entity_id, new_entity_id))


ENTRY_ID = "01M4TV4Q04PQ5A0Q5J88SDA8HP"


def test_system_entity_is_renamed_from_its_unique_id() -> None:
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state")]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    assert registry.updated == [("sensor.wo_de_jia_heating_state", "sensor.cp_heating_state")]


def test_zone_entity_keeps_its_zone_in_the_new_id() -> None:
    """The zone id is part of the unique_id, so rooms stay distinguishable."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [
            FakeEntry(
                "sensor.ke_ting_di_nuan_effective_setpoint",
                f"{ENTRY_ID}_living_room_effective_setpoint",
            ),
            FakeEntry(
                "sensor.zhu_wo_di_nuan_effective_setpoint",
                f"{ENTRY_ID}_master_bedroom_effective_setpoint",
            ),
        ]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 2
    assert registry.updated == [
        (
            "sensor.ke_ting_di_nuan_effective_setpoint",
            "sensor.cp_living_room_effective_setpoint",
        ),
        (
            "sensor.zhu_wo_di_nuan_effective_setpoint",
            "sensor.cp_master_bedroom_effective_setpoint",
        ),
    ]


def test_the_new_id_does_not_depend_on_the_old_name() -> None:
    """A translated name that changed between releases must not matter.

    ``combustion_quality`` became ``cycling_quality``; the registry row still
    carries the old name and the old text, so only the unique_id can be trusted.
    """
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [FakeEntry("sensor.wo_de_jia_combustion_quality", f"{ENTRY_ID}_cycling_quality")]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    assert registry.updated == [
        ("sensor.wo_de_jia_combustion_quality", "sensor.cp_cycling_quality")
    ]


def test_already_migrated_entities_are_left_alone() -> None:
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry([FakeEntry("sensor.cp_heating_state", f"{ENTRY_ID}_heating_state")])

    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == []


def test_migration_is_idempotent() -> None:
    """Running it on every setup must be safe, so it has to be a no-op the second time."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state")]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    first_pass = list(registry.updated)

    # Simulate the rename having taken effect.
    registry._entries = [FakeEntry("sensor.cp_heating_state", f"{ENTRY_ID}_heating_state")]
    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == first_pass


def test_an_id_already_using_the_prefix_is_never_touched_again() -> None:
    """An id that is already ``cp_`` is left alone even if it "disagrees".

    This is the case that distinguishes the two guards in the migration. If the
    id is ``cp_x`` while the unique_id implies ``cp_y``, the "already migrated"
    check keeps ``cp_x`` and the ``new == old`` check would rename it instead.

    Keeping it is the right behaviour: a ``cp_`` id is already in the target
    scheme, and it may well have been renamed by hand in the UI - churning it
    back would undo a deliberate user action on every restart. Without this test
    the two guards look interchangeable, because in the ordinary case (id and
    unique_id agreeing) they are.
    """
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [FakeEntry("sensor.cp_hand_renamed_by_user", f"{ENTRY_ID}_heating_state")]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == [], (
        "an already-migrated id must not be renamed to match its unique_id"
    )


def test_entities_from_another_config_entry_are_ignored() -> None:
    """A second installation's rows must not be renamed by this one."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    other = "01OTHERENTRY000000000000"
    registry = FakeRegistry([FakeEntry("sensor.wo_de_jia_heating_state", f"{other}_heating_state")])

    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == []


def test_rows_without_a_matching_prefix_are_ignored() -> None:
    """Unexpected rows must be skipped, not guessed at.

    Two shapes: a unique_id that does not carry this entry's id (another
    integration, or a scheme we do not know), and a malformed entity id with no
    domain separator.
    """
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [
            FakeEntry("sensor.something_else", "an-unrelated-unique-id"),
            # No domain separator: cannot be rebuilt, so must be left alone.
            FakeEntry("malformed_entity_id", f"{ENTRY_ID}_heating_state"),
        ]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == []


def test_an_entity_with_no_key_in_its_unique_id_is_ignored() -> None:
    """``{entry_id}_`` with nothing after it yields no key, so there is no id to build."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry([FakeEntry("sensor.wo_de_jia_heating_state", ENTRY_ID)])

    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == []


def test_a_collision_does_not_abort_the_whole_migration() -> None:
    """One refused rename must not leave the remaining entities unmigrated.

    Home Assistant raises ``ValueError`` when the target id is already
    registered. If that propagated, setup would fail and the integration would be
    unloadable - a far worse outcome than one entity keeping its old id.
    """
    from custom_components.climatepilot.migration import migrate_entity_ids

    class RefusingRegistry(FakeRegistry):
        def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
            if entity_id.endswith("heating_state"):
                raise ValueError("Entity with this ID is already registered")
            self.updated.append((entity_id, new_entity_id))

    registry = RefusingRegistry(
        [
            FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state"),
            FakeEntry("sensor.wo_de_jia_delta_t", f"{ENTRY_ID}_delta_t"),
        ]
    )

    # The second row must still be renamed despite the first failing.
    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    assert registry.updated == [("sensor.wo_de_jia_delta_t", "sensor.cp_delta_t")]


def test_missing_entry_id_is_a_no_op() -> None:
    """Without an entry id nothing can be matched, so nothing may be touched."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry(
        [FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state")]
    )

    assert migrate_entity_ids(registry, "") == 0
    assert registry.updated == []


def test_a_registry_with_no_rows_loaded_is_a_no_op() -> None:
    """Setup must not crash when the registry has not been loaded.

    A real Home Assistant loads the registry during bootstrap, so this cannot
    happen in production - but the failure mode if it ever did is an integration
    that refuses to set up, which is far worse than a skipped rename. Measured:
    ``async_get`` returns a registry whose ``entities`` attribute only exists
    after ``async_load``, so a bare ``HomeAssistant`` in a test has none.
    """

    class UnloadedRegistry:
        """Mimics an EntityRegistry before async_load."""

        def __init__(self) -> None:
            self.updated: list[tuple[str, str]] = []

        def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
            self.updated.append((entity_id, new_entity_id))

    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = UnloadedRegistry()
    assert migrate_entity_ids(registry, ENTRY_ID) == 0
    assert registry.updated == []


@pytest.mark.parametrize(
    ("entity_id", "unique_id", "expected"),
    [
        (
            "switch.wo_de_jia_heating_season",
            f"{ENTRY_ID}_heating_season",
            "switch.cp_heating_season",
        ),
        (
            "binary_sensor.wo_de_jia_probe_fault",
            f"{ENTRY_ID}_probe_fault",
            "binary_sensor.cp_probe_fault",
        ),
        (
            "climate.wo_de_jia_whole_home_heating",
            f"{ENTRY_ID}_system_thermostat",
            "climate.cp_system_thermostat",
        ),
    ],
)
def test_every_platform_is_covered(entity_id: str, unique_id: str, expected: str) -> None:
    """The domain is taken from the existing id, so no platform is special-cased."""
    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = FakeRegistry([FakeEntry(entity_id, unique_id)])
    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    assert registry.updated == [(entity_id, expected)]


def test_a_container_that_yields_entity_id_strings_is_read_correctly() -> None:
    """The real registry container iterates to **strings**, not entries.

    ``EntityRegistryItems`` maps ``entity_id -> entry``, so iterating it yields
    entity id strings. Reading ``.entity_id`` off a string finds nothing, and a
    migration that does so runs, recognises no rows, and renames nothing - all
    without raising. A fake registry that yields entry objects cannot reproduce
    that, which is why this case has its own test: it was the real behaviour
    until an end-to-end run against a booted Home Assistant caught it.

    The supported lookup is tried first, so this pins the fallback for a
    container exposing only the mapping interface.
    """

    class MappingContainer(dict):
        """Behaves like EntityRegistryItems minus the lookup helper."""

    class Registry:
        def __init__(self, entries: list[FakeEntry]) -> None:
            self.entities = MappingContainer((e.entity_id, e) for e in entries)
            self.updated: list[tuple[str, str]] = []

        def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
            self.updated.append((entity_id, new_entity_id))

    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = Registry(
        [
            FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state"),
            FakeEntry("sensor.wo_de_jia_delta_t", f"{ENTRY_ID}_delta_t"),
        ]
    )

    assert migrate_entity_ids(registry, ENTRY_ID) == 2, (
        "iterating the mapping yields entity id strings; reading .entity_id off "
        "them finds nothing and the migration silently does no work"
    )
    assert registry.updated == [
        ("sensor.wo_de_jia_heating_state", "sensor.cp_heating_state"),
        ("sensor.wo_de_jia_delta_t", "sensor.cp_delta_t"),
    ]


def test_the_supported_config_entry_lookup_is_preferred() -> None:
    """On a real registry the config-entry lookup is used, not a full scan.

    ``get_entries_for_config_entry_id`` returns real ``RegistryEntry`` objects
    already filtered to this entry, so the migration neither misreads strings nor
    walks other integrations' rows.
    """

    class Container:
        """Exposes the supported lookup and nothing iterable."""

        def __init__(self, entries: list[FakeEntry]) -> None:
            self._entries = entries
            self.calls: list[str] = []

        def get_entries_for_config_entry_id(self, entry_id: str) -> list[FakeEntry]:
            self.calls.append(entry_id)
            return list(self._entries)

    class Registry:
        def __init__(self, entries: list[FakeEntry]) -> None:
            self.entities = Container(entries)
            self.updated: list[tuple[str, str]] = []

        def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
            self.updated.append((entity_id, new_entity_id))

    from custom_components.climatepilot.migration import migrate_entity_ids

    registry = Registry([FakeEntry("sensor.wo_de_jia_heating_state", f"{ENTRY_ID}_heating_state")])

    assert migrate_entity_ids(registry, ENTRY_ID) == 1
    assert registry.entities.calls == [ENTRY_ID]
    assert registry.updated == [("sensor.wo_de_jia_heating_state", "sensor.cp_heating_state")]
