"""Exact goal syntax mirroring ``forseti-lean``'s ``Goal``, and its encoding.

A goal is an ordered context of coordinate names and two quantifier-free
formulas over it: ``P ⊨ Q``. The values here copy the Lean constructors one for
one (``Gimle.Forseti.Syntax.Formula`` over ``Gimle.Asgard.Polynomial.Expr``) so
a goal crosses to Lean by rendering, never by parsing text. Coefficients are
``fractions.Fraction``; coordinates are indices into the context.

The JSON encoding ``gimle.forseti.entailment-goal/v1`` uses explicit
constructor tags, ``[numerator, denominator]`` integer pairs in lowest terms and
coordinate indices. Decoding is total and bounded: anything malformed or over a
limit is refused with `GoalError` before a value is built.

Nothing here decides an entailment. A goal is what a checker is asked about.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from math import gcd
from typing import Any, Union

from .limits import CEILINGS, EntailmentLimits

GOAL_SCHEMA = "gimle.forseti.entailment-goal/v1"
#: No goal value, however built, is walked past this many nodes.
NODE_CEILING = CEILINGS["max_nodes"]

#: Names are rendered into Lean string literals, so they are restricted to a
#: charset that cannot close the literal or form any other syntax.
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class GoalError(ValueError):
    """A goal, formula, polynomial or rational encoding was rejected."""


class Relation(str, Enum):
    """The comparisons of ``Gimle.Forseti.Syntax.Relation``, each against zero."""

    EQ = "eq"
    LE = "le"
    LT = "lt"
    GE = "ge"
    GT = "gt"


@dataclass(frozen=True, slots=True)
class Var:
    """The coordinate at ``index`` in the goal's context."""

    index: int


@dataclass(frozen=True, slots=True)
class Const:
    """An exact rational constant."""

    value: Fraction


@dataclass(frozen=True, slots=True)
class Add:
    """``left + right``."""

    left: Poly
    right: Poly


@dataclass(frozen=True, slots=True)
class Mul:
    """``left * right``."""

    left: Poly
    right: Poly


@dataclass(frozen=True, slots=True)
class Neg:
    """``-argument``."""

    argument: Poly


Poly = Union[Var, Const, Add, Mul, Neg]


@dataclass(frozen=True, slots=True)
class Tru:
    """The formula that always holds."""


@dataclass(frozen=True, slots=True)
class Fls:
    """The formula that never holds."""


@dataclass(frozen=True, slots=True)
class Atom:
    """``polynomial ⋈ 0`` for one relation ``⋈``."""

    relation: Relation
    polynomial: Poly


@dataclass(frozen=True, slots=True)
class And:
    """Conjunction."""

    left: Formula
    right: Formula


@dataclass(frozen=True, slots=True)
class Or:
    """Disjunction."""

    left: Formula
    right: Formula


@dataclass(frozen=True, slots=True)
class Not:
    """Negation."""

    argument: Formula


Formula = Union[Tru, Fls, Atom, And, Or, Not]


