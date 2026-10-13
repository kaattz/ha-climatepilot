"""End-to-end entity-id migration against a real Home Assistant.

The unit tests in test_entity_id_migration.py use fake registries: they pin the
logic precisely and run in milliseconds, but they cannot prove Home Assistant
accepts the renames, and a fake that yields entry objects hides an entire class of
mistake.

This test exists because of exactly that. `registry.entities` maps entity_id to
entry, so **iterating it yields entity_id strings**, and reading `.entity_id` off
a string finds nothing. The migration therefore ran, matched no rows, and renamed
nothing - while every unit test passed. It took a booted Home Assistant to see it.

What this covers that the unit tests cannot:
  * Home Assistant accepts `async_update_entity(new_entity_id=...)` for all 52 ids
  * unique_ids survive, so history and references are intact
  * a second reload changes nothing
  * the entity count is unchanged
"""

from __future__ import annotations

from pathlib import Path

#: Reuse the fixture defined in test_integration_setup.py. It lives there rather
#: than in a conftest.py, and pytest resolves fixtures per module, so it is
#: requested by its fully-qualified name instead of a bare plug.
pytest_plugins = ["test_integration_setup"]

from custom_components.climatepilot.const import DOMAIN

ROOT = Path(__file__).resolve().parents[1]

#: Pinyin prefixes the pre-V0.4 releases produced.
LEGACY_PREFIXES = ("wo_de_jia_", "ke_ting_di_nuan_", "zhu_wo_di_nuan_")


def _rows(hass) -> dict[str, str]:
    """This integration's registry rows as {entity_id: unique_id}.

    Note the API shape: ``registry.entities`` maps entity_id -> entry, so
    iterating it yields *strings*. The migration made that mistake once; this
    helper must not repeat it.
    """
    from homeassistant.helpers import entity_registry

    registry = entity_registry.async_get(hass)
    return {
        e.entity_id: e.unique_id
        for e in registry.entities.values()
        if getattr(e, "platform", None) == DOMAIN
    }


def _legacy_id(entity_id: str) -> str:
    """Rewrite a cp_ id back to the shape pre-V0.4 releases produced.

    System-level keys got the pinyin device prefix; zone keys got the pinyin room
    prefix. That is what the old rule generated from the Chinese names.
    """
    domain, _, object_id = entity_id.partition(".")
    key = object_id[len("cp_") :] if object_id.startswith("cp_") else object_id
    if key.startswith("living_room_"):
        return f"{domain}.ke_ting_di_nuan_{key[len('living_room_') :]}"
    if key.startswith("master_bedroom_"):
        return f"{domain}.zhu_wo_di_nuan_{key[len('master_bedroom_') :]}"
    return f"{domain}.wo_de_jia_{key}"


async def test_legacy_entity_ids_are_migrated_on_a_real_registry(hass):
    """Install, rewrite the ids to the legacy shape, reload, and check."""
    import test_integration_setup as setup_helpers
    from homeassistant.helpers import entity_registry

    entry = await setup_helpers.setup_entry(hass)
    registry = entity_registry.async_get(hass)

    # --- put the registry back into its pre-V0.4 shape --------------------
    installed = _rows(hass)
    assert installed, "no entities were created; the test cannot prove anything"

    for entity_id in list(installed):
        registry.async_update_entity(entity_id, new_entity_id=_legacy_id(entity_id))

    legacy = _rows(hass)
    assert len(legacy) == len(installed)
    assert all(e.split(".", 1)[1].startswith(LEGACY_PREFIXES) for e in legacy), (
        "setup is wrong: not every id was rewritten to the legacy shape"
    )
    unique_ids_before = sorted(legacy.values())

    # --- reload: the migration must run -----------------------------------
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    migrated = _rows(hass)

    still_legacy = [e for e in migrated if e.split(".", 1)[1].startswith(LEGACY_PREFIXES)]
    assert not still_legacy, f"these were not renamed: {still_legacy}"
    assert all(e.split(".", 1)[1].startswith("cp_") for e in migrated)

    assert len(migrated) == len(legacy), (
        f"entity count changed during migration: {len(legacy)} -> {len(migrated)}"
    )
    assert sorted(migrated.values()) == unique_ids_before, (
        "unique_ids changed; history and references would be lost"
    )

    # --- reload again: idempotent -----------------------------------------
    snapshot = dict(migrated)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _rows(hass) == snapshot, "a second reload changed ids"

    # --- the ids users reference must exist --------------------------------
    for entity_id, expected_suffix in (
        ("sensor.cp_heating_state", "heating_state"),
        ("sensor.cp_living_room_effective_setpoint", "living_room_effective_setpoint"),
        ("switch.cp_heating_season", "heating_season"),
    ):
        assert entity_id in snapshot, f"missing after migration: {entity_id}"
        assert snapshot[entity_id].endswith(expected_suffix)
