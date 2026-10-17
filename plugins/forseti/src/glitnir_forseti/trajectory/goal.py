"""Closed root goals about a Lean-compiled continuous model, and their encoding.

A goal binds the original circuit by model key, its interpretation, the
obligation (a well-posed contract), the time axis and start, the initial
state by initial-port ID, the observed output port, a quantifier over the
forward domain, and a postcondition: a quantifier-free formula over the one
observed coordinate, in task 111's exact syntax. A goal about a driven model
also binds its driver precondition: each bounded driver port with a bound
``B``, read as "the drivers are admitted and ``|u| ≤ B`` on the domain".

``gimle.forseti.trajectory-goal/v1`` is a strict JSON encoding of it. The
``drivers`` field is present exactly when the goal bounds a driver, so a goal
about an undriven model encodes as it always has. Decoding
checks shape and bounds only; whether the model, interpretation, obligation
and quantifier are *supported* is the service's question, answered with
``UNKNOWN`` rather than a decoding error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import (
    Context,
    DecodeBudget,
    Formula,
    Goal,
    GoalError,
    Tru,
)
from glitnir_forseti.entailment.syntax import check_goal_limits as check_formula_limits
from glitnir_forseti.entailment.syntax import (
    check_rational_bits,
    decode_formula,
    decode_rational,
    encode_formula,
    encode_rational,
    walk,
)

GOAL_SCHEMA = "gimle.forseti.trajectory-goal/v1"
#: The interpretations a goal may name. Only the first is supported; the rest
#: decode so that a request for them ends ``UNKNOWN`` with a reason.
INTERPRETATIONS = ("continuous-real", "stochastic-ito", "formal-stream")
OBLIGATIONS = ("contract", "holds")
QUANTIFIERS = ("always", "at-start", "eventually")
#: Most states a goal's initial list, or a witness's, may name. The formula is
#: over one observed coordinate, so the entailment limit on formula variables
#: (8) is the wrong bound here; this is a sanity cap on the model size. The
#: largest registered model has 12 states.
MAX_STATES = 32
#: Stable IDs are rendered into Lean string literals: no quote, no backslash.
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_KEYS = frozenset(
    {
        "schema",
        "model",
        "interpretation",
        "obligation",
        "time",
        "initial",
        "observation",
        "quantifier",
        "post",
    }
)


@dataclass(frozen=True, slots=True)
class TrajectoryGoal:
    """A closed root goal.

    Attributes:
        model: The model key.
        interpretation: How the circuit is read (``continuous-real``).
        obligation: ``contract`` (existence, uniqueness and the
            postcondition) or ``holds`` (partial correctness only).
        axis: The time axis's stable ID.
        start: The start of the forward time domain.
        initial: ``(initial-port ID, value)`` pairs, sorted by ID.
        observation: The observed output port's stable ID.
        name: The observation's coordinate name in ``post``.
        quantifier: Over which times ``post`` must hold (``always``).
        post: The postcondition, over the one coordinate ``name``.
        drivers: ``(driver port ID, bound)`` pairs, sorted by ID: the drivers
            are admitted and each named one stays in ``[-bound, bound]``.
            Empty for a goal that states no driver precondition.
    """

    model: str
    interpretation: str
    obligation: str
    axis: str
    start: Fraction
    initial: tuple[tuple[str, Fraction], ...]
    observation: str
    name: str
    quantifier: str
    post: Formula
    drivers: tuple[tuple[str, Fraction], ...] = ()

    def __post_init__(self) -> None:
        """Check tags, IDs, exact values and the postcondition's coordinates."""
        for label, tag, allowed in (
            ("interpretation", self.interpretation, INTERPRETATIONS),
            ("obligation", self.obligation, OBLIGATIONS),
            ("quantifier", self.quantifier, QUANTIFIERS),
        ):
            if tag not in allowed:
                raise GoalError(f"unknown {label} {tag!r}")
        for identifier in (self.model, self.axis, self.observation):
            _check_id(identifier)
        Context((self.name,))
        if type(self.start) is not Fraction:
            raise GoalError("the start time is a fractions.Fraction")
        if type(self.initial) is not tuple or not self.initial:
            raise GoalError("the initial state names at least one port")
        ids = [identifier for identifier, _ in self.initial]
        if ids != sorted(set(ids)):
            raise GoalError("initial ports are distinct and sorted by ID")
        for identifier, value in self.initial:
            _check_id(identifier)
            if type(value) is not Fraction:
                raise GoalError("initial values are fractions.Fraction")
        if type(self.drivers) is not tuple:
            raise GoalError("the driver bounds are a tuple")
        ports = [port for port, _ in self.drivers]
        if ports != sorted(set(ports)):
            raise GoalError("bounded drivers are distinct and sorted by ID")
        for port, bound in self.drivers:
            _check_id(port)
            if type(bound) is not Fraction:
                raise GoalError("driver bounds are fractions.Fraction")
            # No driver satisfies a negative bound, and a contract whose
            # precondition nothing satisfies holds vacuously.
            if bound < 0:
                raise GoalError("a driver bound is nonnegative")
        for node in walk(self.post):
            if getattr(node, "index", 0) != 0:
                raise GoalError("the postcondition has one coordinate")

    @property
    def context(self) -> Context:
        """The postcondition's one-coordinate context."""
        return Context((self.name,))


