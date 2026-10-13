"""The heating-season switch: the off-season gate.

The switch answers one question - "is the heating season running?" - and does it
by mapping onto the control mode that already exists, rather than adding a
fourth system state. Two independent flags for the same decision (a "season" flag
and a "control mode") would eventually disagree, and the reader would have no way
to tell which one won.

The mapping is deliberately asymmetric:

    OFF -> paused    stop coordinating; withdraw our own heat request
    ON  -> observe   resume monitoring and suggestions, still writing nothing

ON does **not** select ``coordinate``. Turning the season on should not silently
begin writing to the room thermostats - that skips the review step observe mode
exists for, and it would change behaviour on the user's heating system without a
fresh confirmation.
"""

from __future__ import annotations

import pytest

from custom_components.climatepilot.const import (
    CONTROL_MODE_COORDINATE,
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
    CONTROL_MODES,
)


@pytest.mark.parametrize(
    ("season_on", "expected_mode"),
    [(True, CONTROL_MODE_OBSERVE), (False, CONTROL_MODE_PAUSED)],
)
def test_switch_maps_to_a_control_mode(season_on: bool, expected_mode: str) -> None:
    from custom_components.climatepilot.switch import season_to_control_mode

    assert season_to_control_mode(season_on) == expected_mode


def test_opening_the_season_never_selects_coordinate() -> None:
    """The switch must not be able to start real control on its own.

    This is the safety property, not a style preference: coordinate mode writes
    to the KNX thermostats, and reaching it by flipping a "season" switch would
    be an unannounced change to a live heating system.
    """
    from custom_components.climatepilot.switch import season_to_control_mode

    assert season_to_control_mode(True) != CONTROL_MODE_COORDINATE


@pytest.mark.parametrize(
    ("mode", "expected_on"),
    [
        (CONTROL_MODE_PAUSED, False),
        (CONTROL_MODE_OBSERVE, True),
        # Reached only by an explicit user choice, but the switch must still
        # report the season as running rather than showing OFF.
        (CONTROL_MODE_COORDINATE, True),
    ],
)
def test_control_mode_maps_back_to_the_switch_state(
    mode: str, expected_on: bool
) -> None:
    from custom_components.climatepilot.switch import control_mode_to_season

    assert control_mode_to_season(mode) is expected_on


def test_every_control_mode_has_a_defined_switch_state() -> None:
    """A new control mode must not silently read as "season off"."""
    from custom_components.climatepilot.switch import control_mode_to_season

    for mode in CONTROL_MODES:
        assert isinstance(control_mode_to_season(mode), bool), mode


def test_round_trip_is_stable_for_paused_and_observe() -> None:
    """Flipping the switch off and on must not drift into another mode.

    ``coordinate`` is excluded because the round trip is intentionally lossy
    there: turning the season off and on again returns to observe, not to
    coordinate. That is the safe direction, and it is asserted separately below.
    """
    from custom_components.climatepilot.switch import (
        control_mode_to_season,
        season_to_control_mode,
    )

    for mode in (CONTROL_MODE_PAUSED, CONTROL_MODE_OBSERVE):
        assert season_to_control_mode(control_mode_to_season(mode)) == mode


def test_leaving_and_returning_from_coordinate_lands_in_observe() -> None:
    """Coordinate is never restored by the switch - observe is the safe landing."""
    from custom_components.climatepilot.switch import (
        control_mode_to_season,
        season_to_control_mode,
    )

    assert season_to_control_mode(control_mode_to_season(CONTROL_MODE_COORDINATE)) == (
        CONTROL_MODE_OBSERVE
    )
