"""Regenerate the Lean checker application from validated exact data.

The prelude and statement are rendered from the *expected* goal alone and are
trusted, as the prelude of any replay is: they fix what is being proved. The
proof script is rendered from validated evidence and is untrusted, which is
what `check_independently` assumes. Nothing is spliced in from a solver or an
artifact as text: every name has passed `syntax.Context`'s charset check,
every number is a Python ``int`` and every constructor is emitted from a
closed table.

The statement is ``FormulaEntailment P Q`` (or its negation) about the
reconstructed formulas themselves, not ``goal.Entailment``, so the theorem that
is checked is the one the task asks for; the checker lemmas prove
``goal.Entailment``, which unfolds to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .algebraic import AlgebraicRoot, AlgebraicWitness, AtomProof, root_polynomial
from .evidence import Certificate, PsatzCertificate, Row, SquareSum, Witness
from .syntax import (
    Add,
    And,
    Atom,
    Const,
    Fls,
    Formula,
    Goal,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Tru,
    Var,
)

#: The one library module the application imports; the replay environment
#: pins its source closure.
IMPORT = "Gimle.Forseti.Syntax.Certificate"
#: The import set replay pins, for `glitnir_forseti.lean.closure`.
IMPORTS = (IMPORT,)
#: The checker for real-algebraic counterexamples (forseti-lean 022); it
#: imports ``IMPORT`` and pins its own closure.
ALGEBRAIC_IMPORT = "Gimle.Forseti.Syntax.AlgebraicWitness"
ALGEBRAIC_IMPORTS = (ALGEBRAIC_IMPORT,)
#: The checker for degree-2 Positivstellensatz certificates (forseti-lean 027);
#: it imports ``IMPORT`` and pins its own closure.
PSATZ_IMPORT = "Gimle.Forseti.Syntax.Positivstellensatz"
PSATZ_IMPORTS = (PSATZ_IMPORT,)

#: Every checker name the application uses, fully qualified.
_SYNTAX = "Gimle.Forseti.Syntax"

_RELATIONS = {"eq": ".eq", "le": ".le", "lt": ".lt", "ge": ".ge", "gt": ".gt"}


@dataclass(frozen=True, slots=True)
class Application:
    """The three parts `check_independently` takes."""

    prelude: str
    statement: str
    script: tuple[str, ...]

    @property
    def source(self) -> str:
        """Render the whole unit, as a documentary record."""
        from glitnir_forseti.lean.unit import render_unit

        return render_unit(self.prelude, self.statement, self.script)


def rational(value: Fraction) -> str:
    """Render ``mkRat n d`` from the value's exact integers."""
    numerator = int(value.numerator)
    denominator = int(value.denominator)
    return f"(mkRat ({numerator}) {denominator})"


def poly(value: Poly) -> str:
    """Render a ``Polynomial.Expr`` term."""
    if type(value) is Var:
        return f"(.var ⟨{int(value.index)}, by decide⟩)"
    if type(value) is Const:
        return f"(.constant {rational(value.value)})"
    if type(value) is Add:
        return f"(.add {poly(value.left)} {poly(value.right)})"
    if type(value) is Mul:
        return f"(.mul {poly(value.left)} {poly(value.right)})"
    if type(value) is Neg:
        return f"(.neg {poly(value.argument)})"
    raise TypeError(f"not a polynomial: {value!r}")


def formula(value: Formula) -> str:
    """Render a ``Formula`` term."""
    if type(value) is Tru:
        return ".tru"
    if type(value) is Fls:
        return ".fls"
    if type(value) is Atom:
        return f"(.atom {_RELATIONS[value.relation.value]} {poly(value.polynomial)})"
    if type(value) is And:
        return f"(.and {formula(value.left)} {formula(value.right)})"
    if type(value) is Or:
        return f"(.or {formula(value.left)} {formula(value.right)})"
    if type(value) is Not:
        return f"(.not {formula(value.argument)})"
    raise TypeError(f"not a formula: {value!r}")


def prelude(goal: Goal, module: str = IMPORT) -> str:
    """Render the imports and the goal's definitions, from the expected goal.

    ``module`` is one of this module's closed import names, never data.
    """
    if module not in (IMPORT, ALGEBRAIC_IMPORT, PSATZ_IMPORT):
        raise ValueError(f"not a checker module: {module!r}")
    names = ", ".join(f'"{name}"' for name in goal.context.names)
    dimension = goal.context.dimension
    # Every name is fully qualified: no ``open`` lets a short name resolve to
    # a declaration some later release adds elsewhere in the closure.
    return (
        f"import {module}\n\n"
        "def forsetiContext : Gimle.Forseti.Syntax.Context :=\n"
        f"  ⟨[{names}], by decide, by decide, by decide⟩\n\n"
        f"def forsetiAntecedent : Gimle.Forseti.Syntax.Formula {dimension} :=\n"
        f"  {formula(goal.antecedent)}\n\n"
        f"def forsetiConsequent : Gimle.Forseti.Syntax.Formula {dimension} :=\n"
        f"  {formula(goal.consequent)}\n\n"
        "def forsetiGoal : Gimle.Forseti.Syntax.Goal := "
        "⟨forsetiContext, forsetiAntecedent, forsetiConsequent⟩\n"
    )


