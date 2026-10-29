"""Closed goals about hedging a convex payoff under a move band, and their encoding.

A hedging goal names a payoff (a call, a put or a straddle) with its strike, the
spot the hedge starts from, and a band within which the spot may move at each
of ``steps`` steps: by a fraction ``down`` below and ``up`` above. It makes one
claim about the hedge built for that band:

* ``covers``: the band's own binomial hedge, started from the cash ``premium``,
  covers the payoff on every path in the band;
* ``shortfall``: the same hedge, started from its own price, is run on every
  path in the wider ``against`` band, and ends at most ``bound`` below the
  payoff.

``gimle.forseti.hedging-goal/v1`` is a strict JSON encoding of it, one key per
field; ``premium`` is present exactly for ``covers`` and ``against_up``,
``against_down`` and ``bound`` exactly for ``shortfall``, so each goal has one
encoding. Decoding checks shape and bounds only; whether the payoff exists and
the band is one the lane can decide is the decider's question, answered with
``UNKNOWN`` rather than a decoding error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, ClassVar, Literal

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import (
    GoalError,
    decode_rational,
    encode_rational,
)

SCHEMA = "gimle.forseti.hedging-goal/v1"
CLAIMS = ("covers", "shortfall")
#: Payoff keys are names, as the goal text reads them and as a Lean identifier
#: carries them.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_KEYS = frozenset(
    {"schema", "payoff", "strike", "spot", "up", "down", "steps", "claim"}
)
#: The keys each claim adds to `_KEYS`, and so the only ones it may carry.
_CLAIM_KEYS = {
    "covers": frozenset({"premium"}),
    "shortfall": frozenset({"against_up", "against_down", "bound"}),
}
_DEFAULT_LIMITS = EntailmentLimits()


@dataclass(frozen=True)
class HedgingGoal:
    """A closed goal about hedging a convex payoff under a move band.

    Attributes:
        payoff: The payoff's key: ``call``, ``put`` or ``straddle``.
        strike: The strike; not negative.
        spot: The spot the hedge starts from; positive.
        up: How far the spot may rise in one step, as a fraction of it;
            positive.
        down: How far the spot may fall in one step, as a fraction of it;
            positive.
        steps: The number of steps; at least one.
        claim: ``covers`` or ``shortfall``.
        premium: The cash the hedge starts from; not negative. Present
            exactly for ``covers``.
        against_up: The rise per step of the band the hedge is run on;
            positive. Present exactly for ``shortfall``.
        against_down: The fall per step of the band the hedge is run on;
            positive. Present exactly for ``shortfall``.
        bound: The most the hedge may end below the payoff. Present exactly
            for ``shortfall``.
    """

    SCHEMA: ClassVar[str] = SCHEMA

    payoff: str
    strike: Fraction
    spot: Fraction
    up: Fraction
    down: Fraction
    steps: int
    claim: Literal["covers", "shortfall"]
    premium: Fraction | None = None
    against_up: Fraction | None = None
    against_down: Fraction | None = None
    bound: Fraction | None = None

    def __post_init__(self) -> None:
        """Check tags, names, exact values and which fields the claim carries."""
        if self.claim not in CLAIMS:
            raise GoalError(f"unknown claim {self.claim!r}")
        if type(self.payoff) is not str or not _NAME.fullmatch(self.payoff):
            raise GoalError(f"invalid payoff name {self.payoff!r}")
        for label, number in (
            ("strike", self.strike),
            ("spot", self.spot),
            ("up", self.up),
            ("down", self.down),
        ):
            if type(number) is not Fraction:
                raise GoalError(f"the {label} is a fractions.Fraction")
        if type(self.steps) is not int or self.steps < 1:
            raise GoalError(
                f"the steps are a whole number, at least 1, got {self.steps!r}"
            )
        if self.strike < 0:
            raise GoalError(f"the strike must not be negative, got {self.strike}")
        if self.spot <= 0:
            raise GoalError(f"the spot must be positive, got {self.spot}")
        if self.up <= 0:
            raise GoalError(f"the up move must be positive, got {self.up}")
        if self.down <= 0:
            raise GoalError(f"the down move must be positive, got {self.down}")
        if self.claim == "covers":
            self._check_covers()
        else:
            self._check_shortfall()

    def _check_covers(self) -> None:
        """Check that a ``covers`` goal has a premium and nothing of a shortfall."""
        if (self.against_up, self.against_down, self.bound) != (None, None, None):
            raise GoalError("a covers claim has no against band and no bound")
        if type(self.premium) is not Fraction:
            raise GoalError("a covers claim has a premium, a Fraction")
        if self.premium < 0:
            raise GoalError(f"the premium must not be negative, got {self.premium}")

    def _check_shortfall(self) -> None:
        """Check that a ``shortfall`` goal has an against band, a bound, no premium."""
        if self.premium is not None:
            raise GoalError("a shortfall claim has no premium")
        for label, number in (
            ("against up", self.against_up),
            ("against down", self.against_down),
            ("bound", self.bound),
        ):
            if type(number) is not Fraction:
                raise GoalError(f"a shortfall claim has a {label}, a Fraction")
        assert self.against_up is not None and self.against_down is not None
        if self.against_up <= 0:
            raise GoalError(
                f"the against up move must be positive, got {self.against_up}"
            )
        if self.against_down <= 0:
            raise GoalError(
                f"the against down move must be positive, got {self.against_down}"
            )

    def to_json(self) -> dict[str, Any]:
        """Encode the goal as its versioned JSON document."""
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "payoff": self.payoff,
            "strike": encode_rational(self.strike),
            "spot": encode_rational(self.spot),
            "up": encode_rational(self.up),
            "down": encode_rational(self.down),
            "steps": self.steps,
            "claim": self.claim,
        }
        for key, number in (
            ("premium", self.premium),
            ("against_up", self.against_up),
            ("against_down", self.against_down),
            ("bound", self.bound),
        ):
            if number is not None:
                document[key] = encode_rational(number)
        return document

    @classmethod
    def from_json(
        cls, data: Any, limits: EntailmentLimits = _DEFAULT_LIMITS
    ) -> HedgingGoal:
        """Read a versioned goal document, refusing anything malformed.

        Raises:
            GoalError: The document is not exactly a v1 hedging goal.
        """
        if type(data) is not dict or "claim" not in data:
            raise GoalError("a hedging goal has exactly the v1 fields")
        claim = data["claim"]
        if type(claim) is not str or claim not in CLAIMS:
            raise GoalError(f"unknown claim {claim!r}")
        if set(data) != _KEYS | _CLAIM_KEYS[claim]:
            raise GoalError(f"a {claim} hedging goal has exactly the v1 fields")
        if data["schema"] != SCHEMA:
            raise GoalError("unsupported hedging goal schema")
        if type(data["payoff"]) is not str:
            raise GoalError("payoff is text")
        return cls(
            payoff=data["payoff"],
            strike=decode_rational(data["strike"], limits),
            spot=decode_rational(data["spot"], limits),
            up=decode_rational(data["up"], limits),
            down=decode_rational(data["down"], limits),
            steps=data["steps"],
            claim=data["claim"],
            premium=_optional(data, "premium", limits),
            against_up=_optional(data, "against_up", limits),
            against_down=_optional(data, "against_down", limits),
            bound=_optional(data, "bound", limits),
        )


def _optional(
    data: dict[str, Any], key: str, limits: EntailmentLimits
) -> Fraction | None:
    """Read a rational the claim may carry; ``None`` when the document lacks it."""
    return decode_rational(data[key], limits) if key in data else None
