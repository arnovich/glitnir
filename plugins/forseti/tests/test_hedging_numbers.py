"""Exact prices, hedge ratios and worst extreme paths of the hedging lane."""

from fractions import Fraction
from itertools import product

import pytest

from glitnir_forseti.hedging.numbers import (
    Interrupted,
    delta,
    fair_weight,
    price,
    wealth,
    worst_extreme_path,
)
from glitnir_forseti.hedging.payoffs import PAYOFFS

CALL = PAYOFFS["call"]
PUT = PAYOFFS["put"]
STRADDLE = PAYOFFS["straddle"]

STRIKE = Fraction(100)
SPOT = Fraction(100)
TWO_PERCENT = Fraction(1, 50)
ONE_PERCENT = Fraction(1, 100)
STEPS = 20

#: The at-the-money call at the ±2 % band with twenty steps to go.
PRICE_20 = Fraction(
    177744620736998422379605070486628497811,
    50000000000000000000000000000000000000,
)
#: The ±1 % hedge's wealth less payoff on the ±2 % zigzag of twenty returns.
ZIGZAG_SHORTFALL = Fraction(
    -215634705196340256271204096412694900382147731,
    52428800000000000000000000000000000000000000,
)
ZIGZAG_RETURNS = tuple(
    TWO_PERCENT if step % 2 == 0 else -TWO_PERCENT for step in range(STEPS)
)
ZIGZAG_PATH = tuple(step % 2 == 0 for step in range(STEPS))


def never() -> bool:
    """A ``stop`` callback that never asks to stop."""
    return False


def test_payoff_registry_names_and_values() -> None:
    """Each payoff cites its Lean definitions and evaluates as its name says."""
    assert set(PAYOFFS) == {"call", "put", "straddle"}
    for key, payoff in PAYOFFS.items():
        lean = f"Gimle.Forseti.Hedging.{key}"
        assert payoff.key == key
        assert payoff.lean_name == lean
        assert payoff.lean_q == f"{lean}Q"
        assert payoff.cast_lemma == f"{lean}_cast"
        assert payoff.convexity == f"{lean}_convexOn"
    strike = Fraction(100)
    assert CALL.value(strike, Fraction(103)) == 3
    assert CALL.value(strike, Fraction(95)) == 0
    assert PUT.value(strike, Fraction(103)) == 0
    assert PUT.value(strike, Fraction(95)) == 5
    assert STRADDLE.value(strike, Fraction(103)) == 3
    assert STRADDLE.value(strike, Fraction(95)) == 5


def test_fair_weight_of_a_symmetric_band_is_one_half() -> None:
    """Equal moves up and down weigh each other equally."""
    assert fair_weight(TWO_PERCENT, TWO_PERCENT) == Fraction(1, 2)


def test_fair_weight_makes_the_spot_a_martingale() -> None:
    """``q(1+up) + (1-q)(1-down) = 1`` for a lopsided band."""
    up, down = Fraction(1, 10), Fraction(1, 20)
    q = fair_weight(up, down)
    assert q == Fraction(1, 3)
    assert q * (1 + up) + (1 - q) * (1 - down) == 1


def test_price_of_the_at_the_money_call_with_twenty_steps() -> None:
    """The closed form reproduces the tabulated price to the last digit."""
    assert price(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, STEPS) == PRICE_20


def test_price_of_a_put_equals_that_of_a_call_at_the_money() -> None:
    """A symmetric band at the strike makes put and call the same price."""
    put = price(PUT, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, STEPS)
    assert put == PRICE_20


def test_price_with_one_step_is_half_the_gap() -> None:
    """One step: half of the 2 by which the up move clears the strike."""
    assert price(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 1) == 1


def test_price_with_no_steps_is_the_payoff() -> None:
    """With nothing left to move the price is the payoff at the spot."""
    assert price(CALL, STRIKE, Fraction(107), TWO_PERCENT, TWO_PERCENT, 0) == 7
    assert price(PUT, STRIKE, Fraction(107), TWO_PERCENT, TWO_PERCENT, 0) == 0


