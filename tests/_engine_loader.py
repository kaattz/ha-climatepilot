"""Shared loader for the pure-engine test modules.

The engine tests must be able to import a pure engine **without** importing the
integration package ``custom_components.climatepilot.__init__``, which pulls in
Home Assistant. That isolation is deliberate and is itself asserted by
``tests/test_contracts.py``.

The naive way to achieve it - installing a stub module under the *real* package
name - has a nasty consequence: if ``__init__.py`` genuinely breaks, the stub
hides it, and the engine tests keep passing while the integration is dead. Even
worse, the resulting error surfaces later as a confusing
``cannot import name ... (unknown location)`` from an unrelated module, pointing
whoever debugs it at the wrong file.

So the engines are loaded under an **alias** package name instead. The alias
never touches ``custom_components.climatepilot``, so:

* a broken ``__init__.py`` stays broken and visible to the integration tests;
* the engine tests remain independent of Home Assistant, as intended.

Usage::

    from _engine_loader import load_engine

    wc = load_engine("weather_compensator")
    const = load_engine("const")
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPONENT_DIR = REPO_ROOT / "custom_components" / "climatepilot"
ALIAS = "_climatepilot_engine"


def _ensure_repo_on_path() -> None:
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _ensure_alias_package() -> types.ModuleType:
    """Create the alias package once, pointing at the real component directory."""
    existing = sys.modules.get(ALIAS)
    if existing is not None:
        return existing

    alias = types.ModuleType(ALIAS)
    alias.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules[ALIAS] = alias
    return alias


def load_engine(name: str) -> types.ModuleType:
    """Import ``custom_components/climatepilot/<name>.py`` under the alias.

    Returns the same module object on repeated calls, so tests that compare
    constants across engines see one instance.
    """
    _ensure_repo_on_path()
    _ensure_alias_package()
    return importlib.import_module(f"{ALIAS}.{name}")
