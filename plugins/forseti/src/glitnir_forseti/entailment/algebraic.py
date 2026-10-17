"""Real-algebraic counterexamples: the exact data and its encoding.

Some false entailments fail at no rational point: ``x² = 2 ⊨ x < 0`` fails
only at ``±√2``. Z3 represents such a model exactly, as an algebraic number: a
polynomial and bounds on one of its roots. This module holds the data
``forseti-lean``'s ``algebraicRefutes`` checks (task 022):

- per coordinate, a univariate polynomial and a rational interval holding
  exactly one of its roots (a rational coordinate ``q`` is ``x − q`` on
  ``[q, q]``);
- per atom of the goal, in the order the formulas are read, how its sign at
  the point is established: an interval **enclosure** over the box that
  excludes zero, or, for a zero, **multipliers** ``sᵢ`` with
  ``q = Σᵢ sᵢ · pᵢ(xᵢ)``.

The producer that builds it from Z3's model is `algebraic_search`, which
replay never imports. Decoding here is structural only: Lean recomputes the
root isolation, every enclosure and every identity itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .evidence import EvidenceError
from .limits import EntailmentLimits
from .syntax import (
    Add,
    Const,
    Context,
    DecodeBudget,
    GoalError,
    Mul,
    Poly,
    Var,
    decode_poly,
    decode_rational,
    encode_poly,
    encode_rational,
)


@dataclass(frozen=True, slots=True)
class AlgebraicRoot:
    """One coordinate: a polynomial and an interval meant to isolate one root.

    The coefficients are lowest degree first.
    """

    coefficients: tuple[Fraction, ...]
    lower: Fraction
    upper: Fraction


@dataclass(frozen=True, slots=True)
class AtomProof:
    """How one atom's sign is established.

    ``zero`` carries one multiplier per coordinate; ``enclosure`` carries none.
    """

    kind: str
    multipliers: tuple[Poly, ...] = ()


@dataclass(frozen=True, slots=True)
class AlgebraicWitness:
    """A real-algebraic counterexample, as ``algebraicRefutes`` reads it."""

    coordinates: tuple[AlgebraicRoot, ...]
    antecedent: tuple[AtomProof, ...]
    consequent: tuple[AtomProof, ...]


def root_polynomial(root: AlgebraicRoot) -> Poly:
    """Return the root's polynomial as an expression in ``Var(0)``."""
    terms: Poly = Const(Fraction(0))
    power: Poly = Const(Fraction(1))
    for coefficient in root.coefficients:
        if coefficient:
            terms = Add(terms, Mul(Const(coefficient), power))
        power = Mul(power, Var(0))
    return terms


# --- encoding -------------------------------------------------------------


def encode_algebraic(witness: AlgebraicWitness) -> dict[str, Any]:
    """Encode a witness as tagged JSON."""

    def proof(atom: AtomProof) -> list[Any]:
        if atom.kind == "zero":
            return ["zero", [encode_poly(m) for m in atom.multipliers]]
        return ["enclosure"]

    return {
        "kind": "algebraic-witness",
        "coordinates": [
            {
                "coefficients": [encode_rational(c) for c in root.coefficients],
                "interval": [encode_rational(root.lower), encode_rational(root.upper)],
            }
            for root in witness.coordinates
        ],
        "antecedent": [proof(atom) for atom in witness.antecedent],
        "consequent": [proof(atom) for atom in witness.consequent],
    }


def decode_algebraic(
    data: Any, context: Context, limits: EntailmentLimits
) -> AlgebraicWitness:
    """Read an encoded witness, refusing anything malformed or over a limit."""
    try:
        return _decode(data, context, limits)
    except GoalError as error:
        raise EvidenceError(str(error)) from error


def _decode(data: Any, context: Context, limits: EntailmentLimits) -> AlgebraicWitness:
    keys = {"kind", "coordinates", "antecedent", "consequent"}
    if (
        type(data) is not dict
        or set(data) != keys
        or data["kind"] != "algebraic-witness"
    ):
        raise EvidenceError("an algebraic witness has exactly the v1 fields")
    raw = data["coordinates"]
    if type(raw) is not list or len(raw) != context.dimension:
        raise EvidenceError("an algebraic witness gives one root per coordinate")
    coordinates = []
    for entry in raw:
        if type(entry) is not dict or set(entry) != {"coefficients", "interval"}:
            raise EvidenceError("a root is coefficients and an interval")
        values = entry["coefficients"]
        if type(values) is not list or not 1 < len(values) <= limits.max_degree + 1:
            raise EvidenceError("a root's polynomial has a bounded positive degree")
        interval = entry["interval"]
        if type(interval) is not list or len(interval) != 2:
            raise EvidenceError("an interval is [lower, upper]")
        coefficients = tuple(decode_rational(v, limits) for v in values)
        if not coefficients[-1]:
            raise EvidenceError("a root's leading coefficient is nonzero")
        coordinates.append(
            AlgebraicRoot(
                coefficients,
                decode_rational(interval[0], limits),
                decode_rational(interval[1], limits),
            )
        )
    budget = DecodeBudget(limits, context.dimension)
    atoms = 0

    def proofs(value: Any) -> tuple[AtomProof, ...]:
        nonlocal atoms
        if type(value) is not list:
            raise EvidenceError("atom evidence is a list")
        found = []
        for item in value:
            atoms += 1
            if atoms > limits.max_weights:
                raise EvidenceError("too much atom evidence")
            if item == ["enclosure"]:
                found.append(AtomProof("enclosure"))
            elif type(item) is list and len(item) == 2 and item[0] == "zero":
                multipliers = item[1]
                if (
                    type(multipliers) is not list
                    or len(multipliers) != context.dimension
                ):
                    raise EvidenceError("a zero gives one multiplier per coordinate")
                found.append(
                    AtomProof(
                        "zero", tuple(decode_poly(m, budget) for m in multipliers)
                    )
                )
            else:
                raise EvidenceError("atom evidence is an enclosure or a zero")
        return tuple(found)

    return AlgebraicWitness(
        tuple(coordinates), proofs(data["antecedent"]), proofs(data["consequent"])
    )


__all__ = [
    "AlgebraicRoot",
    "AlgebraicWitness",
    "AtomProof",
    "decode_algebraic",
    "encode_algebraic",
    "root_polynomial",
]