def test_price_of_a_straddle_is_the_call_plus_the_put() -> None:
    """The straddle's payoff is the sum of the two, and so is its price."""
    args = (STRIKE, Fraction(97), Fraction(1, 10), Fraction(1, 20), 7)
    assert price(STRADDLE, *args) == price(CALL, *args) + price(PUT, *args)


def test_delta_with_one_step_to_go() -> None:
    """The ratio is the payoff gap over the spot gap: 2 / 4 at the money."""
    assert delta(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 0) == Fraction(1, 2)
    assert delta(PUT, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 0) == Fraction(-1, 2)
    assert delta(STRADDLE, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 0) == 0


def test_delta_is_the_price_difference_over_the_spot_gap() -> None:
    """With ``n`` steps to go the ratio differences the price at the two moves."""
    up, down, n = Fraction(1, 10), Fraction(1, 20), 5
    spot = Fraction(104)
    expected = (
        price(CALL, STRIKE, spot * (1 + up), up, down, n)
        - price(CALL, STRIKE, spot * (1 - down), up, down, n)
    ) / (spot * (up + down))
    assert delta(CALL, STRIKE, spot, up, down, n) == expected
    assert 0 < expected < 1


def test_the_bands_own_hedge_replicates_on_every_extreme_path() -> None:
    """Starting from the price, the hedge ends at the payoff on all paths."""
    up, down, steps = Fraction(1, 10), Fraction(1, 20), 6
    for payoff in (CALL, PUT, STRADDLE):
        for ups in product((True, False), repeat=steps):
            returns = tuple(up if move else -down for move in ups)
            final, last = wealth(payoff, STRIKE, SPOT, up, down, steps, returns)
            assert final == payoff.value(STRIKE, last)


def test_wealth_on_the_zigzag_falls_short_of_the_payoff() -> None:
    """The ±1 % hedge on the ±2 % zigzag ends below the payoff by the tabulated sum."""
    final, last = wealth(
        CALL, STRIKE, SPOT, ONE_PERCENT, ONE_PERCENT, STEPS, ZIGZAG_RETURNS
    )
    assert last == SPOT * (1 + TWO_PERCENT) ** 10 * (1 - TWO_PERCENT) ** 10
    assert final - CALL.value(STRIKE, last) == ZIGZAG_SHORTFALL


def test_the_zigzag_is_the_worst_extreme_path() -> None:
    """Of all extreme paths of the ±2 % band, the zigzag hurts the ±1 % hedge most."""
    path, shortfall = worst_extreme_path(
        CALL,
        STRIKE,
        SPOT,
        ONE_PERCENT,
        ONE_PERCENT,
        TWO_PERCENT,
        TWO_PERCENT,
        STEPS,
        never,
    )
    assert path == ZIGZAG_PATH
    assert shortfall == ZIGZAG_SHORTFALL


def test_the_worst_extreme_path_agrees_with_enumeration() -> None:
    """The backward recursion finds the minimum a brute-force search finds."""
    steps = 7
    hedge = (Fraction(1, 100), Fraction(1, 50))
    against_up, against_down = Fraction(1, 25), Fraction(1, 30)
    for payoff in (CALL, PUT, STRADDLE):
        shortfalls = []
        for ups in product((True, False), repeat=steps):
            returns = tuple(against_up if up else -against_down for up in ups)
            final, last = wealth(payoff, STRIKE, SPOT, *hedge, steps, returns)
            shortfalls.append(final - payoff.value(STRIKE, last))
        path, shortfall = worst_extreme_path(
            payoff, STRIKE, SPOT, *hedge, against_up, against_down, steps, never
        )
        assert shortfall == min(shortfalls)
        returns = tuple(against_up if up else -against_down for up in path)
        final, last = wealth(payoff, STRIKE, SPOT, *hedge, steps, returns)
        assert final - payoff.value(STRIKE, last) == shortfall


