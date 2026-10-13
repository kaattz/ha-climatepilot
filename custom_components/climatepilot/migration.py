"""One-off rename of entity ids created before the pinned ``cp_`` prefix.

Why this exists
---------------
Home Assistant keys the entity registry by ``unique_id`` and pins the
``entity_id`` stored beside it, so changing the naming code does not rename
entities that already exist. Measured on 2026.9.4: deleting the config entry and
reinstalling does not help either - the device id survives the delete, so the new
entity id is derived from the *old* device name and the pinyin prefix comes back.

So the installed base is renamed explicitly, through Home Assistant's own
registry API, which preserves the ``unique_id``, the history and every reference.

Why the new id comes from the unique_id
---------------------------------------
Not from the old name. The old ids were built from translated English strings
that no longer match the current keys (``combustion_quality`` became
``cycling_quality``), so a name-based mapping would need a maintained table and
would silently miss any entity whose translation changed. The unique_id has been
``{entry_id}_{key}`` since V0.1 and is the stable thing to derive from.

The domain is taken from the existing entity id rather than looked up, so no
platform is special-cased and a new platform needs no change here.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ENTITY_ID_PREFIX, entity_id_for

_LOGGER = logging.getLogger(__name__)


def migrate_entity_ids(registry: Any, entry_id: str) -> int:
    """Rename this entry's legacy entity ids to the pinned prefix.

    Returns the number of entities renamed. Safe to call on every setup: rows
    already using the current prefix, rows belonging to another config entry, and
    rows whose unique_id does not carry this entry's id are all skipped.

    The trigger is "this entry's entity whose id does not already start with the
    pinned prefix", rather than "whose id starts with the old pinyin prefix".
    That covers both places pinyin appeared - the system-level ids
    (``wo_de_jia_*``) and the per-room ones (``ke_ting_di_nuan_*``) - and it keeps
    working if a future release changes the prefix again.

    A rename that Home Assistant refuses (its target already exists raises
    ``ValueError``) is logged and skipped rather than propagated. One stuck
    entity is a far smaller problem than an integration that fails to set up.

    The whole body is defensive about the registry exposing no rows at all. On a
    real system it always does - Home Assistant loads the registry during
    bootstrap, before any config entry - but ``async_get`` returns a registry
    whose ``entities`` collection only exists once ``async_load`` has run, and
    setup must not be the thing that discovers that by raising.
    """
    if not entry_id:
        return 0

    rows = getattr(registry, "entities", None)
    if rows is None:
        _LOGGER.debug("climatepilot: entity registry has no rows loaded; skipping id migration")
        return 0

    renamed = 0
    prefix = f"{entry_id}_"

    for entry in list(rows):
        entity_id = getattr(entry, "entity_id", None)
        unique_id = getattr(entry, "unique_id", None)
        if not entity_id or not unique_id or "." not in entity_id:
            continue

        domain, _, object_id = entity_id.partition(".")
        if object_id.startswith(f"{ENTITY_ID_PREFIX}_"):
            continue  # already migrated

        if not unique_id.startswith(prefix):
            continue  # another entry's entity, or an id scheme we do not know

        key = unique_id[len(prefix) :]
        if not key:
            continue

        new_entity_id = entity_id_for(domain, key)
        if new_entity_id == entity_id:
            continue

        try:
            registry.async_update_entity(entity_id, new_entity_id=new_entity_id)
        except ValueError as err:
            _LOGGER.warning(
                "climatepilot: could not rename %s to %s (%s); leaving it as is",
                entity_id,
                new_entity_id,
                err,
            )
            continue

        _LOGGER.info("climatepilot: renamed %s -> %s", entity_id, new_entity_id)
        renamed += 1

    return renamed
