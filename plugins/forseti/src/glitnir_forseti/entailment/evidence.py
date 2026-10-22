"""Finite exact evidence about a goal, mirroring 017's checker inputs.

Positive evidence is ``EntailmentCertificate``: one `Row` per consequent
constraint, each a base square sum and one multiplier per antecedent
constraint. `PsatzCertificate` (``Gimle.Forseti.Syntax.Positivstellensatz``) also names
pairs of
antecedent constraints and gives its rows against the antecedent augmented with
``−(p_i · p_j) ≤ 0`` per pair (`augment`). Negative evidence is a rational
`Witness`, named coordinate by coordinate. All are untrusted data: this module
only bounds and shapes them, and Lean's ``EntailmentCertificate.check``,
``PsatzCertificate.check`` and ``refutes`` decide them.

`nonstrict` copies ``Formula.nonstrict?`` exactly, so the producer sees the
same constraint lists, in the same order, that the Lean checker will.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .limits import EntailmentLimits
from .syntax import (
    And,
    Atom,
    Context,
    DecodeBudget,
    Formula,
    GoalError,
    Mul,
    Neg,
    Poly,
    Relation,
    Tru,
    check_rational_bits,
    decode_poly,
    decode_rational,
    encode_poly,
    encode_rational,
)


class EvidenceError(ValueError):
    """Evidence was malformed or over a limit."""


def nonstrict(formula: Formula) -> list[Poly] | None:
    """Read a formula as ordered constraints ``p ≤ 0``, if it is in the fragment.

    ``tru``, ``le``, ``ge`` (negated exactly) and ``and``; any other shape is
    outside the positive fragment. Mirrors ``Formula.nonstrict?``.
    """
    if type(formula) is Tru:
        return []
    if type(formula) is Atom and formula.relation is Relation.LE:
        return [formula.polynomial]
    if type(formula) is Atom and formula.relation is Relation.GE:
        return [Neg(formula.polynomial)]
    if type(formula) is And:
        left = nonstrict(formula.left)
        right = nonstrict(formula.right)
        if left is None or right is None:
            return None
        return left + right
    return None


@dataclass(frozen=True, slots=True)
class SquareSum:
    """``Σ w_k · g_k²``: each term a weight and a polynomial."""

    terms: tuple[tuple[Fraction, Poly], ...]


@dataclass(frozen=True, slots=True)
class Row:
    """The evidence for one consequent constraint."""

    base: SquareSum
    multipliers: tuple[SquareSum, ...]


@dataclass(frozen=True, slots=True)
class Certificate:
    """One row per consequent constraint, in order."""

    rows: tuple[Row, ...]


@dataclass(frozen=True, slots=True)
class PsatzCertificate:
    """Positive evidence with pairwise products of antecedent constraints.

    ``pairs`` names ``(i, j)`` of antecedent constraints; ``rows`` has one row
    per consequent constraint, against the antecedent augmented with them.
    """

    pairs: tuple[tuple[int, int], ...]
    rows: tuple[Row, ...]


def augment(antecedent: list[Poly], pairs: tuple[tuple[int, int], ...]) -> list[Poly]:
    """Return the constraints, then ``−(p_i · p_j)`` per pair, as Lean's ``augment``."""
    return antecedent + [Neg(Mul(antecedent[i], antecedent[j])) for i, j in pairs]


@dataclass(frozen=True, slots=True)
class Witness:
    """A rational point, one ``(name, value)`` per coordinate in context order."""

    assignment: tuple[tuple[str, Fraction], ...]


def witness_for(context: Context, point: tuple[Fraction, ...]) -> Witness:
    """Name a point in context order, as ``Context.assign`` does."""
    if len(point) != context.dimension:
        raise EvidenceError("a witness gives one value per coordinate")
    return Witness(tuple(zip(context.names, point)))


def check_witness(witness: Witness, context: Context, limits: EntailmentLimits) -> None:
    """Require exactly the context's names, once each, in order, within limits.

    Lean's ``Context.read`` accepts any order; fixing it here keeps one
    canonical encoding per witness.
    """
    if tuple(name for name, _ in witness.assignment) != context.names:
        raise EvidenceError("a witness names every coordinate once, in order")
    for _, value in witness.assignment:
        if type(value) is not Fraction:
            raise EvidenceError("witness values are fractions.Fraction")
        _bits(value, limits)


def check_certificate(
    certificate: Certificate,
    antecedent: list[Poly],
    consequent: list[Poly],
    limits: EntailmentLimits,
) -> None:
    """Check the certificate's shape against the goal's constraint lists.

    One row per consequent constraint and one multiplier per antecedent
    constraint in every row; weights are bounded rationals. Nonnegativity and
    the identity are Lean's to check; a negative weight is still well-formed
    data, which Lean then rejects.
    """
    if len(certificate.rows) != len(consequent):
        raise EvidenceError("one row per consequent constraint")
    count = 0
    for row in certificate.rows:
        if len(row.multipliers) != len(antecedent):
            raise EvidenceError("one multiplier per antecedent constraint")
        for sum_ in (row.base, *row.multipliers):
            for weight, _ in sum_.terms:
                if type(weight) is not Fraction:
                    raise EvidenceError("weights are fractions.Fraction")
                _bits(weight, limits)
                count += 1
    if count > limits.max_weights:
        raise EvidenceError("too many certificate terms")


