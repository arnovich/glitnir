"""The acceptance corpus: 017's five goals and tasks 111's, 033's and 145's additions.

Each goal is written here as data, exactly as ``Examples/PredicateCertificates``
writes it in Lean, and no evidence is written anywhere: the producer must find
it from the formulas alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .syntax import Add, And, Atom, Const, Context, Goal, Mul, Neg, Poly, Relation

PLANE = Context(("x", "y"))
LINE = Context(("x",))
SPACE = Context(("x", "y", "z"))


def _sub(left: Poly, right: Poly) -> Poly:
    return Add(left, Neg(right))


def _const(numerator: int, denominator: int = 1) -> Const:
    return Const(Fraction(numerator, denominator))


def _square(value: Poly) -> Poly:
    return Mul(value, value)


_X, _Y = PLANE.var("x"), PLANE.var("y")
_A, _B, _C = SPACE.var("x"), SPACE.var("y"), SPACE.var("z")
_U = LINE.var("x")
_DISK = Atom(Relation.LE, _sub(Add(_square(_X), _square(_Y)), _const(1)))


@dataclass(frozen=True, slots=True)
class Case:
    """One corpus goal and what the task requires of it.

    Attributes:
        name: A stable identifier.
        claim: The goal as written on paper.
        goal: The goal.
        expected: ``PROVEN``, ``REFUTED`` or ``UNKNOWN:<reason>``.
        dictionary: A restricted square dictionary, when the case needs one.
    """

    name: str
    claim: str
    goal: Goal
    expected: str
    dictionary: tuple[Poly, ...] | None = None


DISK = Goal(
    PLANE,
    _DISK,
    And(
        Atom(Relation.LE, _sub(_square(_X), _const(1))),
        Atom(Relation.LE, _sub(_square(_Y), _const(1))),
    ),
)
QUARTIC = Goal(
    LINE,
    Atom(Relation.LE, _sub(_square(_U), _const(1))),
    Atom(Relation.LE, _sub(Mul(Mul(Mul(_U, _U), _U), _U), _square(_U))),
)
BOX = Goal(
    LINE,
    And(Atom(Relation.GE, Add(_U, _const(1))), Atom(Relation.LE, _sub(_U, _const(1)))),
    Atom(Relation.LE, _sub(_U, _const(2))),
)
HALF_DISK = Goal(
    PLANE,
    _DISK,
    Atom(Relation.LE, _sub(Add(_square(_X), _square(_Y)), _const(1, 2))),
)
UNRELATED = Goal(PLANE, Atom(Relation.LE, _X), Atom(Relation.LE, _Y))
BOX_THREE_HALVES = Goal(LINE, BOX.antecedent, Atom(Relation.LE, _sub(_U, _const(3, 2))))
ROOT_TWO = Goal(
    LINE,
    Atom(Relation.EQ, _sub(_square(_U), _const(2))),
    Atom(Relation.LT, _U),
)
STRICT = Goal(LINE, Atom(Relation.LT, _U), Atom(Relation.LE, _U))

# Task 033: counterexamples at irrational points only.
_ROOT_TWO_X = Atom(Relation.EQ, _sub(_square(_X), _const(2)))
#: Refuted only at −√2: a negative root.
ROOT_TWO_POSITIVE = Goal(LINE, ROOT_TWO.antecedent, Atom(Relation.GT, _U))
#: Independent algebraic coordinates: refuted at (±√2, ±√3) with equal signs.
PRODUCT = Goal(
    PLANE,
    And(_ROOT_TWO_X, Atom(Relation.EQ, _sub(_square(_Y), _const(3)))),
    Atom(Relation.LT, Mul(_X, _Y)),
)
#: One algebraic value at both coordinates, bound only through signs.
SHARED = Goal(
    PLANE,
    And(
        And(_ROOT_TWO_X, Atom(Relation.EQ, _sub(_square(_Y), _const(2)))),
        Atom(Relation.GE, _sub(Mul(_X, _Y), _const(1))),
    ),
    Atom(Relation.LT, _X),
)
#: A rational coordinate beside an algebraic one.
MIXED = Goal(
    PLANE,
    And(_ROOT_TWO_X, Atom(Relation.EQ, _sub(_Y, _const(1)))),
    Atom(Relation.LT, Mul(_X, _Y)),
)
#: The consequent's atom is zero at the point: equality on the boundary.
BOUNDARY = Goal(
    LINE, ROOT_TWO.antecedent, Atom(Relation.LT, _sub(_square(_U), _const(2)))
)
#: ``y = x`` relates two coordinates, which per-coordinate root evidence cannot
#: establish: ``y − x`` is not in the ideal of ``x² − 2`` and ``y² − 2``.
TIED = Goal(
    PLANE,
    And(_ROOT_TWO_X, Atom(Relation.EQ, _sub(_Y, _X))),
    Atom(Relation.LT, _Y),
)

#: The disk goal over ``[y, x]``: the same claim with renamed, reordered
#: coordinates, which the producer must handle without a new certificate.
_SWAPPED = Context(("y", "x"))
_P, _Q = _SWAPPED.var("x"), _SWAPPED.var("y")
DISK_REORDERED = Goal(
    _SWAPPED,
    Atom(Relation.LE, _sub(Add(_square(_P), _square(_Q)), _const(1))),
    And(
        Atom(Relation.LE, _sub(_square(_P), _const(1))),
        Atom(Relation.LE, _sub(_square(_Q), _const(1))),
    ),
)

#: A product bound only a square of ``x ± y`` certifies (task 145's DSOS
#: widening): ``1 − x·y = ½(1 − x²) + ½(1 − y²) + ½(x − y)²``.
UNIT_PRODUCT = Goal(
    PLANE,
    And(
        Atom(Relation.LE, _sub(_square(_X), _const(1))),
        Atom(Relation.LE, _sub(_square(_Y), _const(1))),
    ),
    Atom(Relation.LE, _sub(Mul(_X, _Y), _const(1))),
)
#: The product of two constraints itself (the pair ``(0, 1)`` of
#: ``Gimle.Forseti.Syntax.Positivstellensatz``).
PRODUCT_NONNEG = Goal(
    PLANE,
    And(Atom(Relation.GE, _X), Atom(Relation.GE, _Y)),
    Atom(Relation.GE, Mul(_X, _Y)),
)
#: ``x·y − 1 = (x − 1)(y − 1) + (x − 1) + (y − 1)``: one product, two constraints.
BOX_PRODUCT = Goal(
    PLANE,
    And(
        Atom(Relation.GE, _sub(_X, _const(1))),
        Atom(Relation.GE, _sub(_Y, _const(1))),
    ),
    Atom(Relation.GE, _sub(Mul(_X, _Y), _const(1))),
)
#: A product of three constraints, beyond every dictionary the lane searches.
TRIPLE_PRODUCT = Goal(
    SPACE,
    And(
        And(Atom(Relation.GE, _A), Atom(Relation.GE, _B)),
        Atom(Relation.GE, _C),
    ),
    Atom(Relation.GE, Mul(Mul(_A, _B), _C)),
)

CASES = (
    Case("disk", "x² + y² ≤ 1 ⊨ x² ≤ 1 ∧ y² ≤ 1", DISK, "PROVEN"),
    Case("quartic", "x² ≤ 1 ⊨ x⁴ ≤ x²", QUARTIC, "PROVEN"),
    Case("box", "−1 ≤ x ∧ x ≤ 1 ⊨ x ≤ 2", BOX, "PROVEN"),
    Case("half-disk", "x² + y² ≤ 1 ⊨ x² + y² ≤ 1/2", HALF_DISK, "REFUTED"),
    Case("unrelated", "x ≤ 0 ⊨ y ≤ 0 over [x, y]", UNRELATED, "REFUTED"),
    Case(
        "box-three-halves",
        "−1 ≤ x ∧ x ≤ 1 ⊨ x ≤ 3/2",
        BOX_THREE_HALVES,
        "PROVEN",
    ),
    Case(
        "disk-reordered",
        "the disk claim over [y, x]",
        DISK_REORDERED,
        "PROVEN",
    ),
    Case(
        "root-two",
        "x² = 2 ⊨ x < 0",
        ROOT_TWO,
        "REFUTED",
    ),
    Case("root-two-positive", "x² = 2 ⊨ x > 0", ROOT_TWO_POSITIVE, "REFUTED"),
    Case("product", "x² = 2 ∧ y² = 3 ⊨ x·y < 0", PRODUCT, "REFUTED"),
    Case("shared", "x² = 2 ∧ y² = 2 ∧ x·y ≥ 1 ⊨ x < 0", SHARED, "REFUTED"),
    Case("mixed", "x² = 2 ∧ y = 1 ⊨ x·y < 0", MIXED, "REFUTED"),
    Case("boundary", "x² = 2 ⊨ x² < 2", BOUNDARY, "REFUTED"),
    Case("tied", "x² = 2 ∧ y = x ⊨ y < 0", TIED, "UNKNOWN:unsupported-witness"),
    Case(
        "quartic-restricted",
        "x² ≤ 1 ⊨ x⁴ ≤ x² with dictionary {1}",
        QUARTIC,
        "UNKNOWN:dictionary-exhausted",
        (_const(1),),
    ),
    Case(
        "quartic-restored",
        "x² ≤ 1 ⊨ x⁴ ≤ x² with dictionary {1, x}",
        QUARTIC,
        "PROVEN",
        (_const(1), _U),
    ),
    Case("strict", "x < 0 ⊨ x ≤ 0", STRICT, "UNKNOWN:unsupported-fragment"),
    Case("unit-product", "x² ≤ 1 ∧ y² ≤ 1 ⊨ x·y ≤ 1", UNIT_PRODUCT, "PROVEN"),
    Case("product-nonneg", "x ≥ 0 ∧ y ≥ 0 ⊨ x·y ≥ 0", PRODUCT_NONNEG, "PROVEN"),
    Case("box-product", "x ≥ 1 ∧ y ≥ 1 ⊨ x·y ≥ 1", BOX_PRODUCT, "PROVEN"),
    Case(
        "triple-product",
        "x ≥ 0 ∧ y ≥ 0 ∧ z ≥ 0 ⊨ x·y·z ≥ 0",
        TRIPLE_PRODUCT,
        "UNKNOWN:dictionary-exhausted",
    ),
)