def _check_id(identifier: object) -> None:
    """Refuse an ID that could not be rendered safely into Lean."""
    if type(identifier) is not str or not _ID.fullmatch(identifier):
        raise GoalError(f"invalid stable ID {identifier!r}")


def check_goal_limits(goal: TrajectoryGoal, limits: EntailmentLimits) -> None:
    """Refuse a goal over the node, depth, variable or coefficient limits."""
    check_formula_limits(Goal(goal.context, Tru(), goal.post), limits)
    if len(goal.initial) > MAX_STATES:
        raise GoalError("too many initial ports")
    check_rational_bits(goal.start, limits)
    for _, value in goal.initial:
        check_rational_bits(value, limits)
    if len(goal.drivers) > limits.max_variables:
        raise GoalError("too many bounded drivers")
    for _, bound in goal.drivers:
        check_rational_bits(bound, limits)


def encode_goal(goal: TrajectoryGoal) -> dict[str, Any]:
    """Encode a goal as its versioned JSON document."""
    document: dict[str, Any] = {
        "schema": GOAL_SCHEMA,
        "model": goal.model,
        "interpretation": goal.interpretation,
        "obligation": goal.obligation,
        "time": {"axis": goal.axis, "start": encode_rational(goal.start)},
        "initial": [[i, encode_rational(v)] for i, v in goal.initial],
        "observation": {"port": goal.observation, "name": goal.name},
        "quantifier": goal.quantifier,
        "post": encode_formula(goal.post),
    }
    if goal.drivers:
        document["drivers"] = [[p, encode_rational(b)] for p, b in goal.drivers]
    return document


def decode_goal(data: Any, limits: EntailmentLimits) -> TrajectoryGoal:
    """Read a versioned goal document, refusing anything malformed or too large."""
    if type(data) is not dict or set(data) - {"drivers"} != _KEYS:
        raise GoalError("a trajectory goal has exactly the v1 fields")
    if data["schema"] != GOAL_SCHEMA:
        raise GoalError("unsupported trajectory goal schema")
    time = data["time"]
    if type(time) is not dict or set(time) != {"axis", "start"}:
        raise GoalError("time is an axis and a start")
    observation = data["observation"]
    if type(observation) is not dict or set(observation) != {"port", "name"}:
        raise GoalError("the observation is a port and a name")
    entries = data["initial"]
    if type(entries) is not list or len(entries) > MAX_STATES:
        raise GoalError("the initial state is a bounded list")
    initial = []
    for entry in entries:
        if type(entry) is not list or len(entry) != 2:
            raise GoalError("an initial entry is [port ID, rational]")
        _check_id(entry[0])
        initial.append((entry[0], decode_rational(entry[1], limits)))
    drivers = []
    if "drivers" in data:
        bounds = data["drivers"]
        # Present means non-empty, so each goal has one encoding.
        if type(bounds) is not list or not bounds or len(bounds) > limits.max_variables:
            raise GoalError("the driver bounds are a non-empty bounded list")
        for bound in bounds:
            if type(bound) is not list or len(bound) != 2:
                raise GoalError("a driver bound is [port ID, rational]")
            _check_id(bound[0])
            drivers.append((bound[0], decode_rational(bound[1], limits)))
    for label in ("model", "interpretation", "obligation", "quantifier"):
        if type(data[label]) is not str:
            raise GoalError(f"{label} is text")
    return TrajectoryGoal(
        model=data["model"],
        interpretation=data["interpretation"],
        obligation=data["obligation"],
        axis=time["axis"],
        start=decode_rational(time["start"], limits),
        initial=tuple(initial),
        observation=observation["port"],
        name=observation["name"],
        quantifier=data["quantifier"],
        post=decode_formula(data["post"], DecodeBudget(limits, 1)),
        drivers=tuple(drivers),
    )
