"""The convex payoffs a hedging goal may name, and the Lean names a block cites.

A payoff maps a strike and a final spot to what the option pays. Each entry also
records the qualified Lean names of its definition over the reals, its rational
twin, the lemma that the two agree, and its convexity theorem, which a block
cites. Like a stepped model an entry decides which constants a goal's words
refer to and is not trusted to be right: the arithmetic only chooses a
template and its constants, and Lean checks the block, so a name that disagrees
with the Lean makes a block Lean refuses, never a proof of another claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

_HEDGING = "Gimle.Forseti.Hedging"


@dataclass(frozen=True, slots=True)
class Payoff:
    """One convex payoff.

    Attributes:
        key: The stable name a goal uses.
        lean_name: The payoff as a function on the reals.
        lean_q: The same payoff on the rationals, which the arithmetic runs.
        cast_lemma: The lemma that `lean_q` is `lean_name` on a cast rational.
        convexity: The theorem that `lean_name` is convex on the reals.
        value: The payoff, from the strike and the final spot, exactly.
    """

    key: str
    lean_name: str
    lean_q: str
    cast_lemma: str
    convexity: str
    value: Callable[[Fraction, Fraction], Fraction]


def _call(strike: Fraction, spot: Fraction) -> Fraction:
    """Return ``max(spot - strike, 0)``."""
    return max(spot - strike, Fraction(0))


def _put(strike: Fraction, spot: Fraction) -> Fraction:
    """Return ``max(strike - spot, 0)``."""
    return max(strike - spot, Fraction(0))


def _straddle(strike: Fraction, spot: Fraction) -> Fraction:
    """Return ``|spot - strike|``."""
    return abs(spot - strike)


def _payoff(key: str, value: Callable[[Fraction, Fraction], Fraction]) -> Payoff:
    """Return the payoff ``key`` with the Lean names ``Hedging.<key>…``."""
    lean = f"{_HEDGING}.{key}"
    return Payoff(
        key=key,
        lean_name=lean,
        lean_q=f"{lean}Q",
        cast_lemma=f"{lean}_cast",
        convexity=f"{lean}_convexOn",
        value=value,
    )


PAYOFFS: dict[str, Payoff] = {
    payoff.key: payoff
    for payoff in (
        _payoff("call", _call),
        _payoff("put", _put),
        _payoff("straddle", _straddle),
    )
}
