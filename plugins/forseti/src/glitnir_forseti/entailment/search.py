"""The two untrusted producers: a counterexample and a certificate.

**Refute.** Z3 looks for a model of ``P ∧ ¬Q`` over the reals. A rational
model becomes a `Witness`. An irrational one is an ``unsupported-witness``
that carries the model exactly, never rounded, as `LaneOutcome.roots`; the
entailment service turns it into an algebraic witness (`algebraic_search`). Z3
``unsat`` means only that there is no counterexample to hand to Lean, which is
not evidence the goal holds.

**Certificate.** For conjunctions of non-strict inequalities, the identity

    −q_j = Σ_k w_0k · g_k² + Σ_i Σ_k w_ik · g_k² · (−p_i),   w ≥ 0

is set up over a fixed square dictionary ``g``. The polynomials are fixed
before Z3 runs, so the unknown weights enter linearly: expanding both sides
exactly gives one linear equation per monomial, and Z3 returns exact
nonnegative rationals. ``unsat`` means this dictionary cannot produce a
certificate, never that the goal is false.

The default dictionary is ``{1, x_0, …, x_(n-1)}``, read from the context.
When it is exhausted the lane widens it to the DSOS dictionary: the default
plus every pairwise sum and difference ``g_i ± g_j``. Nonnegative combinations
of their squares are every diagonally dominant Gram matrix over the default,
so the weights still enter linearly and a certificate found this way is an
ordinary one, replayed unchanged. The widening has ``(n+1)²`` squares, so under
the default dictionary limit it runs for at most three coordinates; it takes
at most half the lane's remaining time, and never runs for a dictionary the
caller names.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .algebraic import AlgebraicRoot
from .algebraic_search import from_model
from .evidence import (
    Certificate,
    EvidenceError,
    PsatzCertificate,
    Row,
    SquareSum,
    Witness,
    augment,
    nonstrict,
    witness_for,
)
from .limits import EntailmentLimits
from .polynomial import (
    ExpansionLimit,
    Sparse,
    check_expansion,
    degree_bound,
    expand,
    multiply,
)
from .solver import WorkerStop, run_worker
from .syntax import (
    Add,
    Atom,
    Const,
    Context,
    DecodeBudget,
    Goal,
    GoalError,
    Neg,
    Poly,
    Relation,
    Var,
    check_goal_limits,
    check_rational_bits,
    decode_poly,
    encode_goal,
)


@dataclass(frozen=True, slots=True)
class LaneOutcome:
    """What one producer lane ended with, before any Lean check.

    Attributes:
        outcome: ``found``, ``none`` (the solver found no evidence),
            ``unsupported-fragment``, ``unsupported-witness``,
            ``resource-limit``, ``timeout``, ``cancelled``,
            ``z3-unavailable`` or ``z3-unknown``.
        detail: The backend's own diagnostic, retained verbatim but bounded.
        evidence: The candidate evidence when ``found``.
    """

    outcome: str
    detail: str = ""
    evidence: Certificate | PsatzCertificate | Witness | None = None
    #: For ``unsupported-witness``: the exact algebraic model, if one was read.
    roots: tuple[AlgebraicRoot, ...] = ()


def default_dictionary(context: Context) -> tuple[Poly, ...]:
    """Return the dictionary ``{1, x_0, …, x_(n-1)}``."""
    return (Const(Fraction(1)),) + tuple(Var(i) for i in range(context.dimension))


def dsos_dictionary(context: Context) -> tuple[Poly, ...]:
    """Return the default dictionary with every pairwise sum and difference."""
    base = default_dictionary(context)
    return base + tuple(
        combined
        for i, first in enumerate(base)
        for second in base[i + 1 :]
        for combined in (Add(first, second), Add(first, Neg(second)))
    )


#: The most of a certificate lane's remaining time the DSOS widening may use,
#: so a widening that cannot finish never turns a fast exhaustion into a
#: timeout, or starves what the caller runs next.
WIDENING_SHARE = 0.5

_STOPS = {
    "timeout": "timeout",
    "cancelled": "cancelled",
    "output": "resource-limit",
    "failed": "z3-unknown",
}


def _stop(stop: WorkerStop) -> LaneOutcome:
    """Name a worker that did not answer; an oversized answer is a limit."""
    return LaneOutcome(_STOPS[stop.cause], f"worker {stop.cause}: {stop.detail}"[:500])


def _backend(answer: dict[str, Any]) -> LaneOutcome | None:
    """Name an answer that carries no candidate, or None for one that may."""
    result = answer["result"]
    detail = str(answer.get("detail", ""))[:500]
    if result == "unavailable":
        return LaneOutcome("z3-unavailable", detail)
    if result == "unknown" and detail in ("timeout", "canceled"):
        return LaneOutcome("timeout", f"z3 unknown: {detail}")
    if result in ("unknown", "error"):
        return LaneOutcome("z3-unknown", f"z3 {result}: {detail}")
    return None


def refute(
    goal: Goal,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None = None,
) -> LaneOutcome:
    """Ask Z3 for a rational point where ``P`` holds and ``Q`` fails."""
    answer = run_worker(
        {"op": "refute", "goal": encode_goal(goal), "limits": limits.as_dict()},
        deadline,
        cancel,
    )
    if isinstance(answer, WorkerStop):
        return _stop(answer)
    reported = _backend(answer)
    if reported is not None:
        return reported
    if answer["result"] == "unsat":
        return LaneOutcome("none", "z3 unsat: no counterexample exists to check")
    if answer["result"] == "algebraic":
        detail = str(answer.get("detail", ""))[:500]
        try:
            roots = from_model(answer.get("model"), goal.context.dimension, limits)
        except (EvidenceError, GoalError) as error:
            return LaneOutcome(
                "unsupported-witness", f"{detail}; unusable model: {error}"
            )
        return LaneOutcome("unsupported-witness", detail, roots=roots)
    if answer["result"] != "sat":
        return LaneOutcome("z3-unknown", "unexpected answer")
    try:
        point = _rationals(answer.get("point"), goal.context.dimension, limits)
    except (GoalError, ValueError) as error:
        return LaneOutcome("z3-unknown", f"unusable model: {error}")
    return LaneOutcome("found", "z3 sat", witness_for(goal.context, tuple(point)))


def certify(
    goal: Goal,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None = None,
    dictionary: tuple[Poly, ...] | None = None,
) -> LaneOutcome:
    """Search for a certificate: the named dictionary, or the default then DSOS.

    Without ``dictionary`` the default is searched first. Only when it is
    exhausted is the DSOS widening searched, with at most `WIDENING_SHARE` of
    the time left, and only when it fits the dictionary limit. The widening is
    optional: unless it finds a certificate or the request is cancelled, the
    lane stays ``none`` — the default dictionary was exhausted — and the detail
    says why the widening did not help.
    """
    antecedent = nonstrict(goal.antecedent)
    consequent = nonstrict(goal.consequent)
    if antecedent is None or consequent is None:
        return LaneOutcome(
            "unsupported-fragment",
            "certificates need conjunctions of non-strict inequalities on both sides",
        )
    if dictionary is not None:
        return _search(
            goal.context, antecedent, consequent, limits, deadline, cancel, dictionary
        )
    return _widening(goal.context, antecedent, consequent, limits, deadline, cancel)


def _widening(
    context: Context,
    antecedent: list[Poly],
    consequent: list[Poly],
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None,
) -> LaneOutcome:
    """Search the default dictionary, then its DSOS widening (`certify`)."""
    first = _search(
        context,
        antecedent,
        consequent,
        limits,
        deadline,
        cancel,
        default_dictionary(context),
    )
    if first.outcome != "none":
        return first
    widened = dsos_dictionary(context)
    label = f"its {len(widened)}-square dsos widening"
    if len(widened) > limits.max_dictionary:
        return LaneOutcome("none", f"{first.detail}; {label} exceeds the limits")
    now = time.monotonic()
    share = now + WIDENING_SHARE * max(0.0, deadline - now)
    second = _search(context, antecedent, consequent, limits, share, cancel, widened)
    if second.outcome == "found":
        return LaneOutcome("found", f"z3 sat with {label}", second.evidence)
    if second.outcome == "cancelled":
        return LaneOutcome("cancelled", f"{first.detail}; {label}: {second.detail}")
    if second.outcome == "none":
        return LaneOutcome("none", f"{first.detail}; so is {label}")
    return LaneOutcome(
        "none", f"{first.detail}; {label} ended {second.outcome}: {second.detail}"
    )


def _search(
    context: Context,
    antecedent: list[Poly],
    consequent: list[Poly],
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None,
    squares: tuple[Poly, ...],
) -> LaneOutcome:
    """Search one fixed square dictionary for a certificate, row by row.

    It works on constraint lists, so a caller can pose an augmented antecedent
    without building a formula for it.
    """
    try:
        _check_dictionary(context, squares, limits)
        slots = 1 + len(antecedent)
        if len(consequent) * slots * len(squares) > limits.max_weights:
            raise ExpansionLimit("too many weights for the solver")
        dimension = context.dimension
        # Every product is bounded before any expansion allocates.
        for g in squares:
            check_expansion(dimension, 2 * degree_bound(g), limits)
            for p in antecedent:
                check_expansion(
                    dimension, 2 * degree_bound(g) + degree_bound(p), limits
                )
        for q in consequent:
            check_expansion(dimension, degree_bound(q), limits)
        square_forms = [
            multiply(g, g) for g in (expand(p, dimension, limits) for p in squares)
        ]
        negated = [expand(Neg(p), dimension, limits) for p in antecedent]
        columns: list[Sparse] = list(square_forms)
        for factor in negated:
            for square in square_forms:
                # Expansion is CPU-bound Fraction arithmetic; stay responsive.
                if cancel is not None and cancel.is_set():
                    return LaneOutcome("cancelled", "during expansion")
                if time.monotonic() >= deadline:
                    return LaneOutcome("timeout", "during expansion")
                columns.append(multiply(square, factor))
        targets = [expand(Neg(q), dimension, limits) for q in consequent]
    except ExpansionLimit as error:
        return LaneOutcome("resource-limit", str(error))
    rows = []
    for target in targets:
        answer = run_worker(
            {
                "op": "weights",
                "columns": [_encode_sparse(c) for c in columns],
                "target": _encode_sparse(target),
            },
            deadline,
            cancel,
        )
        if isinstance(answer, WorkerStop):
            return _stop(answer)
        reported = _backend(answer)
        if reported is not None:
            return reported
        if answer["result"] == "unsat":
            return LaneOutcome(
                "none", f"z3 unsat: the {len(squares)}-square dictionary is exhausted"
            )
        if answer["result"] != "sat":
            return LaneOutcome("z3-unknown", "unexpected answer")
        try:
            weights = _rationals(answer.get("weights"), len(columns), limits)
        except (GoalError, ValueError) as error:
            return LaneOutcome("z3-unknown", f"unusable weights: {error}")
        rows.append(_row(weights, squares, len(antecedent)))
    return LaneOutcome("found", "z3 sat", Certificate(tuple(rows)))


def certify_with_products(
    goal: Goal,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None = None,
    dictionary: tuple[Poly, ...] | None = None,
) -> LaneOutcome:
    """Search `certify`'s dictionaries, then constraint products.

    The products are checked by ``Gimle.Forseti.Syntax.Positivstellensatz``.

    Only when `certify` exhausts the default and DSOS dictionaries, and no
    dictionary was supplied, is the antecedent augmented with every pairwise
    product, with at most `WIDENING_SHARE` of the time left. Like the DSOS
    widening it is optional: unless it finds a certificate or the request is
    cancelled, the lane stays ``none``.
    """
    first = certify(goal, limits, deadline, cancel, dictionary)
    if dictionary is not None or first.outcome != "none":
        return first
    if not nonstrict(goal.antecedent):
        return first  # no constraints to multiply
    now = time.monotonic()
    share = now + WIDENING_SHARE * max(0.0, deadline - now)
    second = certify_products(goal, limits, share, cancel)
    if second.outcome == "found":
        return second
    if second.outcome == "cancelled":
        return LaneOutcome("cancelled", f"{first.detail}; products: {second.detail}")
    return LaneOutcome(
        "none", f"{first.detail}; constraint products ended {second.outcome}"
    )


def certify_products(
    goal: Goal,
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None = None,
) -> LaneOutcome:
    """Search for a Psatz certificate over every pairwise constraint product.

    The antecedent's constraints are augmented with ``−(p_i · p_j)`` for every
    pair ``i ≤ j``, in Lean's ``augment`` order, and the default dictionary and
    its DSOS widening are searched against that list. The weights are bounded
    before anything is built, since the pairs grow quadratically. The rows
    found are against the augmented list; the pairs no row uses are then
    dropped, so the certificate names only the products it needs.
    """
    antecedent = nonstrict(goal.antecedent)
    consequent = nonstrict(goal.consequent)
    if antecedent is None or consequent is None or not antecedent:
        return LaneOutcome(
            "unsupported-fragment", "constraint products need non-strict constraints"
        )
    size = len(antecedent)
    pairs = tuple((i, j) for i in range(size) for j in range(i, size))
    weights = (
        len(consequent)
        * (1 + size + len(pairs))
        * len(default_dictionary(goal.context))
    )
    if weights > limits.max_weights:
        return LaneOutcome(
            "resource-limit", f"{len(pairs)} constraint products exceed the weights"
        )
    found = _widening(
        goal.context,
        augment(antecedent, pairs),
        consequent,
        limits,
        deadline,
        cancel,
    )
    if found.outcome != "found" or not isinstance(found.evidence, Certificate):
        return found
    pruned = _prune(found.evidence.rows, pairs, size)
    if isinstance(pruned, PsatzCertificate):
        return LaneOutcome("found", f"{found.detail} over constraint products", pruned)
    return LaneOutcome("found", f"{found.detail}; no constraint product used", pruned)


def _prune(
    rows: tuple[Row, ...], pairs: tuple[tuple[int, int], ...], size: int
) -> PsatzCertificate | Certificate:
    """Keep only the pairs some row uses, and those rows' multipliers for them."""
    used = [
        k
        for k in range(len(pairs))
        if any(row.multipliers[size + k].terms for row in rows)
    ]
    kept = tuple(
        Row(
            row.base,
            row.multipliers[:size] + tuple(row.multipliers[size + k] for k in used),
        )
        for row in rows
    )
    if not used:
        return Certificate(kept)
    return PsatzCertificate(tuple(pairs[k] for k in used), kept)


