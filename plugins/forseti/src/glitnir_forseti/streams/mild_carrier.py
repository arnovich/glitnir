"""Bounded exact mild carrier tables for reports, without proof authority.

A coefficient is Σ c t^m exp(-ν λ t). Absolute carrier coefficients are never
used as physical norms or supplied to a proof root. Cancellation matters.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import GoalError, check_rational_bits

from .goal import Modes
from .trig import SearchStopped

Mode = tuple[int, int]
ExpPoly = dict[tuple[int, int], Fraction]
MildPoly = dict[Mode, ExpPoly]
MAX_REPORT_DEGREE = 3


@dataclass(frozen=True)
class _Budget:
    """Bound every report table and rational intermediate, and honor interruption."""

    limits: EntailmentLimits
    deadline: float | None = None
    cancel: threading.Event | None = None

    def check(self, value: Fraction = Fraction(0), count: int = 0) -> None:
        """Stop report arithmetic without changing the mathematical decision."""
        if self.cancel is not None and self.cancel.is_set():
            raise SearchStopped("cancelled", "mild carrier report cancelled")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise SearchStopped("timeout", "mild carrier report deadline reached")
        if count > self.limits.max_monomials:
            raise SearchStopped("resource-limit", "mild carrier report table limit")
        try:
            check_rational_bits(value, self.limits)
        except GoalError as error:
            raise SearchStopped("resource-limit", str(error)) from error


def _put(out: ExpPoly, key: tuple[int, int], value: Fraction, budget: _Budget) -> None:
    """Accumulate a coefficient with exact cancellation and bounded intermediates."""
    budget.check(value)
    total = out.get(key, Fraction(0)) + value
    budget.check(total)
    if total:
        out[key] = total
    else:
        out.pop(key, None)
    budget.check(count=len(out))


def _duhamel(poly: ExpPoly, rate: int, viscosity: Fraction, budget: _Budget) -> ExpPoly:
    """Integrate each basis term by parts, with the exact resonant branch."""
    out: ExpPoly = {}
    for (lam, power), value in poly.items():
        frequency = viscosity * (rate - lam)
        budget.check(frequency)
        if frequency == 0:
            _put(out, (lam, power + 1), value / (power + 1), budget)
            continue
        for j in range(power + 1):
            term = (
                value
                * (-1) ** j
                * Fraction(math.factorial(power), math.factorial(power - j))
                / frequency ** (j + 1)
            )
            _put(out, (lam, power - j), term, budget)
        _put(
            out,
            (rate, 0),
            value
            * (-1) ** (power + 1)
            * math.factorial(power)
            / frequency ** (power + 1),
            budget,
        )
    return out


def duhamel(
    poly: ExpPoly, rate: int, viscosity: Fraction, limits: EntailmentLimits
) -> ExpPoly:
    """Compute one bounded exact carrier integral for diagnostic comparison."""
    if rate < 0 or any(
        lam < 0 or not 0 <= power <= MAX_REPORT_DEGREE for lam, power in poly
    ):
        raise ValueError("carrier rates are nonnegative and report powers are bounded")
    return _duhamel(poly, rate, viscosity, _Budget(limits))


def derivative(poly: ExpPoly, viscosity: Fraction) -> ExpPoly:
    """Differentiate a finite carrier table exactly for diagnostic identities."""
    out: ExpPoly = {}
    for (lam, power), value in poly.items():
        if power:
            key = (lam, power - 1)
            out[key] = out.get(key, Fraction(0)) + power * value
        key = (lam, power)
        out[key] = out.get(key, Fraction(0)) - viscosity * lam * value
    return {key: value for key, value in out.items() if value}


def evaluate(poly: ExpPoly, viscosity: Fraction, at: Fraction) -> Decimal:
    """Evaluate with 60-digit decimal arithmetic: numerical evidence, not a bound."""
    with localcontext() as context:
        context.prec = 60
        t = Decimal(at.numerator) / Decimal(at.denominator)
        nu = Decimal(viscosity.numerator) / Decimal(viscosity.denominator)
        return sum(
            (
                Decimal(c.numerator)
                / Decimal(c.denominator)
                * (t ** power if power else Decimal(1))
                * (-nu * lam * t).exp()
                for (lam, power), c in poly.items()
            ),
            Decimal(0),
        )


def _transport(left: MildPoly, right: MildPoly, budget: _Budget) -> MildPoly:
    """Multiply Fourier modes with the exact vorticity transport coupling."""
    out: MildPoly = {}
    for p, a in left.items():
        lam_p = p[0] ** 2 + p[1] ** 2
        if not lam_p:
            continue
        for q, b in right.items():
            coupling = Fraction(p[0] * q[1] - p[1] * q[0], lam_p)
            budget.check(coupling)
            if not coupling:
                continue
            mode = (p[0] + q[0], p[1] + q[1])
            target = out.setdefault(mode, {})
            for (la, ma), ca in a.items():
                for (lb, mb), cb in b.items():
                    _put(target, (la + lb, ma + mb), coupling * ca * cb, budget)
            budget.check(count=sum(map(len, out.values())))
    return {mode: poly for mode, poly in out.items() if poly}


def exact_terms(
    start: Modes,
    viscosity: Fraction,
    degree: int,
    limits: EntailmentLimits,
    *,
    deadline: float | None = None,
    cancel: threading.Event | None = None,
) -> tuple[MildPoly, ...]:
    """Compute heat and at most three interaction corrections for report evidence."""
    if type(degree) is not int or not 0 <= degree <= MAX_REPORT_DEGREE:
        raise ValueError(f"report degree must lie between 0 and {MAX_REPORT_DEGREE}")
    budget = _Budget(limits, deadline, cancel)
    budget.check(viscosity)
    heat: MildPoly = {}
    for k1, k2, value in start.terms:
        _put(heat.setdefault((k1, k2), {}), (k1 * k1 + k2 * k2, 0), value, budget)
    omega = [{mode: poly for mode, poly in heat.items() if poly}]
    budget.check(count=sum(map(len, omega[0].values())))
    for n in range(degree):
        forcing: MildPoly = {}
        for j in range(n + 1):
            for mode, poly in _transport(omega[j], omega[n - j], budget).items():
                target = forcing.setdefault(mode, {})
                for key, value in poly.items():
                    _put(target, key, -value, budget)
                budget.check(count=sum(map(len, forcing.values())))
        result: MildPoly = {}
        for mode, poly in forcing.items():
            integrated = _duhamel(poly, mode[0] ** 2 + mode[1] ** 2, viscosity, budget)
            if integrated:
                result[mode] = integrated
            budget.check(count=sum(map(len, result.values())))
        omega.append(result)
    return tuple(omega)


def report(terms: tuple[MildPoly, ...], viscosity: Fraction, at: Fraction) -> str:
    """Print a compact physical evaluation table explicitly as numerical evidence."""
    lines = [
        "Carrier report evidence only (60-digit evaluation at T; no supremum claim):",
        "degree | Fourier modes | carrier entries | physical ℓ1(T)",
    ]
    with localcontext() as context:
        context.prec = 60
        for degree, term in enumerate(terms):
            mass = sum(
                (abs(evaluate(poly, viscosity, at)) for poly in term.values()),
                Decimal(0),
            )
            lines.append(
                f"{degree} | {len(term)} | {sum(map(len, term.values()))} | {mass:.12E}"
            )
    return "\n".join(lines)
