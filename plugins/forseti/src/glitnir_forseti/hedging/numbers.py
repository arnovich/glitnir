"""Exact rational arithmetic behind the hedging lane.

Every function takes and returns `fractions.Fraction` (or `int` for counts);
nothing here touches floating point, so a figure is the very number the
corresponding theorem states. At each step the spot ``S`` moves to some ``S'``
in ``[S(1 - down), S(1 + up)]``. The module computes:

* the fair weight ``q = down / (up + down)``, for which
  ``q(1 + up) + (1 - q)(1 - down) = 1``;
* the binomial price with ``n`` steps to go, in closed form,
  ``Σ_{j≤n} C(n, j) q^j (1 - q)^(n-j) · g(S (1 + up)^j (1 - down)^(n-j))``
  for a payoff ``g``;
* the hedge ratio with ``n`` steps to go at spot ``S``,
  ``(price(n, S(1 + up)) - price(n, S(1 - down))) / (S (up + down))``;
* the wealth of a band's own hedge, which starts from the price and holds the
  ratio with ``N - 1 - k`` steps to go over step ``k``, along given returns;
* the worst extreme path of a hedge built for one band but run on the extreme
  moves of another: the path, each move to an end of that band, that minimises
  wealth less payoff.

The worst path is found by backward dynamic programming on the recombining
tree. The hedge ratio depends only on the step and the spot, and the spot only
on the step and the number of up moves, so the state is ``(step, ups)`` and the
value is the least, over continuations, of the remaining ``Σ δ·ΔS - payoff``.
The tree has about ``N²/2`` states, each costing one hedge ratio, and each
hedge ratio is itself two prices of up to ``N`` terms over rationals whose size
grows with the step, so the search is about cubic in ``N`` in rational
operations, on numbers that grow too. The search asks its ``stop`` callback
before each state, never during one, and raises `Interrupted` when it answers
true.
"""

from collections.abc import Callable, Sequence
from fractions import Fraction
from math import comb

from glitnir_forseti.hedging.payoffs import Payoff

#: Asked at every state of a long search: whether to stop it.
Stop = Callable[[], bool]


class Interrupted(Exception):
    """Raised when a search's ``stop`` callback asks it to stop."""


def _check(stop: Stop) -> None:
    """Raise `Interrupted` if ``stop`` asks to stop."""
    if stop():
        raise Interrupted


def _require_steps(name: str, steps: int) -> None:
    """Raise `ValueError` unless ``steps`` is a non-negative integer."""
    if steps < 0:
        raise ValueError(f"{name} must not be negative, got {steps}")


def _require_spot(spot: Fraction) -> None:
    """Raise `ValueError` unless ``spot`` is strictly positive."""
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot}")


def _require_band(up: Fraction, down: Fraction) -> None:
    """Raise `ValueError` unless the band is one `Band.ofRat` accepts.

    Those are ``0 < up``, ``0 < down`` and ``down < 1``: the spot moves both
    ways and stays positive.
    """
    if not (0 < up and 0 < down < 1):
        raise ValueError(
            f"band must have 0 < up and 0 < down < 1, got up={up}, down={down}"
        )


def fair_weight(up: Fraction, down: Fraction) -> Fraction:
    """Return ``q = down / (up + down)``, the weight of the up move."""
    _require_band(up, down)
    return down / (up + down)


def price(
    payoff: Payoff,
    strike: Fraction,
    spot: Fraction,
    up: Fraction,
    down: Fraction,
    steps: int,
) -> Fraction:
    """Return the binomial price of ``payoff`` at ``spot`` with ``steps`` to go.

    This is ``Σ_{j≤n} C(n, j) q^j (1 - q)^(n-j) · g(S (1 + up)^j (1 - down)^(n-j))``
    with ``n = steps`` and ``q`` the `fair_weight`.
    """
    _require_steps("steps", steps)
    _require_spot(spot)
    weight = fair_weight(up, down)
    rise, fall = 1 + up, 1 - down
    return sum(
        (
            comb(steps, j)
            * weight**j
            * (1 - weight) ** (steps - j)
            * payoff.value(strike, spot * rise**j * fall ** (steps - j))
            for j in range(steps + 1)
        ),
        Fraction(0),
    )


