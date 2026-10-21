"""Closed goals about a stepped stochastic model, and their encoding.

A stepped model starts from an exact initial vector at a start time, advances
by a fixed step, and is hit each step by a shock of bounded variance. A goal
binds the model by key, that initial vector, the start, the step, the law the
shocks are known to follow, and one claim about an observed quantity:

* ``horizon``: within ``horizon`` steps the observation breaches ``barrier``
  with probability at most ``bound``;
* ``fixed-date``: the same, at every mark up to ``horizon`` steps;
* ``expected``: at step ``horizon`` the expected observation is at most
  ``bound``. It has no barrier.

``gimle.forseti.stepped-goal/v1`` is a strict JSON encoding of it, one key per
field; ``barrier`` is present exactly when the claim has one, so each goal has
one encoding. Decoding checks shape and bounds only; whether the model, law and
claim are *supported* is the decider's question, answered with ``UNKNOWN``
rather than a decoding error.
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

SCHEMA = "gimle.forseti.stepped-goal/v1"
LAWS = ("two-moment", "sub-gaussian")
CLAIMS = ("horizon", "fixed-date", "expected")
#: Most entries a goal's initial vector may name; a sanity cap on the model size.
MAX_STATES = 32
#: Model keys are stable IDs and observations are names, as the goal text reads
#: them, so that every goal has a text form that reads back to it.
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_KEYS = frozenset(
    {
        "schema",
        "model",
        "initial",
        "start",
        "step",
        "law",
        "variance",
        "claim",
        "horizon",
        "observation",
        "bound",
    }
)
_DEFAULT_LIMITS = EntailmentLimits()


@dataclass(frozen=True)
class SteppedGoal:
    """A closed goal about a stepped stochastic model.

    Attributes:
        model: The model key.
        initial: The initial vector, one exact value per state.
        start: The start time.
        step: The size of one step.
        law: The shock law: ``two-moment`` (only its variance is known) or
            ``sub-gaussian``.
        variance: The shock variance (or variance proxy); not negative.
        claim: ``horizon``, ``fixed-date`` or ``expected``.
        horizon: The number of steps the claim ranges over. For ``expected``
            it is the step the expectation is taken at.
        observation: The observed quantity's name.
        barrier: The level whose breach is bounded; ``None`` for ``expected``
            and present for the other claims.
        bound: The claimed upper bound on the breach probability, or on the
            expected observation; not negative.
    """

    SCHEMA: ClassVar[str] = SCHEMA

    model: str
    initial: tuple[Fraction, ...]
    start: Fraction
    step: Fraction
    law: Literal["two-moment", "sub-gaussian"]
    variance: Fraction
    claim: Literal["horizon", "fixed-date", "expected"]
    horizon: int
    observation: str
    barrier: Fraction | None
    bound: Fraction

    def __post_init__(self) -> None:
        """Check tags, names and exact values; refuse a negative variance or bound."""
        if self.law not in LAWS:
            raise GoalError(f"unknown law {self.law!r}")
        if self.claim not in CLAIMS:
            raise GoalError(f"unknown claim {self.claim!r}")
        if type(self.model) is not str or not _ID.fullmatch(self.model):
            raise GoalError(f"invalid model key {self.model!r}")
        if type(self.observation) is not str or not _NAME.fullmatch(self.observation):
            raise GoalError(f"invalid observation name {self.observation!r}")
        if type(self.initial) is not tuple or not self.initial:
            raise GoalError("the initial vector names at least one value")
        if len(self.initial) > MAX_STATES:
            raise GoalError("too many initial values")
        for value in self.initial:
            if type(value) is not Fraction:
                raise GoalError("initial values are fractions.Fraction")
        for label, number in (
            ("start", self.start),
            ("step", self.step),
            ("variance", self.variance),
            ("bound", self.bound),
        ):
            if type(number) is not Fraction:
                raise GoalError(f"the {label} is a fractions.Fraction")
        if type(self.horizon) is not int or self.horizon < 0:
            raise GoalError("the horizon is a whole number of steps, not negative")
        if self.claim == "expected":
            if self.barrier is not None:
                raise GoalError("an expected claim has no barrier")
        elif type(self.barrier) is not Fraction:
            raise GoalError(f"a {self.claim} claim has a barrier, a Fraction")
        # A negative variance describes no shock, and a negative bound is
        # below every probability and every energy, so the claim would be
        # refuted or vacuous by construction.
        if self.variance < 0:
            raise GoalError(f"the variance must not be negative, got {self.variance}")
        if self.bound < 0:
            raise GoalError(f"the bound must not be negative, got {self.bound}")

    def to_json(self) -> dict[str, Any]:
        """Encode the goal as its versioned JSON document."""
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "model": self.model,
            "initial": [encode_rational(value) for value in self.initial],
            "start": encode_rational(self.start),
            "step": encode_rational(self.step),
            "law": self.law,
            "variance": encode_rational(self.variance),
            "claim": self.claim,
            "horizon": self.horizon,
            "observation": self.observation,
            "bound": encode_rational(self.bound),
        }
        if self.barrier is not None:
            document["barrier"] = encode_rational(self.barrier)
        return document

    @classmethod
    def from_json(
        cls, data: Any, limits: EntailmentLimits = _DEFAULT_LIMITS
    ) -> SteppedGoal:
        """Read a versioned goal document, refusing anything malformed.

        Raises:
            GoalError: The document is not exactly a v1 stepped goal.
        """
        if type(data) is not dict or set(data) - {"barrier"} != _KEYS:
            raise GoalError("a stepped goal has exactly the v1 fields")
        if data["schema"] != SCHEMA:
            raise GoalError("unsupported stepped goal schema")
        for label in ("model", "law", "claim", "observation"):
            if type(data[label]) is not str:
                raise GoalError(f"{label} is text")
        entries = data["initial"]
        if type(entries) is not list or len(entries) > MAX_STATES:
            raise GoalError("the initial vector is a bounded list")
        barrier = None
        if "barrier" in data:
            barrier = decode_rational(data["barrier"], limits)
        return cls(
            model=data["model"],
            initial=tuple(decode_rational(entry, limits) for entry in entries),
            start=decode_rational(data["start"], limits),
            step=decode_rational(data["step"], limits),
            law=data["law"],
            variance=decode_rational(data["variance"], limits),
            claim=data["claim"],
            horizon=data["horizon"],
            observation=data["observation"],
            barrier=barrier,
            bound=decode_rational(data["bound"], limits),
        )