def _square_sum(value: SquareSum) -> str:
    terms = ", ".join(f"({rational(w)}, {poly(g)})" for w, g in value.terms)
    return f"⟨[{terms}]⟩"


def _rows(rows: tuple[Row, ...]) -> str:
    return ", ".join(
        f"⟨{_square_sum(row.base)}, "
        f"[{', '.join(_square_sum(m) for m in row.multipliers)}]⟩"
        for row in rows
    )


def prove(goal: Goal, certificate: Certificate) -> Application:
    """Prove ``FormulaEntailment P Q`` by ``EntailmentCertificate.sound``."""
    rows = _rows(certificate.rows)
    return Application(
        prelude(goal),
        f": {_SYNTAX}.FormulaEntailment forsetiAntecedent forsetiConsequent",
        (
            f"exact {_SYNTAX}.EntailmentCertificate.sound forsetiGoal ⟨[{rows}]⟩ "
            "(by decide +kernel)",
        ),
    )


def prove_psatz(goal: Goal, certificate: PsatzCertificate) -> Application:
    """Prove ``FormulaEntailment P Q`` by ``PsatzCertificate.sound``."""
    if any(type(k) is not int for pair in certificate.pairs for k in pair):
        raise ValueError("pair indices are integers")
    pairs = ", ".join(f"({i}, {j})" for i, j in certificate.pairs)
    return Application(
        prelude(goal, PSATZ_IMPORT),
        f": {_SYNTAX}.FormulaEntailment forsetiAntecedent forsetiConsequent",
        (
            f"exact {_SYNTAX}.PsatzCertificate.sound forsetiGoal "
            f"⟨[{pairs}], [{_rows(certificate.rows)}]⟩ (by decide +kernel)",
        ),
    )


def refute(goal: Goal, witness: Witness) -> Application:
    """Prove ``¬ FormulaEntailment P Q`` by ``refutes_sound``."""
    if tuple(name for name, _ in witness.assignment) != goal.context.names:
        raise ValueError("a witness must name the goal's coordinates in order")
    # Names come from the expected goal's validated context, never the witness.
    entries = ", ".join(
        f'("{name}", {rational(v)})'
        for name, (_, v) in zip(goal.context.names, witness.assignment)
    )
    return Application(
        prelude(goal),
        f": ¬ {_SYNTAX}.FormulaEntailment forsetiAntecedent forsetiConsequent",
        (
            f"exact {_SYNTAX}.refutes_sound forsetiGoal [{entries}] "
            "(by decide +kernel)",
        ),
    )


def _root(value: AlgebraicRoot) -> str:
    return (
        f"⟨{poly(root_polynomial(value))}, "
        f"⟨{rational(value.lower)}, {rational(value.upper)}⟩⟩"
    )


def _atom_proof(value: AtomProof) -> str:
    if value.kind == "enclosure":
        return ".enclosure"
    if value.kind == "zero":
        return f"(.zero [{', '.join(poly(m) for m in value.multipliers)}])"
    raise ValueError(f"not atom evidence: {value.kind!r}")


def refute_algebraic(goal: Goal, witness: AlgebraicWitness) -> Application:
    """Prove ``¬ FormulaEntailment P Q`` by ``algebraicRefutes_sound``."""
    if len(witness.coordinates) != goal.context.dimension:
        raise ValueError("an algebraic witness gives one root per coordinate")
    roots = ", ".join(_root(root) for root in witness.coordinates)
    antecedent = ", ".join(_atom_proof(a) for a in witness.antecedent)
    consequent = ", ".join(_atom_proof(a) for a in witness.consequent)
    return Application(
        prelude(goal, ALGEBRAIC_IMPORT),
        f": ¬ {_SYNTAX}.FormulaEntailment forsetiAntecedent forsetiConsequent",
        (
            f"exact {_SYNTAX}.algebraicRefutes_sound forsetiGoal "
            f"⟨[{roots}], [{antecedent}], [{consequent}]⟩ (by decide +kernel)",
        ),
    )


def application(
    goal: Goal, evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness
) -> Application:
    """Render the checker application for any kind of evidence."""
    if isinstance(evidence, Certificate):
        return prove(goal, evidence)
    if isinstance(evidence, PsatzCertificate):
        return prove_psatz(goal, evidence)
    if isinstance(evidence, AlgebraicWitness):
        return refute_algebraic(goal, evidence)
    return refute(goal, evidence)


def imports_for(
    evidence: Certificate | PsatzCertificate | Witness | AlgebraicWitness,
) -> tuple[str, ...]:
    """Return the import set the evidence's application is checked in."""
    if isinstance(evidence, AlgebraicWitness):
        return ALGEBRAIC_IMPORTS
    if isinstance(evidence, PsatzCertificate):
        return PSATZ_IMPORTS
    return IMPORTS
