"""Unit tests for ClimatePilot persistent state.

The tests exercise the real ``homeassistant.helpers.storage.Store`` -- including
its genuine migrate hook, which is dispatched by argument count -- against a
minimal ``hass`` double whose ``config.config_dir`` points at ``tmp_path``.
That means the files really are written, read back and migrated, without booting
a Home Assistant instance or requiring the HA pytest fixture plugin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.helpers.storage import STORAGE_DIR, Store

from custom_components.climatepilot.const import (
    DOMAIN,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)
from custom_components.climatepilot.store import ClimatePilotStore, StoreDataInvalid


class FakeHass:
    """The subset of ``HomeAssistant`` that ``Store`` actually touches."""

    def __init__(self, config_dir: Path) -> None:
        config_dir_str = str(config_dir)
        self.config = SimpleNamespace(
            config_dir=config_dir_str,
            path=lambda *parts: os.path.join(config_dir_str, *parts),
        )
        self.data: dict[str, object] = {}
        self.state = Mock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """Resolve the running loop lazily.

        ``Store`` needs a loop only while it is awaiting, and the fixture that
        builds this double is synchronous.
        """
        return asyncio.get_running_loop()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def hass(tmp_path: Path) -> FakeHass:
    return FakeHass(tmp_path)


@pytest.fixture
def store(hass: FakeHass) -> ClimatePilotStore:
    return ClimatePilotStore(hass, "entry-abc")


def store_path(hass: FakeHass, key: str) -> Path:
    return Path(hass.config.config_dir) / STORAGE_DIR / key


# ---------------------------------------------------------------------------
# identity / wiring
# ---------------------------------------------------------------------------


def test_storage_key_and_version_follow_const(store: ClimatePilotStore) -> None:
    assert store.key == f"{STORAGE_KEY_PREFIX}.entry-abc"
    assert store.key == f"{DOMAIN}.state.entry-abc"
    assert store.version == STORAGE_VERSION


def test_two_systems_do_not_collide(hass: FakeHass) -> None:
    first = ClimatePilotStore(hass, "entry-1")
    second = ClimatePilotStore(hass, "entry-2")

    assert first.key != second.key
    assert first.key.endswith(".entry-1")
    assert second.key.endswith(".entry-2")


def test_migrate_func_is_implemented(store: ClimatePilotStore) -> None:
    assert hasattr(store, "_async_migrate_func")
    assert callable(store._async_migrate_func)


# ---------------------------------------------------------------------------
# load / save round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_subclasses_ha_store_for_real_migration(
    store: ClimatePilotStore,
) -> None:
    """The migrate hook only runs if we *are* a HomeAssistant Store.

    ``Store._async_load_data`` calls ``self._async_migrate_func``; a wrapper
    object would leave that dead code and silently skip migration.
    """
    assert isinstance(store, Store)
    assert type(store)._async_migrate_func is not Store._async_migrate_func
    assert store.system_id == "entry-abc"
    assert store.key == f"{STORAGE_KEY_PREFIX}.entry-abc"
    assert store.version == STORAGE_VERSION


@pytest.mark.asyncio
async def test_load_without_any_file_returns_empty_dict(
    store: ClimatePilotStore,
) -> None:
    """No persisted state is a normal first run -- never an exception, never None."""
    result = await store.async_load()

    assert result == {}
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_round_trip_preserves_state(store: ClimatePilotStore) -> None:
    data = {
        "zones": {
            "living_room": {
                "manual_override": {
                    "value": 21.5,
                    "created_at": "2026-02-03T12:00:00+00:00",
                    "policy": "next_schedule",
                },
                "last_confirmed_write": {
                    "target": 21.5,
                    "at": "2026-02-03T12:00:00+00:00",
                },
            }
        },
        "heat_state": "heating",
        "burner": {"starts_24h": 7},
    }

    await store.async_save(data)
    loaded = await store.async_load()

    assert loaded["zones"] == data["zones"]
    assert loaded["heat_state"] == "heating"
    assert loaded["burner"] == {"starts_24h": 7}


@pytest.mark.asyncio
async def test_saved_payload_carries_schema_header(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    await store.async_save({"heat_state": "idle"})

    raw = json.loads(store_path(hass, store.key).read_text(encoding="utf-8"))

    assert raw["version"] == STORAGE_VERSION
    assert raw["key"] == store.key
    assert raw["data"]["version"] == STORAGE_VERSION
    assert raw["data"]["system_id"] == "entry-abc"
    assert raw["data"]["heat_state"] == "idle"
    assert isinstance(raw["data"]["saved_at"], str)


@pytest.mark.asyncio
async def test_save_does_not_overwrite_the_schema_header(
    store: ClimatePilotStore,
) -> None:
    """A caller must not be able to inject a stale version or foreign system id."""
    await store.async_save({"version": 99, "system_id": "somebody-else", "heat_state": "heating"})

    loaded = await store.async_load()

    assert loaded["version"] == STORAGE_VERSION
    assert loaded["system_id"] == "entry-abc"
    assert loaded["heat_state"] == "heating"


@pytest.mark.asyncio
async def test_save_replaces_previous_payload(store: ClimatePilotStore) -> None:
    await store.async_save({"heat_state": "heating", "gone": True})
    await store.async_save({"heat_state": "idle"})

    loaded = await store.async_load()

    assert loaded["heat_state"] == "idle"
    assert "gone" not in loaded


@pytest.mark.asyncio
async def test_round_trip_survives_a_new_store_instance(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    """The restart path: a fresh store object reads what the old one wrote."""
    await store.async_save({"heat_state": "satisfied", "burner": {"starts_24h": 3}})

    reopened = ClimatePilotStore(hass, "entry-abc")
    loaded = await reopened.async_load()

    assert loaded["heat_state"] == "satisfied"
    assert loaded["burner"] == {"starts_24h": 3}


@pytest.mark.asyncio
async def test_load_returns_a_copy_not_the_live_dict(store: ClimatePilotStore) -> None:
    await store.async_save({"heat_state": "heating"})

    first = await store.async_load()
    first["heat_state"] = "MUTATED"
    second = await store.async_load()

    assert second["heat_state"] == "heating"


@pytest.mark.asyncio
async def test_save_with_empty_dict_is_loadable(store: ClimatePilotStore) -> None:
    await store.async_save({})

    loaded = await store.async_load()

    assert loaded["version"] == STORAGE_VERSION
    assert loaded["system_id"] == "entry-abc"


@pytest.mark.asyncio
async def test_save_payload_is_json_serialisable(store: ClimatePilotStore) -> None:
    """Round-tripping through the file must not lose nested structure/types."""
    data = {
        "zones": {"a": {"target": 21.5, "confirmed": True}},
        "list": [1, 2.5, "three", None, True],
        "nested": {"deep": {"deeper": {"value": -0.25}}},
    }

    await store.async_save(data)
    loaded = await store.async_load()

    for key, value in data.items():
        assert loaded[key] == value


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_deletes_persisted_state(hass: FakeHass, store: ClimatePilotStore) -> None:
    await store.async_save({"heat_state": "heating"})
    assert store_path(hass, store.key).exists()

    await store.async_remove()

    assert not store_path(hass, store.key).exists()
    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_remove_without_state_does_not_raise(store: ClimatePilotStore) -> None:
    await store.async_remove()

    assert await store.async_load() == {}


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_behind_version_file_yields_cold_start(
    hass: FakeHass, store: ClimatePilotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A v0 payload is below MIN_SUPPORTED_VERSION -> cold start, never a crash."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 0,
                "minor_version": 1,
                "key": store.key,
                "data": {"system_id": "entry-abc", "heat_state": "heating"},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        loaded = await store.async_load()

    assert loaded == {}
    assert any("could not be read" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_migrate_current_version_payload_is_read_verbatim(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    """A same-version payload must NOT go through the migrate path."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION,
                "minor_version": 1,
                "key": store.key,
                "data": {"system_id": "entry-abc", "heat_state": "satisfied"},
            }
        ),
        encoding="utf-8",
    )

    loaded = await store.async_load()

    assert loaded["heat_state"] == "satisfied"


