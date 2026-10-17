"""The Z3 child process: one JSON request on stdin, one JSON answer on stdout.

Z3 runs here, never in the caller's process, so a timeout or cancellation can
kill it outright and a missing ``z3-solver`` install is an answer rather than
an import error. Everything this process says is untrusted: the parent bounds
and re-validates every number, and only Lean decides anything.

Requests:

- ``{"op": "refute", "goal": <goal>, "limits": <limits>, "timeout_ms": n}``
  asks for a model of ``P ∧ ¬Q``. Answers ``sat`` with an exact rational point
  in context order, ``algebraic`` when a coordinate is irrational, ``unsat``
  (no counterexample: not evidence of anything), or ``unknown``.
- ``{"op": "weights", "columns": [...], "target": [...], "timeout_ms": n}``
  asks for nonnegative rationals ``w`` with ``Σ_j w_j · column_j = target``,
  each a sparse polynomial ``[[monomial, [num, den]], ...]``. Linear real
  arithmetic only: every unknown multiplies a fixed polynomial.
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from typing import Any

from .limits import EntailmentLimits
from .syntax import (
    Add,
    And,
    Atom,
    Const,
    Fls,
    Formula,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Relation,
    Tru,
    Var,
    decode_goal,
)


def _poly(z3: Any, variables: list[Any], value: Poly) -> Any:
    if type(value) is Var:
        return variables[value.index]
    if type(value) is Const:
        return z3.RealVal(f"{value.value.numerator}/{value.value.denominator}")
    if type(value) is Add:
        return _poly(z3, variables, value.left) + _poly(z3, variables, value.right)
    if type(value) is Mul:
        return _poly(z3, variables, value.left) * _poly(z3, variables, value.right)
    if type(value) is Neg:
        return -_poly(z3, variables, value.argument)
    raise TypeError(f"not a polynomial: {value!r}")


def translate(z3: Any, variables: list[Any], formula: Formula) -> Any:
    """Translate a formula to Z3: strictness, negation and equality as written."""
    if type(formula) is Tru:
        return z3.BoolVal(True)
    if type(formula) is Fls:
        return z3.BoolVal(False)
    if type(formula) is Atom:
        value = _poly(z3, variables, formula.polynomial)
        return {
            Relation.EQ: lambda: value == 0,
            Relation.LE: lambda: value <= 0,
            Relation.LT: lambda: value < 0,
            Relation.GE: lambda: value >= 0,
            Relation.GT: lambda: value > 0,
        }[formula.relation]()
    if type(formula) is And:
        return z3.And(
            translate(z3, variables, formula.left),
            translate(z3, variables, formula.right),
        )
    if type(formula) is Or:
        return z3.Or(
            translate(z3, variables, formula.left),
            translate(z3, variables, formula.right),
        )
    if type(formula) is Not:
        return z3.Not(translate(z3, variables, formula.argument))
    raise TypeError(f"not a formula: {formula!r}")


def _rational(z3: Any, value: Any) -> list[int] | None:
    if z3.is_rational_value(value):
        return [value.numerator_as_long(), value.denominator_as_long()]
    return None


def refute(z3: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Look for a point where the antecedent holds and the consequent fails."""
    limits = EntailmentLimits.from_dict(request["limits"])
    goal = decode_goal(request["goal"], limits)
    # Coordinates are named by index, so no user name reaches Z3.
    variables = [z3.Real(f"c{i}") for i in range(goal.context.dimension)]
    solver = z3.Solver()
    solver.set("timeout", int(request["timeout_ms"]))
    solver.add(translate(z3, variables, goal.antecedent))
    solver.add(z3.Not(translate(z3, variables, goal.consequent)))
    answer = solver.check()
    if answer == z3.unsat:
        return {"result": "unsat"}
    if answer != z3.sat:
        return {"result": "unknown", "detail": solver.reason_unknown()}
    model = solver.model()
    point: list[Any] = []
    algebraic = []
    for index, variable in enumerate(variables):
        value = model.eval(variable, model_completion=True)
        exact = _rational(z3, value)
        if exact is None:
            algebraic.append(f"coordinate {index} is {value.sexpr()}")
            point.append(_algebraic(z3, value))
        else:
            point.append(exact)
    if algebraic:
        return {"result": "algebraic", "detail": "; ".join(algebraic), "model": point}
    return {"result": "sat", "point": point}


def _algebraic(z3: Any, value: Any) -> dict[str, Any]:
    """Read an algebraic value exactly.

    Returns its polynomial's integer coefficients, lowest degree first, and
    rational bounds on the root. Untrusted: Lean re-checks that the bounds
    isolate a single root before using them.
    """
    polynomial = value.poly()
    lower = z3.RatNumRef(
        z3.Z3_get_algebraic_number_lower(value.ctx_ref(), value.as_ast(), 20), value.ctx
    )
    upper = z3.RatNumRef(
        z3.Z3_get_algebraic_number_upper(value.ctx_ref(), value.as_ast(), 20), value.ctx
    )
    return {
        "polynomial": [polynomial[i].as_long() for i in range(len(polynomial))],
        "lower": _rational(z3, lower),
        "upper": _rational(z3, upper),
    }


def weights(z3: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Solve the linear coefficient equations for nonnegative weights."""
    columns = request["columns"]
    unknowns = [z3.Real(f"w{j}") for j in range(len(columns))]
    solver = z3.Solver()
    solver.set("timeout", int(request["timeout_ms"]))
    for unknown in unknowns:
        solver.add(unknown >= 0)
    equations: dict[tuple[int, ...], list[Any]] = {}
    target = {tuple(m): Fraction(*c) for m, c in request["target"]}
    for unknown, column in zip(unknowns, columns):
        for monomial, coefficient in column:
            value = Fraction(*coefficient)
            term = unknown * z3.RealVal(f"{value.numerator}/{value.denominator}")
            equations.setdefault(tuple(monomial), []).append(term)
    for monomial in set(equations) | set(target):
        wanted = target.get(monomial, Fraction(0))
        solver.add(
            z3.Sum(equations.get(monomial, [z3.RealVal(0)]))
            == z3.RealVal(f"{wanted.numerator}/{wanted.denominator}")
        )
    answer = solver.check()
    if answer == z3.unsat:
        return {"result": "unsat"}
    if answer != z3.sat:
        return {"result": "unknown", "detail": solver.reason_unknown()}
    model = solver.model()
    found = []
    for unknown in unknowns:
        exact = _rational(z3, model.eval(unknown, model_completion=True))
        if exact is None:
            return {"result": "error", "detail": "a weight was not rational"}
        found.append(exact)
    return {"result": "sat", "weights": found}


def answer(request: Any) -> dict[str, Any]:
    """Answer one request; never raises."""
    try:
        import z3  # type: ignore[import-untyped]
    except ImportError as error:
        return {"result": "unavailable", "detail": f"z3 is not installed: {error}"}
    try:
        if type(request) is not dict or request.get("op") not in {"refute", "weights"}:
            return {"result": "error", "detail": "unknown request"}
        operation = refute if request["op"] == "refute" else weights
        return operation(z3, request)
    except Exception as error:  # noqa: BLE001 - reported, never trusted
        return {"result": "error", "detail": f"{type(error).__name__}: {error}"}


def main() -> int:
    """Read one request, write one answer."""
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        request = None
    sys.stdout.write(json.dumps(answer(request)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