def test_a_hedge_run_on_its_own_band_never_falls_short() -> None:
    """Hedge band equal to against band: the worst extreme shortfall is zero."""
    for payoff in (CALL, PUT, STRADDLE):
        _, shortfall = worst_extreme_path(
            payoff,
            STRIKE,
            SPOT,
            TWO_PERCENT,
            TWO_PERCENT,
            TWO_PERCENT,
            TWO_PERCENT,
            STEPS,
            never,
        )
        assert shortfall == 0


def test_a_wider_hedge_does_not_fall_short_on_a_narrower_band() -> None:
    """A hedge built for ±2 % is never short on the extreme paths of ±1 %."""
    _, shortfall = worst_extreme_path(
        CALL,
        STRIKE,
        SPOT,
        TWO_PERCENT,
        TWO_PERCENT,
        ONE_PERCENT,
        ONE_PERCENT,
        STEPS,
        never,
    )
    assert shortfall >= 0


def test_worst_extreme_path_with_no_steps_is_empty() -> None:
    """With no step the hedge is the price, which meets the payoff exactly."""
    path, shortfall = worst_extreme_path(
        CALL,
        STRIKE,
        Fraction(103),
        ONE_PERCENT,
        ONE_PERCENT,
        TWO_PERCENT,
        TWO_PERCENT,
        0,
        never,
    )
    assert path == () and shortfall == 0


def test_worst_extreme_path_stops_when_asked() -> None:
    """A ``stop`` that is true from the start interrupts the search."""
    with pytest.raises(Interrupted):
        worst_extreme_path(
            CALL,
            STRIKE,
            SPOT,
            ONE_PERCENT,
            ONE_PERCENT,
            TWO_PERCENT,
            TWO_PERCENT,
            STEPS,
            lambda: True,
        )


def test_worst_extreme_path_polls_stop_during_the_search() -> None:
    """A ``stop`` that turns true part-way interrupts it there."""
    asked: list[None] = []

    def stop() -> bool:
        asked.append(None)
        return len(asked) > 5

    with pytest.raises(Interrupted):
        worst_extreme_path(
            CALL,
            STRIKE,
            SPOT,
            ONE_PERCENT,
            ONE_PERCENT,
            TWO_PERCENT,
            TWO_PERCENT,
            STEPS,
            stop,
        )
    assert len(asked) == 6


def test_bad_arguments_are_refused() -> None:
    """Negative steps, a non-positive spot, a degenerate band or bad returns."""
    with pytest.raises(ValueError, match="steps"):
        price(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, -1)
    with pytest.raises(ValueError, match="steps"):
        delta(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, -1)
    with pytest.raises(ValueError, match="spot"):
        price(CALL, STRIKE, Fraction(0), TWO_PERCENT, TWO_PERCENT, 1)
    with pytest.raises(ValueError, match="band"):
        fair_weight(Fraction(0), Fraction(0))
    with pytest.raises(ValueError, match="band"):
        fair_weight(TWO_PERCENT, Fraction(1))
    with pytest.raises(ValueError, match="returns"):
        wealth(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 2, (TWO_PERCENT,))
    with pytest.raises(ValueError, match="return"):
        wealth(CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, 1, (Fraction(-1),))


@pytest.mark.parametrize(
    ("up", "down"),
    [
        (Fraction(0), TWO_PERCENT),
        (TWO_PERCENT, Fraction(0)),
        (-TWO_PERCENT, TWO_PERCENT),
        (TWO_PERCENT, -TWO_PERCENT),
        (TWO_PERCENT, Fraction(1)),
        (TWO_PERCENT, Fraction(3, 2)),
    ],
)
def test_a_band_is_refused_unless_lean_would_accept_it(
    up: Fraction, down: Fraction
) -> None:
    """``Band.ofRat`` needs ``0 < up``, ``0 < down`` and ``down < 1``."""
    with pytest.raises(ValueError, match="band must have 0 < up and 0 < down < 1"):
        fair_weight(up, down)
    with pytest.raises(ValueError, match="band"):
        price(CALL, STRIKE, SPOT, up, down, 1)
    with pytest.raises(ValueError, match="band"):
        worst_extreme_path(
            CALL, STRIKE, SPOT, TWO_PERCENT, TWO_PERCENT, up, down, 1, never
        )
