"""The untrusted producer of real-algebraic counterexamples (task 033).

Z3 reports an irrational model exactly: per coordinate, a polynomial and
rational bounds on one of its roots. This module turns it into an
`AlgebraicWitness`: it halves the intervals until each isolates its root, and
establishes each atom's sign at the point by an enclosure over the box or, for
an atom that vanishes there, a zero identity ``q = Σᵢ sᵢ · pᵢ(xᵢ)`` found by
dividing by each coordinate's polynomial in turn.

Everything here only chooses data. Lean recomputes the root isolation, every
enclosure and every identity, and rejects a wrong choice; replay never imports
this module.
"""

from __future__ import annotations

import threading
import time
from fractions import Fraction
from typing import Any

from .algebraic import AlgebraicRoot, AlgebraicWitness, AtomProof, root_polynomial
from .evidence import EvidenceError
from .limits import EntailmentLimits
from .polynomial import ExpansionLimit, Sparse, add, expand, multiply, scale
from .syntax import (
    Add,
    And,
    Atom,
    Const,
    Fls,
    Formula,
    Goal,
    GoalError,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Tru,
    Var,
    check_rational_bits,
    decode_rational,
)

#: How many times the box may be halved while deciding signs.
MAX_REFINEMENTS = 64


def _univariate(root: AlgebraicRoot, value: Fraction) -> Fraction:
    """Evaluate the root's polynomial at ``value`` by Horner's rule."""
    total = Fraction(0)
    for coefficient in reversed(root.coefficients):
        total = total * value + coefficient
    return total


def _enclose(
    poly: Poly, box: tuple[tuple[Fraction, Fraction], ...]
) -> tuple[Fraction, Fraction]:
    """Mirror of Lean's ``enclose``: the same interval arithmetic."""
    if type(poly) is Var:
        return box[poly.index]
    if type(poly) is Const:
        return (poly.value, poly.value)
    if type(poly) is Neg:
        low, high = _enclose(poly.argument, box)
        return (-high, -low)
    if type(poly) is Add:
        (a, b), (c, d) = _enclose(poly.left, box), _enclose(poly.right, box)
        return (a + c, b + d)
    if type(poly) is Mul:
        (a, b), (c, d) = _enclose(poly.left, box), _enclose(poly.right, box)
        corners = (a * c, a * d, b * c, b * d)
        return (min(corners), max(corners))
    raise TypeError(f"not a polynomial: {poly!r}")


def _derivative(poly: Poly) -> Poly:
    """Mirror of Lean's ``derivative`` on univariate expressions."""
    if type(poly) is Var:
        return Const(Fraction(1))
    if type(poly) is Const:
        return Const(Fraction(0))
    if type(poly) is Add:
        return Add(_derivative(poly.left), _derivative(poly.right))
    if type(poly) is Mul:
        return Add(
            Mul(_derivative(poly.left), poly.right),
            Mul(poly.left, _derivative(poly.right)),
        )
    if type(poly) is Neg:
        return Neg(_derivative(poly.argument))
    raise TypeError(f"not a polynomial: {poly!r}")


def root_checks(root: AlgebraicRoot) -> bool:
    """Mirror of Lean's ``Root.check``: order, sign change, derivative sign."""
    if root.lower > root.upper:
        return False
    if _univariate(root, root.lower) * _univariate(root, root.upper) > 0:
        return False
    low, high = _enclose(
        _derivative(root_polynomial(root)), ((root.lower, root.upper),)
    )
    return low > 0 or high < 0


def _halve(root: AlgebraicRoot) -> AlgebraicRoot:
    """Halve the interval, keeping the half where the sign changes."""
    if root.lower == root.upper:
        return root
    middle = (root.lower + root.upper) / 2
    at_middle = _univariate(root, middle)
    if at_middle == 0:
        return AlgebraicRoot(root.coefficients, middle, middle)
    if _univariate(root, root.lower) * at_middle <= 0:
        return AlgebraicRoot(root.coefficients, root.lower, middle)
    return AlgebraicRoot(root.coefficients, middle, root.upper)


def _divide(poly: Sparse, root: AlgebraicRoot, index: int) -> tuple[Sparse, Sparse]:
    """Divide by the root's polynomial in coordinate ``index``.

    Returns the quotient and a remainder of lower degree in that coordinate.
    """
    divisor = {tuple([index] * k): c for k, c in enumerate(root.coefficients) if c}
    degree = len(root.coefficients) - 1
    leading = root.coefficients[-1]
    quotient: Sparse = {}
    remainder = dict(poly)
    while True:
        monomial = max(
            (m for m in remainder if m.count(index) >= degree),
            key=lambda m: m.count(index),
            default=None,
        )
        if monomial is None:
            return quotient, remainder
        rest = list(monomial)
        for _ in range(degree):
            rest.remove(index)
        factor = {tuple(sorted(rest)): remainder[monomial] / leading}
        quotient = add(quotient, factor)
        remainder = add(remainder, scale(multiply(factor, divisor), Fraction(-1)))


def _from_sparse(poly: Sparse) -> Poly:
    """Write a sparse polynomial as an expression."""
    total: Poly = Const(Fraction(0))
    for monomial, coefficient in sorted(poly.items()):
        term: Poly = Const(coefficient)
        for index in monomial:
            term = Mul(term, Var(index))
        total = Add(total, term)
    return total