@pytest.mark.asyncio
async def test_newer_version_file_yields_cold_start(
    hass: FakeHass, store: ClimatePilotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Data from a newer release is unreadable: start cold, log loudly."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION + 1,
                "minor_version": 0,
                "key": store.key,
                "data": {"system_id": "entry-abc", "heat_state": "heating"},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        loaded = await store.async_load()

    assert loaded == {}
    assert caplog.records


@pytest.mark.asyncio
async def test_migrate_rejects_foreign_system_id(store: ClimatePilotStore) -> None:
    """The hook itself still fails closed; async_load is what swallows it."""
    with pytest.raises(StoreDataInvalid):
        await store._async_migrate_func(
            STORAGE_VERSION, 1, {"system_id": "another-entry", "heat_state": "idle"}
        )


@pytest.mark.asyncio
async def test_migrate_direct_call_restamps_and_preserves_data(
    store: ClimatePilotStore,
) -> None:
    """Call the hook the way ``Store`` would, with the real 3-arg signature."""
    migrated = await store._async_migrate_func(
        STORAGE_VERSION, 1, {"system_id": "entry-abc", "heat_state": "heating"}
    )

    assert migrated["version"] == STORAGE_VERSION
    assert migrated["system_id"] == "entry-abc"
    assert migrated["heat_state"] == "heating"
    assert "saved_at" in migrated


@pytest.mark.asyncio
async def test_migrate_accepts_payload_without_system_id(
    store: ClimatePilotStore,
) -> None:
    """A payload predating the ``system_id`` stamp is still accepted."""
    migrated = await store._async_migrate_func(STORAGE_VERSION, 1, {"heat_state": "idle"})

    assert migrated["system_id"] == "entry-abc"
    assert migrated["heat_state"] == "idle"


@pytest.mark.asyncio
async def test_migrate_rejects_non_mapping_payload(store: ClimatePilotStore) -> None:
    with pytest.raises(StoreDataInvalid):
        await store._async_migrate_func(STORAGE_VERSION, 1, ["not", "a", "mapping"])


@pytest.mark.asyncio
async def test_migrate_hook_is_dispatched_by_store(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    """End-to-end: a real version mismatch must run OUR migrate hook.

    This is the regression test for the wrapper-vs-subclass trap: with a
    wrapping object, ``Store._async_load_data`` calls its own
    ``NotImplementedError`` stub and the data would be lost.
    """
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION,
                "minor_version": 0,  # older minor version -> migrate path
                "key": store.key,
                "data": {"heat_state": "heating"},
            }
        ),
        encoding="utf-8",
    )

    loaded = await store.async_load()

    # Our hook ran: it re-stamped system_id and saved_at.
    assert loaded["heat_state"] == "heating"
    assert loaded["system_id"] == "entry-abc"
    assert loaded["version"] == STORAGE_VERSION
    assert "saved_at" in loaded

    # The migrated payload was persisted back, still readable on a second load.
    assert (await store.async_load())["heat_state"] == "heating"