def check_psatz(
    certificate: PsatzCertificate,
    antecedent: list[Poly],
    consequent: list[Poly],
    limits: EntailmentLimits,
) -> None:
    """Check the pairs, then the rows against the augmented antecedent.

    Each pair is ``i ≤ j`` over the antecedent's indices, strictly increasing
    with no repeats, there is at least one, and some row uses each. Lean reads
    an index out of range as the constant ``0``, and would accept a pair no row
    uses; refusing both keeps one encoding per certificate.
    """
    pairs = certificate.pairs
    if not pairs:
        raise EvidenceError("a Psatz certificate names at least one pair")
    for pair in pairs:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or any(type(k) is not int for k in pair)
            or not 0 <= pair[0] <= pair[1] < len(antecedent)
        ):
            raise EvidenceError("a pair is (i, j) with 0 ≤ i ≤ j < antecedent size")
    if list(pairs) != sorted(set(pairs)):
        raise EvidenceError("pairs are distinct and in increasing order")
    check_certificate(
        Certificate(certificate.rows),
        augment(antecedent, pairs),
        consequent,
        limits,
    )
    size = len(antecedent)
    for k in range(len(pairs)):
        if not any(row.multipliers[size + k].terms for row in certificate.rows):
            raise EvidenceError("every pair names a product some row uses")


def _bits(value: Fraction, limits: EntailmentLimits) -> None:
    try:
        check_rational_bits(value, limits)
    except GoalError as error:
        raise EvidenceError(str(error)) from error


# --- encoding -------------------------------------------------------------


def _encode_rows(rows: tuple[Row, ...]) -> list[Any]:
    def square_sum(sum_: SquareSum) -> list[Any]:
        return [[encode_rational(w), encode_poly(g)] for w, g in sum_.terms]

    return [
        {
            "base": square_sum(row.base),
            "multipliers": [square_sum(m) for m in row.multipliers],
        }
        for row in rows
    ]


def encode_certificate(certificate: Certificate) -> dict[str, Any]:
    """Encode a certificate as tagged JSON."""
    return {"kind": "certificate", "rows": _encode_rows(certificate.rows)}


def encode_psatz(certificate: PsatzCertificate) -> dict[str, Any]:
    """Encode a Psatz certificate as tagged JSON."""
    return {
        "kind": "psatz-certificate",
        "pairs": [[i, j] for i, j in certificate.pairs],
        "rows": _encode_rows(certificate.rows),
    }


def encode_witness(witness: Witness) -> dict[str, Any]:
    """Encode a witness as tagged JSON."""
    return {
        "kind": "witness",
        "assignment": [[name, encode_rational(v)] for name, v in witness.assignment],
    }


def decode_evidence(
    data: Any, context: Context, limits: EntailmentLimits
) -> Certificate | PsatzCertificate | Witness:
    """Read encoded evidence, refusing anything malformed or over a limit."""
    try:
        return _decode(data, context, limits)
    except GoalError as error:
        raise EvidenceError(str(error)) from error


def _decode(
    data: Any, context: Context, limits: EntailmentLimits
) -> Certificate | PsatzCertificate | Witness:
    if type(data) is not dict:
        raise EvidenceError("evidence is an object")
    if data.get("kind") == "witness" and set(data) == {"kind", "assignment"}:
        entries = data["assignment"]
        if type(entries) is not list or len(entries) > limits.max_variables:
            raise EvidenceError("a witness assignment is a bounded list")
        assignment = []
        for entry in entries:
            if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str:
                raise EvidenceError("a witness entry is [name, rational]")
            assignment.append((entry[0], decode_rational(entry[1], limits)))
        witness = Witness(tuple(assignment))
        check_witness(witness, context, limits)
        return witness
    if data.get("kind") == "certificate" and set(data) == {"kind", "rows"}:
        return Certificate(_decode_rows(data["rows"], context, limits))
    if data.get("kind") == "psatz-certificate" and set(data) == {
        "kind",
        "pairs",
        "rows",
    }:
        entries = data["pairs"]
        if type(entries) is not list or len(entries) > limits.max_weights:
            raise EvidenceError("Psatz pairs are a bounded list")
        pairs = []
        for entry in entries:
            if (
                type(entry) is not list
                or len(entry) != 2
                or any(type(k) is not int for k in entry)
            ):
                raise EvidenceError("a pair is [i, j] with integer indices")
            pairs.append((entry[0], entry[1]))
        rows = _decode_rows(data["rows"], context, limits)
        return PsatzCertificate(tuple(pairs), rows)
    raise EvidenceError("evidence is a certificate or a witness")


def _decode_rows(
    rows: Any, context: Context, limits: EntailmentLimits
) -> tuple[Row, ...]:
    """Read certificate rows, bounding every term and polynomial."""
    budget = DecodeBudget(limits, context.dimension)
    if type(rows) is not list:
        raise EvidenceError("certificate rows are a list")
    terms = 0

    def square_sum(value: Any) -> SquareSum:
        nonlocal terms
        if type(value) is not list:
            raise EvidenceError("a square sum is a list of terms")
        decoded = []
        for term in value:
            terms += 1
            if terms > limits.max_weights:
                raise EvidenceError("too many certificate terms")
            if type(term) is not list or len(term) != 2:
                raise EvidenceError("a square-sum term is [weight, polynomial]")
            decoded.append(
                (decode_rational(term[0], limits), decode_poly(term[1], budget))
            )
        return SquareSum(tuple(decoded))

    decoded_rows = []
    for row in rows:
        if type(row) is not dict or set(row) != {"base", "multipliers"}:
            raise EvidenceError("a row has exactly base and multipliers")
        if type(row["multipliers"]) is not list:
            raise EvidenceError("multipliers are a list")
        decoded_rows.append(
            Row(
                square_sum(row["base"]),
                tuple(square_sum(m) for m in row["multipliers"]),
            )
        )
    return tuple(decoded_rows)