def delta(
    payoff: Payoff,
    strike: Fraction,
    spot: Fraction,
    up: Fraction,
    down: Fraction,
    steps_to_go: int,
) -> Fraction:
    """Return the hedge ratio held at ``spot`` with ``steps_to_go`` steps after it.

    It is the price at the up end of the next move less the price at its down
    end, over the spot gap ``S (up + down)``, each price with ``steps_to_go``
    steps still to go.
    """
    _require_steps("steps_to_go", steps_to_go)
    _require_spot(spot)
    _require_band(up, down)
    high = price(payoff, strike, spot * (1 + up), up, down, steps_to_go)
    low = price(payoff, strike, spot * (1 - down), up, down, steps_to_go)
    return (high - low) / (spot * (up + down))


def wealth(
    payoff: Payoff,
    strike: Fraction,
    spot: Fraction,
    up: Fraction,
    down: Fraction,
    steps: int,
    returns: Sequence[Fraction],
) -> tuple[Fraction, Fraction]:
    """Return ``(final wealth, final spot)`` of the band's own hedge on ``returns``.

    The hedge starts from ``price(steps)`` and over step ``k`` holds
    `delta` with ``steps - 1 - k`` steps to go, so each return ``r`` adds
    ``δ · S · r`` and takes the spot from ``S`` to ``S (1 + r)``. Subtract
    ``payoff.value(strike, final spot)`` for the shortfall.
    """
    _require_steps("steps", steps)
    if len(returns) != steps:
        raise ValueError(f"returns must have {steps} entries, got {len(returns)}")
    cash = price(payoff, strike, spot, up, down, steps)
    current = spot
    for k, step_return in enumerate(returns):
        if step_return <= -1:
            raise ValueError(
                f"return {step_return} at step {k} would take the spot to zero or below"
            )
        ratio = delta(payoff, strike, current, up, down, steps - 1 - k)
        cash += ratio * current * step_return
        current *= 1 + step_return
    return cash, current


def worst_extreme_path(
    payoff: Payoff,
    strike: Fraction,
    spot: Fraction,
    hedge_up: Fraction,
    hedge_down: Fraction,
    against_up: Fraction,
    against_down: Fraction,
    steps: int,
    stop: Stop,
) -> tuple[tuple[bool, ...], Fraction]:
    """Return the worst extreme path of the ``against`` band and its shortfall.

    The hedge is the one built for the hedge band, starting from its price. A
    path moves up by ``against_up`` or down by ``against_down`` at each step;
    ``True`` marks an up move. The shortfall is the least wealth less payoff
    over all such paths, found backwards over the recombining tree. On a tie
    the path takes the up move. A hedge run on its own band has shortfall 0.

    Raises:
        Interrupted: If ``stop``, asked at every state of the search, answers
            true.
    """
    _require_steps("steps", steps)
    _require_spot(spot)
    _require_band(hedge_up, hedge_down)
    _require_band(against_up, against_down)
    rise, fall = 1 + against_up, 1 - against_down

    def spot_at(step: int, ups: int) -> Fraction:
        """Return the spot after ``step`` moves, ``ups`` of them up."""
        return spot * rise**ups * fall ** (step - ups)

    # value[ups] is the least remaining ``Σ δ·ΔS - payoff`` from (step, ups).
    value: list[Fraction] = []
    for ups in range(steps + 1):
        _check(stop)
        value.append(-payoff.value(strike, spot_at(steps, ups)))
    went_up: list[list[bool]] = []
    for step in range(steps - 1, -1, -1):
        row_value: list[Fraction] = []
        row_up: list[bool] = []
        for ups in range(step + 1):
            _check(stop)
            here = spot_at(step, ups)
            ratio = delta(payoff, strike, here, hedge_up, hedge_down, steps - 1 - step)
            rises = ratio * here * against_up + value[ups + 1]
            falls = -ratio * here * against_down + value[ups]
            row_up.append(rises <= falls)
            row_value.append(min(rises, falls))
        value = row_value
        went_up.append(row_up)
    went_up.reverse()
    path: list[bool] = []
    ups = 0
    for step in range(steps):
        moved_up = went_up[step][ups]
        path.append(moved_up)
        ups += moved_up
    start = price(payoff, strike, spot, hedge_up, hedge_down, steps)
    return tuple(path), start + value[0]
