"""The options flow must be savable on a live system.

Written after a real defect: saving any option raised

    ValueError: Config entry update listeners should not be used with
    OptionsFlowWithReload

because the entry carried an update listener *and* the flow derived from
``OptionsFlowWithReload``. Home Assistant forbids combining the two - a listener
would cause two reloads for one save - and the check lives in
``ConfigEntriesFlowManager.async_finish_flow``.

Every existing options test called the flow's methods directly, so none of them
reached that method and all of them passed while the feature was broken. The
options flow had therefore never worked on a live system; it was found by saving
settings on a real installation during the V0.5 deployment.

Structural assertions are used rather than an end-to-end flow run, because the
guard is reachable only through the flow manager and reproducing its wiring here
would test the harness rather than the integration.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

#: The integration's ``__init__`` module. Imported by its dotted name because
#: ``from custom_components.climatepilot import __init__`` resolves to the
#: *package's* ``__init__`` method-wrapper, which has no ``__file__`` - a trap that
#: silently turns every source inspection below into an AttributeError.
import custom_components.climatepilot.__init__ as integration_module
from custom_components.climatepilot import options_flow as of
from custom_components.climatepilot.const import CONF_GAS_METER_ENTITY

# ---------------------------------------------------------------------------
# The two mechanisms must not coexist
# ---------------------------------------------------------------------------


def test_the_options_flow_uses_the_automatically_reloading_base() -> None:
    """The flow still relies on ``OptionsFlowWithReload`` for its reload.

    Pinned because the fix was to remove the *listener*, not to drop this base
    class: the automatic reload is wanted, and it is what makes an options save
    take effect without a manual restart.
    """
    from homeassistant.config_entries import OptionsFlowWithReload

    assert issubclass(of.ClimatePilotOptionsFlow, OptionsFlowWithReload)
    assert of.ClimatePilotOptionsFlow.automatic_reload is True


def test_setup_registers_no_update_listener() -> None:
    """Nothing may re-add the listener that conflicts with it.

    Read from the parsed AST of ``async_setup_entry`` rather than by searching its
    text: the explanatory comment there *quotes* ``add_update_listener``, so a
    substring check would fail on the very comment documenting why it is absent.
    Only real call sites are counted.

    The guard fires only when a completed automatic-reload flow finishes, so a test
    that merely sets the integration up would stay green with the listener present -
    which is precisely how this survived a full suite.
    """
    tree = ast.parse(_dedented_source(integration_module.async_setup_entry))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "add_update_listener"
    ]
    assert not calls, "an update listener conflicts with OptionsFlowWithReload; HA raises on save"


def _dedented_source(func) -> str:
    """Source of ``func`` with its leading indentation removed, as valid syntax."""
    import textwrap

    return textwrap.dedent(inspect.getsource(func))


def test_the_old_listener_helper_is_gone() -> None:
    """And the helper itself is removed, so it cannot be re-attached by accident."""
    assert not hasattr(integration_module, "_async_update_listener")


def test_no_update_listener_call_exists_anywhere_in_the_module() -> None:
    """Belt and braces across the whole module, not just ``async_setup_entry``.

    A listener could be attached from a service handler or a migration; parsing
    the module catches every call site rather than one.

    Imported as ``custom_components.climatepilot.__init__``: importing the package
    with ``import ... as ...`` binds the package's ``__init__`` *method wrapper*
    on the parent object, which has no ``__file__``. Importing the submodule by its
    dotted name gets the module.
    """
    import custom_components.climatepilot.__init__ as init_module

    tree = ast.parse(Path(init_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "add_update_listener"
    ]
    assert not calls, "add_update_listener must not appear anywhere in the integration"


# ---------------------------------------------------------------------------
# The guard still exists, so these assertions keep meaning something
# ---------------------------------------------------------------------------


def test_the_home_assistant_guard_is_still_present() -> None:
    """Confirm Home Assistant still forbids the combination this test guards.

    If a future release drops the restriction these assertions become vacuous, and
    this test says so rather than letting them pass for the wrong reason.

    Matched on the *condition*, not on the message text, and across the module rather
    than by naming a class. Both vary by release: the guard sits on
    ``OptionsFlowManager`` in 2026.2 and on ``ConfigEntriesFlowManager`` in 2026.9, and
    the message is one string literal in the older version but split across two
    adjacent literals in the newer one - so searching for the sentence fails on
    2026.9 even though the behaviour is identical. That is exactly how this test first
    passed on one version and failed on the other.
    """
    import homeassistant.config_entries as ce

    source = inspect.getsource(ce)
    guard = "if automatic_reload and entry.update_listeners"
    assert guard in source, (
        "HA no longer forbids update listeners alongside OptionsFlowWithReload; "
        "the assertions in this module may now be vacuous"
    )
    assert "raise ValueError(" in source.split(guard)[1][:200]


# ---------------------------------------------------------------------------
# The energy step still saves through the real flow object
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_energy_step_creates_the_entry_data() -> None:
    """Submitting the energy step must produce a create-entry result.

    Reuses ``make_options_flow`` from the sibling options tests, which wires the
    flow the way ``OptionsFlowManager`` does - ``handler`` carrying the entry id,
    plus a ``hass`` double whose ``config_entries`` can resolve it. Hand-rolling
    that wiring with a bare ``Mock`` fails inside ``config`` with "Mock.keys()
    returned a non-iterable", which says nothing about the step under test.

    This does not reach ``async_finish_flow`` - nothing outside the flow manager
    does - but it proves the step persists the meter and the base temperature,
    which is the part the live save depends on.
    """
    from test_options_flow import build_hass, entry_with, make_options_flow

    from custom_components.climatepilot.const import CONF_DEGREE_DAY_BASE_C

    flow = make_options_flow(entry_with(), build_hass())
    result = await flow.async_step_energy(
        {CONF_GAS_METER_ENTITY: "sensor.gas_meter", CONF_DEGREE_DAY_BASE_C: 15.5}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_GAS_METER_ENTITY] == "sensor.gas_meter"
    assert result["data"][CONF_DEGREE_DAY_BASE_C] == 15.5


@pytest.mark.asyncio
async def test_clearing_the_energy_step_stores_none() -> None:
    """Clearing the meter must persist ``None``, which is the documented "off"."""
    from test_options_flow import build_hass, entry_with, make_options_flow

    from custom_components.climatepilot.const import CONF_DEGREE_DAY_BASE_C

    flow = make_options_flow(entry_with(), build_hass())
    result = await flow.async_step_energy(
        {CONF_GAS_METER_ENTITY: None, CONF_DEGREE_DAY_BASE_C: 15.5}
    )

    assert result["data"][CONF_GAS_METER_ENTITY] is None