def decode_dictionary(
    data: Any, context: Context, limits: EntailmentLimits
) -> tuple[Poly, ...]:
    """Read a dictionary: a JSON list of tagged polynomials over ``context``."""
    if type(data) is not list or not 0 < len(data) <= limits.max_dictionary:
        raise GoalError("a dictionary is a non-empty list within the limit")
    budget = DecodeBudget(limits, context.dimension)
    return tuple(decode_poly(entry, budget) for entry in data)


def _check_dictionary(
    context: Context, squares: tuple[Poly, ...], limits: EntailmentLimits
) -> None:
    if not squares or len(squares) > limits.max_dictionary:
        raise ExpansionLimit("the dictionary is empty or too large")
    # A dictionary polynomial is checked like goal syntax: coordinates in the
    # context, bounded coefficients and size.
    try:
        for square in squares:
            probe = Atom(Relation.LE, square)
            check_goal_limits(Goal(context, probe, probe), limits)
    except GoalError as error:
        raise ExpansionLimit(f"invalid dictionary: {error}") from error


def _row(weights: list[Fraction], squares: tuple[Poly, ...], slots: int) -> Row:
    size = len(squares)

    def square_sum(block: int) -> SquareSum:
        chunk = weights[block * size : (block + 1) * size]
        return SquareSum(tuple((w, g) for w, g in zip(chunk, squares) if w))

    return Row(square_sum(0), tuple(square_sum(1 + i) for i in range(slots)))


def _encode_sparse(poly: Sparse) -> list[Any]:
    return [
        [list(monomial), [value.numerator, value.denominator]]
        for monomial, value in sorted(poly.items())
    ]


def _rationals(data: Any, count: int, limits: EntailmentLimits) -> list[Fraction]:
    if type(data) is not list or len(data) != count:
        raise ValueError("wrong number of values")
    values = []
    for pair in data:
        if (
            type(pair) is not list
            or len(pair) != 2
            or any(type(part) is not int for part in pair)
            or pair[1] <= 0
        ):
            raise ValueError("a value is not a rational pair")
        value = Fraction(pair[0], pair[1])
        check_rational_bits(value, limits)
        values.append(value)
    return values