def _atoms(formula: Formula) -> list[Poly]:
    """List every atom's polynomial, in the order ``decideAt`` reads them."""
    if type(formula) in (Tru, Fls):
        return []
    if type(formula) is Atom:
        return [formula.polynomial]
    if type(formula) in (And, Or):
        return _atoms(formula.left) + _atoms(formula.right)  # type: ignore[union-attr]
    if type(formula) is Not:
        return _atoms(formula.argument)
    raise TypeError(f"not a formula: {formula!r}")


def _zero_identity(
    poly: Poly, roots: tuple[AlgebraicRoot, ...], limits: EntailmentLimits
) -> AtomProof | None:
    """Find multipliers with ``poly = Σᵢ sᵢ · pᵢ(xᵢ)``, or None.

    The identity does not depend on the intervals, so it is found once.

    Raises:
        ExpansionLimit: If the atom is over the expansion limits.
    """
    remainder = expand(poly, len(roots), limits)
    multipliers = []
    for index, root in enumerate(roots):
        quotient, remainder = _divide(remainder, root, index)
        multipliers.append(_from_sparse(quotient))
    if remainder:
        return None
    return AtomProof("zero", tuple(multipliers))


def build_witness(
    goal: Goal,
    roots: tuple[AlgebraicRoot, ...],
    limits: EntailmentLimits,
    deadline: float,
    cancel: threading.Event | None = None,
) -> AlgebraicWitness | str:
    """Refine the roots' intervals until every atom's sign is established.

    An atom is proved by an enclosure excluding zero when one exists, and
    otherwise by its zero identity.

    Returns:
        The witness, or why none was found: a limit, the refinement budget,
        the deadline or cancellation.
    """
    try:
        for root in roots:
            for value in (*root.coefficients, root.lower, root.upper):
                check_rational_bits(value, limits)
    except GoalError as error:
        return f"the model is over a limit: {error}"
    antecedent = _atoms(goal.antecedent)
    atoms = antecedent + _atoms(goal.consequent)
    # Zero identities do not depend on the intervals: each is found at most
    # once, and only for an atom no enclosure has decided.
    zeros: dict[int, AtomProof | None] = {}
    current = roots
    for _ in range(MAX_REFINEMENTS):
        if cancel is not None and cancel.is_set():
            return "cancelled while refining the intervals"
        if time.monotonic() >= deadline:
            return "the search deadline passed while refining the intervals"
        if all(root_checks(root) for root in current):
            try:
                proofs = _signs(atoms, zeros, current, limits)
            except ExpansionLimit as error:
                return f"an atom is over an expansion limit: {error}"
            if proofs is not None:
                count = len(antecedent)
                return AlgebraicWitness(
                    current, tuple(proofs[:count]), tuple(proofs[count:])
                )
        current = tuple(_halve(root) for root in current)
        try:
            for root in current:
                check_rational_bits(root.lower, limits)
                check_rational_bits(root.upper, limits)
        except GoalError:
            return (
                "the intervals outgrew the coefficient bit limit "
                "before every sign was known"
            )
    return "no sign was established within the refinement budget"


def _signs(
    atoms: list[Poly],
    zeros: dict[int, AtomProof | None],
    roots: tuple[AlgebraicRoot, ...],
    limits: EntailmentLimits,
) -> list[AtomProof] | None:
    """Establish every atom's sign over the current box, or return None.

    ``zeros`` caches each atom's zero identity across refinements.

    Raises:
        ExpansionLimit: If an atom needing a zero identity is too large.
    """
    box = tuple((root.lower, root.upper) for root in roots)
    proofs = []
    for position, poly in enumerate(atoms):
        low, high = _enclose(poly, box)
        if low > 0 or high < 0:
            proofs.append(AtomProof("enclosure"))
            continue
        if position not in zeros:
            zeros[position] = _zero_identity(poly, roots, limits)
        zero = zeros[position]
        if zero is None:
            return None
        proofs.append(zero)
    return proofs


def from_model(
    entries: Any, dimension: int, limits: EntailmentLimits
) -> tuple[AlgebraicRoot, ...]:
    """Read the worker's exact model: per coordinate, a rational or a root.

    Raises:
        EvidenceError: If the model is malformed or over a limit.
        GoalError: If a number is malformed or over the bit limit.
    """
    if type(entries) is not list or len(entries) != dimension:
        raise EvidenceError("the model gives one value per coordinate")
    roots = []
    for entry in entries:
        if type(entry) is dict and set(entry) == {"polynomial", "lower", "upper"}:
            values = entry["polynomial"]
            if type(values) is not list or not 1 < len(values) <= limits.max_degree + 1:
                raise EvidenceError("an algebraic value's polynomial is bounded")
            coefficients = tuple(
                decode_rational([v, 1] if type(v) is int else v, limits) for v in values
            )
            if not coefficients[-1]:
                raise EvidenceError("an algebraic value's leading coefficient is zero")
            roots.append(
                AlgebraicRoot(
                    coefficients,
                    decode_rational(entry["lower"], limits),
                    decode_rational(entry["upper"], limits),
                )
            )
        else:
            value = decode_rational(entry, limits)
            roots.append(AlgebraicRoot((-value, Fraction(1)), value, value))
    return tuple(roots)


__all__ = ["MAX_REFINEMENTS", "build_witness", "from_model", "root_checks"]