@dataclass(frozen=True, slots=True)
class Context:
    """An ordered context of distinct coordinate names, as in Lean's ``Context``."""

    names: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject an empty context, a malformed name or a repeated name."""
        if type(self.names) is not tuple or not self.names:
            raise GoalError("a context is a non-empty tuple of names")
        for name in self.names:
            if type(name) is not str or not _NAME.fullmatch(name):
                raise GoalError(f"invalid coordinate name {name!r}")
        if len(set(self.names)) != len(self.names):
            raise GoalError("coordinate names must be distinct")

    @property
    def dimension(self) -> int:
        """Count the coordinates: one per name."""
        return len(self.names)

    def var(self, name: str) -> Var:
        """Return the polynomial variable with this name."""
        if name not in self.names:
            raise GoalError(f"unknown coordinate name {name!r}")
        return Var(self.names.index(name))


@dataclass(frozen=True, slots=True)
class Goal:
    """``antecedent ⊨ consequent`` over one ordered context."""

    context: Context
    antecedent: Formula
    consequent: Formula

    def __post_init__(self) -> None:
        """Check the formulas' node types and that coordinates index the context.

        The walk is bounded by the hard node ceiling, so a goal built with
        shared subterms (a DAG whose tree is exponentially large) is refused
        rather than walked.
        """
        if type(self.context) is not Context:
            raise GoalError("context must be a Context")
        visited = 0
        for formula in (self.antecedent, self.consequent):
            for node in walk(formula, NODE_CEILING - visited):
                visited += 1
                if type(node) is Var and not 0 <= node.index < self.context.dimension:
                    raise GoalError(f"coordinate {node.index} outside the context")


def walk(node: Formula | Poly, ceiling: int = 0) -> Iterator[Formula | Poly]:
    """Yield every node of a formula or polynomial, parents first, iteratively.

    Args:
        node: The root.
        ceiling: Refuse the tree once more than this many nodes are visited;
            ``NODE_CEILING`` when zero.
    """
    ceiling = ceiling or NODE_CEILING
    pending: list[Formula | Poly] = [node]
    count = 0
    while pending:
        current = pending.pop()
        count += 1
        if count > ceiling:
            raise GoalError("too many syntax nodes")
        if type(current) in (Add, Mul, And, Or):
            pending.extend((current.right, current.left))  # type: ignore[union-attr]
        elif type(current) in (Neg, Not):
            pending.append(current.argument)  # type: ignore[union-attr]
        elif type(current) is Atom:
            if type(current.relation) is not Relation:
                raise GoalError("an atom's relation is a Relation")
            pending.append(current.polynomial)
        elif type(current) not in (Var, Const, Tru, Fls):
            raise GoalError(f"not a formula or polynomial node: {current!r}")
        if type(current) is Var and (
            type(current.index) is not int or current.index < 0
        ):
            raise GoalError("a coordinate is a non-negative integer")
        if type(current) is Const and type(current.value) is not Fraction:
            raise GoalError("a constant is a fractions.Fraction")
        yield current


# --- limits ---------------------------------------------------------------


def check_goal_limits(goal: Goal, limits: EntailmentLimits) -> None:
    """Refuse a goal over the variable, node, depth or coefficient limits."""
    if goal.context.dimension > limits.max_variables:
        raise GoalError("too many variables")
    nodes = 0
    for formula in (goal.antecedent, goal.consequent):
        pending: list[tuple[Formula | Poly, int]] = [(formula, 1)]
        while pending:
            current, depth = pending.pop()
            nodes += 1
            if nodes > limits.max_nodes:
                raise GoalError("too many syntax nodes")
            if depth > limits.max_depth:
                raise GoalError("syntax nested too deeply")
            if type(current) is Const:
                check_rational_bits(current.value, limits)
            for child in _children(current):
                pending.append((child, depth + 1))


def check_rational_bits(value: Fraction, limits: EntailmentLimits) -> None:
    """Refuse a rational whose numerator or denominator is too large."""
    if (
        value.numerator.bit_length() > limits.max_coefficient_bits
        or value.denominator.bit_length() > limits.max_coefficient_bits
    ):
        raise GoalError("coefficient bit size limit")


def _children(node: Formula | Poly) -> tuple[Formula | Poly, ...]:
    if type(node) in (Add, Mul, And, Or):
        return (node.left, node.right)  # type: ignore[union-attr]
    if type(node) in (Neg, Not):
        return (node.argument,)  # type: ignore[union-attr]
    if type(node) is Atom:
        return (node.polynomial,)
    return ()


# --- encoding -------------------------------------------------------------


def encode_rational(value: Fraction) -> list[int]:
    """``[numerator, denominator]``, in lowest terms with a positive denominator."""
    return [value.numerator, value.denominator]


def decode_rational(data: Any, limits: EntailmentLimits) -> Fraction:
    """Read a rational pair, refusing anything not already canonical."""
    if (
        type(data) is not list
        or len(data) != 2
        or any(type(part) is not int for part in data)
    ):
        raise GoalError("a rational is a [numerator, denominator] integer pair")
    numerator, denominator = data
    if denominator <= 0 or gcd(numerator, denominator) != 1:
        raise GoalError("a rational must be in lowest terms, denominator > 0")
    value = Fraction(numerator, denominator)
    check_rational_bits(value, limits)
    return value


def encode_poly(poly: Poly) -> list[Any]:
    """Encode a polynomial as tagged JSON."""
    if type(poly) is Var:
        return ["var", poly.index]
    if type(poly) is Const:
        return ["const", encode_rational(poly.value)]
    if type(poly) is Add:
        return ["add", encode_poly(poly.left), encode_poly(poly.right)]
    if type(poly) is Mul:
        return ["mul", encode_poly(poly.left), encode_poly(poly.right)]
    if type(poly) is Neg:
        return ["neg", encode_poly(poly.argument)]
    raise GoalError(f"not a polynomial: {poly!r}")


def encode_formula(formula: Formula) -> list[Any]:
    """Encode a formula as tagged JSON."""
    if type(formula) is Tru:
        return ["true"]
    if type(formula) is Fls:
        return ["false"]
    if type(formula) is Atom:
        return ["atom", formula.relation.value, encode_poly(formula.polynomial)]
    if type(formula) is And:
        return ["and", encode_formula(formula.left), encode_formula(formula.right)]
    if type(formula) is Or:
        return ["or", encode_formula(formula.left), encode_formula(formula.right)]
    if type(formula) is Not:
        return ["not", encode_formula(formula.argument)]
    raise GoalError(f"not a formula: {formula!r}")


def encode_goal(goal: Goal) -> dict[str, Any]:
    """Encode a goal as its versioned JSON document."""
    return {
        "schema": GOAL_SCHEMA,
        "context": list(goal.context.names),
        "antecedent": encode_formula(goal.antecedent),
        "consequent": encode_formula(goal.consequent),
    }


class DecodeBudget:
    """Counts nodes and depth while decoding, before anything is built.

    One budget spans everything decoded against it, such as every polynomial
    of a certificate.
    """

    def __init__(self, limits: EntailmentLimits, dimension: int) -> None:
        """Start an empty count for a context of ``dimension`` coordinates."""
        self.limits = limits
        self.dimension = dimension
        self.nodes = 0

    def enter(self, depth: int) -> None:
        """Count one node at ``depth``, refusing past the node or depth limit."""
        self.nodes += 1
        if self.nodes > self.limits.max_nodes:
            raise GoalError("too many syntax nodes")
        if depth > self.limits.max_depth:
            raise GoalError("syntax nested too deeply")


def _tagged(data: Any, arities: dict[str, int]) -> tuple[str, list[Any]]:
    if type(data) is not list or not data or type(data[0]) is not str:
        raise GoalError("a node is a tagged list")
    tag, arguments = data[0], data[1:]
    if tag not in arities or len(arguments) != arities[tag]:
        raise GoalError(f"unknown node tag or arity: {tag!r}")
    return tag, arguments


_POLY_ARITIES = {"var": 1, "const": 1, "add": 2, "mul": 2, "neg": 1}
_FORMULA_ARITIES = {"true": 0, "false": 0, "atom": 2, "and": 2, "or": 2, "not": 1}


def decode_poly(data: Any, budget: DecodeBudget, depth: int = 1) -> Poly:
    """Read a tagged polynomial within the budget."""
    budget.enter(depth)
    tag, arguments = _tagged(data, _POLY_ARITIES)
    if tag == "var":
        index = arguments[0]
        if type(index) is not int or not 0 <= index < budget.dimension:
            raise GoalError("a coordinate must index the context")
        return Var(index)
    if tag == "const":
        return Const(decode_rational(arguments[0], budget.limits))
    if tag == "neg":
        return Neg(decode_poly(arguments[0], budget, depth + 1))
    left = decode_poly(arguments[0], budget, depth + 1)
    right = decode_poly(arguments[1], budget, depth + 1)
    return Add(left, right) if tag == "add" else Mul(left, right)


def decode_formula(data: Any, budget: DecodeBudget, depth: int = 1) -> Formula:
    """Read a tagged formula within the budget."""
    budget.enter(depth)
    tag, arguments = _tagged(data, _FORMULA_ARITIES)
    if tag == "true":
        return Tru()
    if tag == "false":
        return Fls()
    if tag == "atom":
        relation = arguments[0]
        if type(relation) is not str or relation not in {r.value for r in Relation}:
            raise GoalError(f"unknown relation {relation!r}")
        return Atom(Relation(relation), decode_poly(arguments[1], budget, depth + 1))
    if tag == "not":
        return Not(decode_formula(arguments[0], budget, depth + 1))
    left = decode_formula(arguments[0], budget, depth + 1)
    right = decode_formula(arguments[1], budget, depth + 1)
    return And(left, right) if tag == "and" else Or(left, right)


def decode_goal(data: Any, limits: EntailmentLimits) -> Goal:
    """Read a versioned goal document, refusing anything malformed or too large."""
    if type(data) is not dict or set(data) != {
        "schema",
        "context",
        "antecedent",
        "consequent",
    }:
        raise GoalError("a goal has exactly schema, context and both formulas")
    if data["schema"] != GOAL_SCHEMA:
        raise GoalError("unsupported goal schema")
    names = data["context"]
    if type(names) is not list:
        raise GoalError("a context is a list of names")
    if len(names) > limits.max_variables:
        raise GoalError("too many variables")
    context = Context(tuple(names))
    budget = DecodeBudget(limits, context.dimension)
    return Goal(
        context,
        decode_formula(data["antecedent"], budget),
        decode_formula(data["consequent"], budget),
    )