@pytest.mark.asyncio
async def test_migrate_hook_rejection_reaches_the_caller(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    """A rejected migration surfaces as StoreDataInvalid out of the real Store."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION,
                "minor_version": 0,  # triggers the migrate path
                "key": store.key,
                "data": {"system_id": "another-entry", "heat_state": "heating"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(StoreDataInvalid):
        await Store.async_load(store)


@pytest.mark.asyncio
async def test_migrate_hook_failure_is_swallowed_by_async_load(
    hass: FakeHass, store: ClimatePilotStore, caplog: pytest.LogCaptureFixture
) -> None:
    """...and is then turned into a safe cold start by ``async_load``."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION,
                "minor_version": 0,
                "key": store.key,
                "data": {"system_id": "another-entry", "heat_state": "heating"},
            }
        ),
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR):
        loaded = await store.async_load()

    assert loaded == {}
    assert any("could not be read" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# corrupt / hostile input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_json_yields_cold_start(hass: FakeHass, store: ClimatePilotStore) -> None:
    """Corrupt storage must not surface a JSONDecodeError at the caller."""
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")

    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_non_dict_top_level_payload_yields_cold_start(
    hass: FakeHass, store: ClimatePilotStore
) -> None:
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STORAGE_VERSION,
                "minor_version": 1,
                "key": store.key,
                "data": ["not", "a", "mapping"],
            }
        ),
        encoding="utf-8",
    )

    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_load_failure_never_raises(
    store: ClimatePilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any read failure at all degrades to a cold start, not to an aborted setup."""
    monkeypatch.setattr(Store, "async_load", AsyncMock(side_effect=RuntimeError("boom")))

    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_load_returns_empty_when_base_returns_none(
    store: ClimatePilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA's ``Store.async_load`` may legitimately return None; we normalise to {}."""
    monkeypatch.setattr(Store, "async_load", AsyncMock(return_value=None))

    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_load_returns_empty_when_base_returns_non_dict(
    store: ClimatePilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list payload is not usable state; normalise to {} rather than returning it."""
    monkeypatch.setattr(Store, "async_load", AsyncMock(return_value=["nope"]))

    assert await store.async_load() == {}


@pytest.mark.asyncio
async def test_save_failure_is_logged_not_raised(
    store: ClimatePilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing save degrades the next restart; it must not kill the caller."""
    boom = AsyncMock(side_effect=OSError("disk full"))
    monkeypatch.setattr(Store, "async_save", boom)

    await store.async_save({"heat_state": "heating"})  # must not raise

    boom.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_failure_is_logged_not_raised(
    store: ClimatePilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    boom = AsyncMock(side_effect=OSError("disk error"))
    monkeypatch.setattr(Store, "async_remove", boom)

    await store.async_remove()  # must not raise

    boom.assert_awaited_once()


# ---------------------------------------------------------------------------
# cancellation semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [("async_load", ()), ("async_save", ({},)), ("async_remove", ())],
)
async def test_cancellation_is_never_swallowed(
    store: ClimatePilotStore,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple,
) -> None:
    """Shutdown must not be blocked by the never-raise storage wrappers.

    ``CancelledError`` derives from ``BaseException``, so the ``except
    Exception`` guards let it through. Widening those to ``except
    BaseException`` would hang Home Assistant's shutdown path.
    """
    monkeypatch.setattr(Store, method, AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await getattr(store, method)(*args)


def test_store_never_catches_base_exception() -> None:
    """Structural guard: no bare ``except:`` and no ``except BaseException``."""
    import ast
    import inspect

    from custom_components.climatepilot import store as module

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        assert node.type is not None, "bare 'except:' is forbidden"
        if isinstance(node.type, ast.Name):
            assert node.type.id != "BaseException"


@pytest.mark.asyncio
async def test_empty_file_loads_as_empty_dict(hass: FakeHass, store: ClimatePilotStore) -> None:
    path = store_path(hass, store.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    assert await store.async_load() == {}
