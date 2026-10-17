"""Exact sparse polynomials with expansion bounded before it runs.

The certificate producer expands the fixed polynomials of a candidate identity
to set up linear equations for the solver. This is search-side arithmetic: Lean
re-normalizes every identity itself (``Sparse.ofExpr``), so a mistake here can
only lose a certificate, never admit one.

A monomial is a sorted tuple of coordinate indices, with repetition, the same
normal form as Lean's ``Sparse.normalizeMonomial``.
"""

from __future__ import annotations

from fractions import Fraction
from math import comb

from .limits import EntailmentLimits
from .syntax import Add, Const, Mul, Neg, Poly, Var

Monomial = tuple[int, ...]
Sparse = dict[Monomial, Fraction]


class ExpansionLimit(ValueError):
    """An expansion would exceed the degree or monomial limit."""


def degree_bound(poly: Poly) -> int:
    """Bound the degree from the syntax, without expanding."""
    if type(poly) is Var:
        return 1
    if type(poly) is Const:
        return 0
    if type(poly) is Neg:
        return degree_bound(poly.argument)
    if type(poly) is Add:
        return max(degree_bound(poly.left), degree_bound(poly.right))
    if type(poly) is Mul:
        return degree_bound(poly.left) + degree_bound(poly.right)
    raise TypeError(f"not a polynomial: {poly!r}")


def monomial_bound(dimension: int, degree: int) -> int:
    """How many monomials of degree at most ``degree`` exist in ``dimension``."""
    return comb(dimension + degree, degree)


def check_expansion(dimension: int, degree: int, limits: EntailmentLimits) -> None:
    """Refuse an expansion before it runs if its result could be too large.

    Every subexpression's degree is at most its parent's, so the bound on the
    result also bounds every intermediate polynomial.
    """
    if degree > limits.max_degree:
        raise ExpansionLimit(f"degree {degree} exceeds {limits.max_degree}")
    if monomial_bound(dimension, degree) > limits.max_monomials:
        raise ExpansionLimit(
            f"up to {monomial_bound(dimension, degree)} monomials exceeds "
            f"{limits.max_monomials}"
        )


def expand(poly: Poly, dimension: int, limits: EntailmentLimits) -> Sparse:
    """Expand a polynomial to sparse form, after checking the expansion bound."""
    check_expansion(dimension, degree_bound(poly), limits)
    return _expand(poly)


def _expand(poly: Poly) -> Sparse:
    if type(poly) is Var:
        return {(poly.index,): Fraction(1)}
    if type(poly) is Const:
        return {(): poly.value} if poly.value else {}
    if type(poly) is Neg:
        return scale(_expand(poly.argument), Fraction(-1))
    if type(poly) is Add:
        return add(_expand(poly.left), _expand(poly.right))
    if type(poly) is Mul:
        return multiply(_expand(poly.left), _expand(poly.right))
    raise TypeError(f"not a polynomial: {poly!r}")


def add(left: Sparse, right: Sparse) -> Sparse:
    """Sum, dropping cancelled monomials."""
    result = dict(left)
    for monomial, coefficient in right.items():
        total = result.get(monomial, Fraction(0)) + coefficient
        if total:
            result[monomial] = total
        else:
            result.pop(monomial, None)
    return result


def scale(poly: Sparse, factor: Fraction) -> Sparse:
    """Multiply every coefficient by a rational."""
    if not factor:
        return {}
    return {monomial: coefficient * factor for monomial, coefficient in poly.items()}


def multiply(left: Sparse, right: Sparse) -> Sparse:
    """Product, with monomials kept in sorted normal form."""
    result: Sparse = {}
    for first, a in left.items():
        for second, b in right.items():
            monomial = tuple(sorted(first + second))
            total = result.get(monomial, Fraction(0)) + a * b
            if total:
                result[monomial] = total
            else:
                result.pop(monomial, None)
    return result
