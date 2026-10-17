"""Render root theorems about the stream models from validated goals.

Every root is ``forsetiIdentities ∧ forsetiContract`` (or, refuted,
``forsetiIdentities ∧ ¬ forsetiContract``): the identities restate, as
conjuncts Lean proves, what the model entry says the goal's words mean — the
observed coefficients' and the input halo's names, the field's coordinate
names. The contract is a total stream Hoare triple over the original circuit
with a whole-stream precondition:

- full stream: ``StreamHoare admitted circuit (fun y => y = … ∧ Boundary …)``,
  proved by the model's contract;
- coefficients: ``StreamHoare admitted circuit (output.Observes post)``,
  proved with ``lift_certificate`` or refuted with ``refutes_root`` through
  the admitted full input ``[x² + 2t, x², 0]``;
- field: ``StreamHoare (Solves basis x²) (Heat.circuit basis)
  (spec.Observes basis port ![] domain post)``, proved with
  ``FieldSpec.lift_certificate`` after the box lemma gives ``x² ≤ R²``, or
  refuted with ``FieldSpec.refutes_root_answer`` through the solution's own
  input.
"""

from __future__ import annotations

from fractions import Fraction

from glitnir_forseti.entailment.evidence import Certificate, Witness
from glitnir_forseti.entailment.render import _square_sum, formula, rational
from glitnir_forseti.entailment.syntax import (
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
)
from glitnir_forseti.trajectory.render import RootApplication

from .goal import (
    CLASSICAL_EULER,
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    Modes,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    band_of,
)
from .models import MODELS, RULES, StreamModel
from .tail import WindowEvidence, front_window, window_spread
from .trig import (
    BandEvidence,
    RadiusEvidence,
    geometric,
    largest_size,
    start_table,
    tail_bound,
    weight,
)

_SYNTAX = "Gimle.Forseti.Syntax"
_STREAM = "Gimle.Forseti.Stream"
_FIELD_RULES = "Gimle.Forseti.FieldObservation.FieldSpec"
_NOTATION = "Gimle.Forseti.Syntax.ExprNotation"


def _imports(modules: tuple[str, ...]) -> str:
    return "".join(f"import {module}\n" for module in modules) + "\n"


def _certificate(certificate: Certificate) -> str:
    rows = ", ".join(
        f"⟨{_square_sum(row.base)}, "
        f"[{', '.join(_square_sum(m) for m in row.multipliers)}]⟩"
        for row in certificate.rows
    )
    return f"⟨[{rows}]⟩"


def _strings(names: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{name}"' for name in names) + "]"


def _real(value: Fraction) -> str:
    return f"(({rational(value)} : ℚ) : ℝ)"


def _root(identities: str, contract: str) -> str:
    return (
        f"def forsetiIdentities : Prop :=\n  {identities}\n\n"
        f"def forsetiContract : Prop :=\n  {contract}\n\n"
        "def forsetiRoot : Prop := forsetiIdentities ∧ forsetiContract\n"
    )


# --- substitution, for the producers' leaf goals ---------------------------


def substitute_poly(poly: Poly, mapping: dict[int, Poly]) -> Poly:
    """Replace coordinates by polynomials; unmapped coordinates stay."""
    if type(poly) is Var:
        return mapping.get(poly.index, poly)
    if type(poly) is Const:
        return poly
    if type(poly) is Neg:
        return Neg(substitute_poly(poly.argument, mapping))
    if type(poly) is Add:
        return Add(
            substitute_poly(poly.left, mapping), substitute_poly(poly.right, mapping)
        )
    if type(poly) is Mul:
        return Mul(
            substitute_poly(poly.left, mapping), substitute_poly(poly.right, mapping)
        )
    raise TypeError(f"not a polynomial: {poly!r}")


def substitute(value: Formula, mapping: dict[int, Poly]) -> Formula:
    """Replace coordinates in every atom of a formula."""
    if type(value) in (Tru, Fls):
        return value
    if type(value) is Atom:
        return Atom(value.relation, substitute_poly(value.polynomial, mapping))
    if type(value) is And:
        return And(substitute(value.left, mapping), substitute(value.right, mapping))
    if type(value) is Or:
        return Or(substitute(value.left, mapping), substitute(value.right, mapping))
    if type(value) is Not:
        return Not(substitute(value.argument, mapping))
    raise TypeError(f"not a formula: {value!r}")


# --- full stream -------------------------------------------------------------


def mv_polynomial(poly: Poly, dimension: int) -> str:
    """Render a polynomial as an ``MvPolynomial`` over the stream's axes."""
    if type(poly) is Var:
        return f"(MvPolynomial.X {int(poly.index)})"
    if type(poly) is Const:
        return f"(MvPolynomial.C {rational(poly.value)})"
    if type(poly) is Neg:
        return f"(-{mv_polynomial(poly.argument, dimension)})"
    if type(poly) is Add:
        return (
            f"({mv_polynomial(poly.left, dimension)} + "
            f"{mv_polynomial(poly.right, dimension)})"
        )
    if type(poly) is Mul:
        return (
            f"({mv_polynomial(poly.left, dimension)} * "
            f"{mv_polynomial(poly.right, dimension)})"
        )
    raise TypeError(f"not a polynomial: {poly!r}")


def full_stream(
    model: StreamModel, outputs: tuple[tuple[str, Poly], ...]
) -> RootApplication:
    """Prove the full-stream identity by the model's contract.

    The identities say each output constant the contract names is realized by
    the goal's own polynomial, so the root is about the goal's streams.
    """
    stream = model.full_stream
    assert stream is not None
    identities = " ∧\n  ".join(
        f"Gimle.Asgard.Streams.Realizes .ogf {constant}\n"
        f"    ({mv_polynomial(poly, 2)} : Gimle.Asgard.Streams.Poly 2)"
        for constant, (_, poly) in zip(stream.streams, outputs)
    )
    prelude = _imports(model.full_stream_imports) + _root(
        identities,
        f"{_STREAM}.StreamHoare {model.admitted} {model.formal_circuit}\n"
        f"    ({stream.statement})",
    )
    return RootApplication(
        prelude,
        ": forsetiRoot",
        (
            "unfold forsetiRoot forsetiIdentities forsetiContract",
            "refine ⟨?_, " + stream.lemma + "⟩",
            *stream.identity_lines,
        ),
        (
            stream.lemma,
            "Gimle.Asgard.Examples.PolynomialHeat.original_heat",
            "Gimle.Asgard.Examples.PolynomialHeat.square",
        ),
    )


# --- coefficients ------------------------------------------------------------


def _coefficient_prelude(model: StreamModel, claim: CoefficientClaim) -> str:
    c = model.coefficients
    assert c is not None
    output = c.output
    return (
        _imports(model.formal_imports)
        + (
            f"def forsetiPost : {_SYNTAX}.Formula {output}.context.dimension :=\n"
            f"  {formula(claim.post)}\n\n"
        )
        + _root(
            f"{output}.context.names = {_strings(c.output_names)} ∧\n"
            f"  {c.input}.context.names = {_strings(c.input_names)}",
            f"{_STREAM}.StreamHoare {model.admitted} {model.formal_circuit}\n"
            f"    ({output}.Observes forsetiPost)",
        )
    )


def coefficient_leaf_consequent(model: StreamModel, claim: CoefficientClaim) -> Formula:
    """Substitute each observed coefficient's hint into the post."""
    assert model.coefficients is not None
    return substitute(claim.post, dict(enumerate(model.coefficients.hint)))


def prove_coefficients(
    model: StreamModel, claim: CoefficientClaim, certificate: Certificate
) -> RootApplication:
    """Prove a coefficient claim with ``lift_certificate``."""
    c = model.coefficients
    assert c is not None
    script = (
        "unfold forsetiRoot forsetiIdentities forsetiContract",
        "refine ⟨by decide +kernel, ?_⟩",
        f"exact {RULES}.lift_certificate ({RULES}.lowered_ok _ _ _ _) {c.input} rfl "
        f"(antecedent := {c.antecedent_constant}) {_certificate(certificate)} "
        f"(by decide +kernel) {c.inputs_lemma}",
    )
    return RootApplication(
        _coefficient_prelude(model, claim),
        ": forsetiRoot",
        script,
        (f"{RULES}.lift_certificate", c.inputs_lemma, f"{RULES}.lowered_ok"),
    )


def refute_coefficients(
    model: StreamModel, claim: CoefficientClaim, witness: Witness
) -> RootApplication:
    """Refute a coefficient claim with ``refutes_root`` on the admitted input.

    The witness gives the halo coefficients' values; Lean checks they are the
    admitted input's own.

    Raises:
        ValueError: If the witness does not give one value per halo coefficient.
    """
    c = model.coefficients
    assert c is not None
    if len(witness.assignment) != len(c.input_names):
        raise ValueError("a coefficient witness gives one value per halo coefficient")
    values = [value for _, value in witness.assignment]
    answer = (
        "["
        + ", ".join(
            f'("{name}", {rational(value)})'
            for name, value in zip(c.input_names, values)
        )
        + "]"
    )
    listed = ", ".join(rational(value) for value in values)
    admitted = ", ".join(model.admitted_input)
    cases = tuple(
        f"· rw [{c.input}.point_of_slot (by decide +kernel : {c.input}.slot _ = "
        f"⟨{port}, ![{degrees[0]}, {degrees[1]}]⟩)]; "
        f"norm_num [{fact}, Rat.mkRat_eq_div]"
        for (port, degrees), fact in zip(c.input_slots, c.input_facts)
    )
    script = (
        "unfold forsetiIdentities forsetiContract",
        "refine ⟨by decide +kernel, ?_⟩",
        f"refine {RULES}.refutes_root ({RULES}.lowered_ok _ _ _ _) {c.input} rfl "
        f"(antecedent := {c.antecedent_constant}) (answer := {answer}) "
        f"(by decide +kernel) ![{admitted}] ⟨rfl, fun _ _ => rfl⟩ ?_",
        "intro i",
        f"have values : ∀ i, ({_SYNTAX}.Context.valueOf {answer} "
        f"({c.input}.context.name i)).getD 0 = ![{listed}] "
        "(Fin.cast (by decide +kernel) i) := by decide +kernel",
        "rw [values]",
        "fin_cases i",
        *cases,
    )
    return RootApplication(
        _coefficient_prelude(model, claim),
        ": forsetiIdentities ∧ ¬ forsetiContract",
        script,
        (f"{RULES}.refutes_root", f"{RULES}.lowered_ok", *c.input_facts),
    )


# --- field -------------------------------------------------------------------

_T, _X, _U = Var(0), Var(1), Var(2)


def field_domain(claim: FieldClaim) -> Formula:
    """``t0 ≤ t ≤ t1 ∧ x0 ≤ x ≤ x1``, as the four constraints."""
    (t0, t1), (x0, x1) = claim.time, claim.space
    return And(
        Atom(Relation.GE, Add(_T, Neg(Const(t0)))),
        And(
            Atom(Relation.LE, Add(_T, Neg(Const(t1)))),
            And(
                Atom(Relation.GE, Add(_X, Neg(Const(x0)))),
                Atom(Relation.LE, Add(_X, Neg(Const(x1)))),
            ),
        ),
    )


def field_antecedent(claim: FieldClaim) -> Formula:
    """State the time interval and the box bound ``R² − x² ≥ 0`` for ``R = x1``."""
    (t0, t1), (_, radius) = claim.time, claim.space
    return And(
        Atom(Relation.GE, Add(_T, Neg(Const(t0)))),
        And(
            Atom(Relation.LE, Add(_T, Neg(Const(t1)))),
            Atom(Relation.GE, Add(Const(radius * radius), Neg(Mul(_X, _X)))),
        ),
    )


def field_leaf_consequent(model: StreamModel, claim: FieldClaim) -> Formula:
    """Substitute the field's expression for ``u`` in the post."""
    assert model.field is not None
    return substitute(claim.post, {2: model.field.expression})


def _field_prelude(
    goal: StreamGoal, model: StreamModel, claim: FieldClaim, antecedent: bool
) -> str:
    f = model.field
    assert f is not None
    dimension = f"{f.spec}.context.dimension"
    text = _imports(model.field_imports) + (
        f"def forsetiDomain : {_SYNTAX}.Formula {dimension} :=\n"
        f"  {formula(field_domain(claim))}\n\n"
        f"def forsetiPost : {_SYNTAX}.Formula {dimension} :=\n"
        f"  {formula(claim.post)}\n\n"
    )
    if antecedent:
        text += (
            f"def forsetiAntecedent : {_SYNTAX}.Formula {dimension} :=\n"
            f"  {formula(field_antecedent(claim))}\n\n"
        )
    return text + _root(
        f"{f.spec}.context.names = {_strings(f.names)}",
        f"{_STREAM}.StreamHoare ({f.solves} .{goal.basis} {f.profile})\n"
        f"    ({model.field_circuit} .{goal.basis})\n"
        f"    ({f.spec}.Observes .{goal.basis} {f.port} ![] forsetiDomain forsetiPost)",
    )


def _agrees(model: StreamModel) -> str:
    f = model.field
    assert f is not None
    example = f.spec.rsplit(".", 1)[0]
    return (
        f"simp only [{f.expression_constant}, {_NOTATION}.eval_add, "
        f"{_NOTATION}.eval_mul, {_NOTATION}.eval_ofNat, {example}.ut_eval, "
        f"{example}.ux_eval, {f.field_lemma}]; push_cast; ring"
    )


def prove_field(
    goal: StreamGoal, model: StreamModel, claim: FieldClaim, certificate: Certificate
) -> RootApplication:
    """Prove a field claim: box lemma, then ``FieldSpec.lift_certificate``.

    Raises:
        ValueError: If the box is not symmetric in ``x``.
    """
    f = model.field
    assert f is not None
    (_, _), (x0, x1) = claim.time, claim.space
    if x0 != -x1:
        raise ValueError("the box lemma needs a symmetric x interval")
    q = f"(Gimle.Asgard.Streams.Heat.solution {f.profile})"
    point = f"{f.spec}.fieldPoint y ![] {q}"
    script = (
        "unfold forsetiRoot forsetiIdentities forsetiContract",
        "refine ⟨by decide, ?_⟩",
        f"refine {f.spec}.lift_certificate (antecedent := forsetiAntecedent) "
        f"(expression := {f.expression_constant}) ({f.realized} .{goal.basis} _) "
        f"{_certificate(certificate)} (by decide +kernel) "
        "(fun y => ?_) (fun y inside => ?_)",
        f"· {_agrees(model)}",
        f"· have ht : ∀ p, {point} ⟨0, p⟩ = y 0 := fun _ => rfl",
        f"  have hx : ∀ p, {point} ⟨1, p⟩ = y 1 := fun _ => rfl",
        f"  simp only [forsetiDomain, forsetiAntecedent, {_SYNTAX}.Formula.holds, "
        f"{_SYNTAX}.Relation.holds, Gimle.Asgard.Polynomial.Expr.eval, ht, hx] "
        "at inside ⊢",
        "  obtain ⟨start, stop, lower, upper⟩ := inside",
        # The explicit box step: x ∈ [−R, R] gives x² ≤ R², the constraint the
        # default square dictionary cannot derive from linear bounds alone.
        f"  have box := Gimle.Forseti.FieldBound.sq_le_sq_of_box (x := y 1) "
        f"(R := {_real(x1)}) (by norm_num [Rat.mkRat_eq_div] at lower ⊢; linarith) "
        "(by norm_num [Rat.mkRat_eq_div] at upper ⊢; linarith)",
        "  norm_num [Rat.mkRat_eq_div] at start stop box ⊢",
        "  exact ⟨by linarith, by linarith, by nlinarith⟩",
    )
    return RootApplication(
        _field_prelude(goal, model, claim, antecedent=True),
        ": forsetiRoot",
        script,
        (
            f"{_FIELD_RULES}.lift_certificate",
            f.realized,
            f.field_lemma,
            "Gimle.Forseti.FieldBound.sq_le_sq_of_box",
        ),
    )


def refute_field(
    goal: StreamGoal, model: StreamModel, claim: FieldClaim, witness: Witness
) -> RootApplication:
    """Refute a field claim through the solution's own admitted input.

    Raises:
        ValueError: If the witness is not a ``(t, x, u)`` point.
    """
    f = model.field
    assert f is not None
    if tuple(name for name, _ in witness.assignment) != f.names:
        raise ValueError("a field witness names t, x and u")
    tv, xv, uv = (value for _, value in witness.assignment)
    answer = f'[("t", {rational(tv)}), ("x", {rational(xv)}), ("u", {rational(uv)})]'
    example = f.spec.rsplit(".", 1)[0]
    script = (
        "unfold forsetiIdentities forsetiContract",
        "refine ⟨by decide, ?_⟩",
        f"refine {f.spec}.refutes_root_answer (expression := {f.expression_constant}) "
        f"(answer := {answer}) (by decide +kernel) _ ({f.admitted} .{goal.basis} _ 0) "
        f"(Gimle.Asgard.Streams.Heat.circuit_solution .{goal.basis} _ 0) "
        "(q := Gimle.Asgard.Streams.Heat.solution _) rfl "
        f"![{_real(tv)}, {_real(xv)}] (fun i => ?_) ?_",
        f"· have values : ∀ i, ({_SYNTAX}.Context.valueOf {answer} "
        f"({f.spec}.context.name i)).getD 0 = "
        f"![{rational(tv)}, {rational(xv)}, {rational(uv)}] "
        "(Fin.cast (by decide +kernel) i) := by decide +kernel",
        "  rw [values]",
        f"  fin_cases i <;> norm_num [{_FIELD_RULES}.fieldPoint, {_FIELD_RULES}.point, "
        f"{f.field_lemma}, Rat.mkRat_eq_div]",
        f"· {_agrees(model)}",
    )
    return RootApplication(
        _field_prelude(goal, model, claim, antecedent=False),
        ": forsetiIdentities ∧ ¬ forsetiContract",
        script,
        (
            f"{_FIELD_RULES}.refutes_root_answer",
            f.admitted,
            "Gimle.Asgard.Streams.Heat.circuit_solution",
            f.field_lemma,
            example + ".unitExpr",
        ),
    )


#: The asgard-lean module the heat truncation root cites (asgard-lean 026).
ANALYTIC_HEAT = "Gimle.Asgard.Streams.AnalyticHeat"
#: The asgard-lean module the Cole–Hopf truncation root cites.
COLE_HOPF = "Gimle.Asgard.Streams.ColeHopf"
_ASGARD_STREAMS = "Gimle.Asgard.Streams"


def exp_sum_terms(profile: ExpSum) -> str:
    """Write the profile's pairs as a Lean list literal of ``mkRat`` pairs."""
    pairs = ", ".join(f"({rational(c)}, {rational(a)})" for c, a in profile.terms)
    return f"[{pairs}]"


def _tail_relation(goal: StreamGoal, profile: ExpSum) -> str:
    """Write the circuit relation a truncation root quantifies over.

    The relation is in ``a unused v``, the stream, the unused input and the
    other output. For the heat model the circuit rebuilds ``a`` from the
    exponential-sum boundary; for Burgers, from the ``t = 0`` slice of the
    Cole–Hopf front of the exponential-sum potential, at the goal's viscosity.
    """
    basis = f".{goal.basis}"
    terms = exp_sum_terms(profile)
    if goal.viscosity is None:
        return (
            f"({_ASGARD_STREAMS}.Heat.circuit {basis}).Rel\n"
            f"        ![a, {ANALYTIC_HEAT}.boundary {basis} "
            f"({ANALYTIC_HEAT}.expSum {terms}), unused] ![v, a]"
        )
    nu = rational(goal.viscosity)
    return (
        f"({_ASGARD_STREAMS}.Burgers.circuit {basis} {nu}).Rel\n"
        f"        ![a, {COLE_HOPF}.zeroSlice ({COLE_HOPF}.front {nu} {terms}), unused]"
        " ![v, a]"
    )


def _truncation_prelude(
    goal: StreamGoal, profile: ExpSum, claim: TruncationClaim
) -> str:
    """Define the root claim from the expected goal alone.

    The circuit reconstructs some stream from its input, so the claim is not
    vacuous. Every stream it reconstructs has a series that converges on the
    box to its analytic field, and its window ``N`` is within ``ε`` of that
    field there. The convergence is stated, because ``analyticField`` is a
    ``tsum``, which is ``0`` for a divergent series.
    """
    r_t, r_x = claim.radius
    n_t, n_x = claim.window
    basis = f".{goal.basis}"
    relation = _tail_relation(goal, profile)
    tail = MODELS[goal.model].tail
    assert tail is not None
    return (
        f"{_imports(tail.imports)}"
        f"def forsetiBox : {_ASGARD_STREAMS}.Box 2 :=\n"
        f"  ⟨![{rational(r_t)}, {rational(r_x)}], by decide +kernel⟩\n\n"
        "def forsetiRoot : Prop :=\n"
        f"  (∃ a unused v : {_ASGARD_STREAMS}.Stream 2,\n"
        f"      {relation}) ∧\n"
        f"  ∀ a unused v : {_ASGARD_STREAMS}.Stream 2,\n"
        f"      {relation} →\n"
        "    (∀ x, forsetiBox.Mem x →\n"
        f"      HasSum ({_ASGARD_STREAMS}.seriesTerm {basis} a x)\n"
        f"      ({_ASGARD_STREAMS}.analyticField {basis} a x)) ∧\n"
        f"    {_ASGARD_STREAMS}.TruncationBound {basis} a forsetiBox "
        f"![{int(n_t)}, {int(n_x)}] {rational(claim.epsilon)}\n"
    )


def _truncation_parts(goal: StreamGoal) -> tuple[TruncationClaim, ExpSum]:
    claim, profile = goal.claim, goal.profile
    if type(claim) is not TruncationClaim or not isinstance(profile, ExpSum):
        raise ValueError("a truncation proof needs an exponential-sum truncation claim")
    return claim, profile


def prove_truncation(goal: StreamGoal, rho: Fraction) -> RootApplication:
    """Prove a heat truncation claim at radius ``ρ``.

    Existence is ``circuit_stream``. Every reconstruction is the heat stream
    (``candidate_stream``), whose certificate gives convergence (``hasSum``)
    and the bound (``expSum_truncation``).

    Raises:
        ValueError: If the goal is not an exponential-sum truncation claim
            about the heat boundary.
    """
    claim, profile = _truncation_parts(goal)
    if goal.viscosity is not None:
        raise ValueError("the heat truncation proof takes no viscosity")
    basis = f".{goal.basis}"
    radius = f"({rational(rho)} : ℚ)"
    # Inline, not a `have`: `decide` refuses goals that mention a local, and
    # the side conditions' types mention this proof.
    positive = f"(show (0 : ℚ) < {radius} by decide +kernel)"
    heat = ANALYTIC_HEAT
    return RootApplication(
        _truncation_prelude(goal, profile, claim),
        ": forsetiRoot",
        (
            "unfold forsetiRoot",
            f"refine ⟨⟨_, 0, _, {heat}.circuit_stream {basis} _ 0⟩, "
            "fun a unused v rel => ⟨fun x hx => ?_, ?_⟩⟩",
            f"· rw [{heat}.candidate_stream {basis} _ a unused v rel]",
            f"  exact ({heat}.certificate {basis} ({heat}.expSumWeight_nonneg _) "
            f"{positive} ({heat}.profileBound_expSum _ {positive} (by decide +kernel)) "
            f"forsetiBox (({heat}.inside_iff _ _ _ _ _).mpr (by decide +kernel)))"
            ".hasSum hx",
            f"· exact {heat}.expSum_truncation {basis} _ (ρ := {radius}) {positive} "
            "(by decide +kernel) forsetiBox (by decide +kernel) _ (by decide +kernel) "
            "a unused v rel",
        ),
        (
            f"{heat}.circuit_stream",
            f"{heat}.candidate_stream",
            f"{heat}.certificate",
            f"{_ASGARD_STREAMS}.TailCertificate.hasSum",
            f"{heat}.expSum_truncation",
            f"{heat}.profileBound_expSum",
        ),
    )


def prove_front(
    goal: StreamGoal, rho: Fraction, radii: tuple[Fraction, Fraction]
) -> RootApplication:
    """Prove a Cole–Hopf truncation claim at radius ``ρ`` and inverse radii ``r``.

    Existence is ``circuit_front``. Every reconstruction is the front
    (``candidate_quotient`` through the potential's heat equation), whose
    certificate gives convergence (``hasSum``), and ``front_truncation`` the
    bound. The radii are a constant of the prelude, named in the applications,
    and every side condition is an inline ``decide +kernel``: the bound's
    statement mentions the positivity proof, which is closed by the time the
    bound's block runs, and ``decide`` refuses only goals that mention a local.

    Raises:
        ValueError: If the goal is not an exponential-sum truncation claim
            about the Burgers front.
    """
    claim, profile = _truncation_parts(goal)
    if goal.viscosity is None:
        raise ValueError("the front truncation proof needs a viscosity")
    nu = f"({rational(goal.viscosity)} : ℚ)"
    radius = f"({rational(rho)} : ℚ)"
    terms = exp_sum_terms(profile)
    hopf = COLE_HOPF
    n_t, n_x = claim.window
    prelude = (
        _truncation_prelude(goal, profile, claim) + "\ndef forsetiRadii : Fin 2 → ℚ := "
        f"![{rational(radii[0])}, {rational(radii[1])}]\n"
    )
    positive_nu = f"(show (0 : ℚ) < {nu} by decide +kernel)"
    positive = f"(show (0 : ℚ) < {radius} by decide +kernel)"
    nonzero = f"(show {hopf}.constantTerm {terms} ≠ 0 by decide +kernel)"
    decided = "(by decide +kernel)"
    fits, positive_radii, inside, small, box_inside = (decided,) * 5
    majorized = (
        f"(r := forsetiRadii) {positive_radii} {inside} {small} forsetiBox {box_inside}"
    )
    certificate = f"{positive_nu} {positive} {terms} {fits} {majorized}"
    truncation = f"{positive_nu} {positive} {terms} {nonzero} {fits} {majorized}"
    return RootApplication(
        prelude,
        ": forsetiRoot",
        (
            "unfold forsetiRoot",
            f"refine ⟨⟨_, 0, _, {hopf}.circuit_front {nu} {terms} {nonzero} 0⟩, "
            "fun a unused v rel => ⟨fun x hx => ?_, ?_⟩⟩",
            f"· have hc : MvPowerSeries.constantCoeff ({hopf}.expSumHeat {nu} {terms})"
            " ≠ 0 := by",
            f"    rw [{hopf}.constantCoeff_expSumHeat]",
            "    decide +kernel",
            f"  simp only [{hopf}.front] at rel",
            f"  rw [{hopf}.candidate_quotient {nu} _ hc ({hopf}.heatSeries_pde {nu} _) "
            "a unused v rel]",
            f"  exact ({hopf}.certificate {certificate}).hasSum hx",
            f"· exact {hopf}.front_truncation {truncation} ![{int(n_t)}, {int(n_x)}] "
            "(by decide +kernel) a unused v rel",
        ),
        (
            f"{hopf}.circuit_front",
            f"{hopf}.candidate_quotient",
            f"{hopf}.heatSeries_pde",
            f"{hopf}.certificate",
            f"{_ASGARD_STREAMS}.TailCertificate.hasSum",
            f"{hopf}.front_truncation",
        ),
    )


# --- field bands on the front, from a checked window (task 190) ---------------

_DEGREES = "Gimle.Asgard.Streams.Lowering.Degrees"


def _band_parts(goal: StreamGoal) -> tuple[FieldClaim, ExpSum, Fraction]:
    claim, profile = goal.claim, goal.profile
    if (
        type(claim) is not FieldClaim
        or not isinstance(profile, ExpSum)
        or goal.viscosity is None
        or band_of(claim.post) is None
    ):
        raise ValueError(
            "a band proof needs an analytic-field band claim about the front"
        )
    return claim, profile, goal.viscosity


def band_statement(goal: StreamGoal, claim: FieldClaim, stream: str, point: str) -> str:
    """Write the band as Lean, with its bounds as cast rationals."""
    bounds = band_of(claim.post)
    assert bounds is not None
    lo, hi = bounds
    field = f"{_ASGARD_STREAMS}.analyticField .{goal.basis} {stream} {point}"
    parts = []
    if lo is not None:
        parts.append(f"{_real(lo)} ≤ {field}")
    if hi is not None:
        parts.append(f"{field} ≤ {_real(hi)}")
    return " ∧ ".join(parts)


def _band_prelude(goal: StreamGoal, claim: FieldClaim, evidence: WindowEvidence) -> str:
    """Define the box, the radii, the checked table, the point and the root.

    The root quantifies over every stream the Burgers circuit reconstructs
    from the front's slice and every real point of the box, and states that
    the stream's series converges there and the goal's band holds, with the
    bounds the goal names (one side, or both). The convergence is stated, as
    in the truncation root, because ``analyticField`` is a ``tsum``, which is
    ``0`` for a divergent series. The table is the window of the potential's
    inverse the producer computed; Lean checks it through ``WindowIdentity``
    and trusts nothing else about it.
    """
    profile = goal.profile
    assert isinstance(profile, ExpSum)
    (_, r_t), (_, r_x) = claim.time, claim.space
    rows = ",\n    ".join(
        "[" + ", ".join(rational(value) for value in row) + "]"
        for row in evidence.table
    )
    relation = _tail_relation(goal, profile)
    tail = MODELS[goal.model].tail
    assert tail is not None
    point = ""
    if evidence.point is not None:
        p_t, p_x = evidence.point
        point = (
            "def forsetiPoint : Fin 2 → ℚ := "
            f"![{rational(p_t)}, {rational(p_x)}]\n\n"
        )
    return (
        f"{_imports(tail.imports)}"
        f"def forsetiBox : {_ASGARD_STREAMS}.Box 2 :=\n"
        f"  ⟨![{rational(r_t)}, {rational(r_x)}], by decide +kernel⟩\n\n"
        "def forsetiRadii : Fin 2 → ℚ := "
        f"![{rational(evidence.radii[0])}, {rational(evidence.radii[1])}]\n\n"
        f"def forsetiTable : List (List ℚ) := [\n    {rows}]\n\n"
        f"def forsetiQ (k : {_DEGREES} 2) : ℚ := "
        "(forsetiTable.getD (k 0) []).getD (k 1) 0\n\n"
        f"{point}"
        "def forsetiRoot : Prop :=\n"
        f"  ∀ a unused v : {_ASGARD_STREAMS}.Stream 2,\n"
        f"      {relation} →\n"
        "    ∀ x, forsetiBox.Mem x →\n"
        f"      HasSum ({_ASGARD_STREAMS}.seriesTerm .{goal.basis} a x)\n"
        f"        ({_ASGARD_STREAMS}.analyticField .{goal.basis} a x) ∧\n"
        f"      {band_statement(goal, claim, 'a', 'x')}\n"
    )


def window_bounds(
    goal: StreamGoal, claim: FieldClaim, evidence: WindowEvidence
) -> tuple[Fraction, Fraction]:
    """Compute the band the window certifies, ``W₀ ∓ (spread + ε)``, on the box."""
    profile, viscosity = goal.profile, goal.viscosity
    assert isinstance(profile, ExpSum) and viscosity is not None
    coefficients = front_window(viscosity, profile, evidence.table, evidence.window)
    spread = window_spread(coefficients, (claim.time[1], claim.space[1]))
    centre = coefficients[0][0]
    return centre - spread - evidence.epsilon, centre + spread + evidence.epsilon


def _certificate_premises(
    evidence: WindowEvidence, viscosity: Fraction, profile: ExpSum
) -> str:
    """Write the window roots' arguments through ``boxInside``.

    They are ``front_truncation``'s, with the error named: no conclusion of
    the band roots fixes ``ε``.
    """
    nu = f"({rational(viscosity)} : ℚ)"
    radius = f"({rational(evidence.rho)} : ℚ)"
    terms = exp_sum_terms(profile)
    decided = "(by decide +kernel)"
    return (
        f"(ε := {rational(evidence.epsilon)}) "
        f"(show (0 : ℚ) < {nu} by decide +kernel) "
        f"(show (0 : ℚ) < {radius} by decide +kernel) {terms} "
        f"(show {COLE_HOPF}.constantTerm {terms} ≠ 0 by decide +kernel) {decided} "
        f"(r := forsetiRadii) {decided} {decided} {decided} forsetiBox {decided}"
    )


def _window_premises(
    evidence: WindowEvidence, viscosity: Fraction, profile: ExpSum
) -> str:
    """Write the window roots' arguments through the checked identity."""
    n_t, n_x = evidence.window
    decided = "(by decide +kernel)"
    return (
        f"{_certificate_premises(evidence, viscosity, profile)} "
        f"![{int(n_t)}, {int(n_x)}] {decided} {decided} (Q := forsetiQ) {decided}"
    )


_WINDOW_LEMMAS = (
    f"{_ASGARD_STREAMS}.coeff_inv_window",
    f"{_ASGARD_STREAMS}.abs_windowField_sub_le",
)


def prove_band(goal: StreamGoal, evidence: WindowEvidence) -> RootApplication:
    """Prove a band on the front's field from a checked window.

    The band cites ``front_band_of_window``; the side conditions, the window
    identity and the two bound inequalities are all ``decide +kernel``. A side
    the goal does not bound is filled with the window's own bound and dropped
    by projection. Convergence is the front's certificate after
    ``candidate_quotient`` identifies every reconstruction with the front, as
    in `prove_front`.

    Raises:
        ValueError: If the goal is not a band claim about the front, or the
            evidence is a refutation.
    """
    claim, profile, viscosity = _band_parts(goal)
    if evidence.refutes:
        raise ValueError("a band proof carries no refutation point")
    bounds = band_of(claim.post)
    assert bounds is not None
    lo, hi = bounds
    low, high = window_bounds(goal, claim, evidence)
    lo_text = rational(lo if lo is not None else low)
    hi_text = rational(hi if hi is not None else high)
    hopf = COLE_HOPF
    projection = (
        "" if lo is not None and hi is not None else (".1" if lo is not None else ".2")
    )
    nu = f"({rational(viscosity)} : ℚ)"
    terms = exp_sum_terms(profile)
    decided = "(by decide +kernel)"
    certificate = (
        f"(show (0 : ℚ) < {nu} by decide +kernel) "
        f"(show (0 : ℚ) < ({rational(evidence.rho)} : ℚ) by decide +kernel) {terms} "
        f"{decided} (r := forsetiRadii) {decided} {decided} {decided} forsetiBox "
        f"{decided}"
    )
    premises = _window_premises(evidence, viscosity, profile)
    return RootApplication(
        _band_prelude(goal, claim, evidence),
        ": forsetiRoot",
        (
            "unfold forsetiRoot",
            "intro a unused v rel x hx",
            f"refine ⟨?_, ({hopf}.front_band_of_window (lo := ({lo_text} : ℚ)) "
            f"(hi := ({hi_text} : ℚ)) {premises} (by decide +kernel) "
            f"(by decide +kernel) a unused v rel x hx){projection}⟩",
            f"have hc : MvPowerSeries.constantCoeff ({hopf}.expSumHeat {nu} {terms}) "
            "≠ 0 := by",
            f"  rw [{hopf}.constantCoeff_expSumHeat]",
            "  decide +kernel",
            f"simp only [{hopf}.front] at rel",
            f"rw [{hopf}.candidate_quotient {nu} _ hc ({hopf}.heatSeries_pde {nu} _) "
            "a unused v rel]",
            f"exact ({hopf}.certificate {certificate}).hasSum hx",
        ),
        (
            f"{hopf}.front_band_of_window",
            f"{hopf}.front_truncation",
            *_WINDOW_LEMMAS,
            f"{hopf}.candidate_quotient",
            f"{hopf}.heatSeries_pde",
            f"{hopf}.certificate",
            f"{_ASGARD_STREAMS}.TailCertificate.hasSum",
        ),
    )


def refute_band(goal: StreamGoal, evidence: WindowEvidence) -> RootApplication:
    """Refute a band on the front's field at a rational point of the box.

    Above the band the root cites ``front_above_of_window``, below it
    ``front_below_of_window``: the front is one reconstruction, its field at
    the point is within ``ε`` of its window, and the window's value there is
    the exact rational ``frontValue``, which ``decide +kernel`` puts more than
    ``ε`` outside the bound.

    Raises:
        ValueError: If the goal is not a band claim about the front, or the
            evidence carries no refutation point, or its side has no bound.
    """
    claim, profile, viscosity = _band_parts(goal)
    if evidence.point is None or evidence.side not in ("above", "below"):
        raise ValueError("a band refutation carries a point and a side")
    bounds = band_of(claim.post)
    assert bounds is not None
    lo, hi = bounds
    hopf = COLE_HOPF
    both = lo is not None and hi is not None
    premises = _window_premises(evidence, viscosity, profile)
    if evidence.side == "above":
        if hi is None:
            raise ValueError("a refutation above needs an upper bound")
        lemma, bound = f"{hopf}.front_above_of_window", f"(hi := ({rational(hi)} : ℚ))"
        take = "(h a unused v rel x hx).2" + (".2" if both else "")
    else:
        if lo is None:
            raise ValueError("a refutation below needs a lower bound")
        lemma, bound = f"{hopf}.front_below_of_window", f"(lo := ({rational(lo)} : ℚ))"
        take = "(h a unused v rel x hx).2" + (".1" if both else "")
    script = (
        "unfold forsetiRoot",
        "intro h",
        f"exact {lemma} {bound} {premises} forsetiPoint (by decide +kernel) "
        f"(by decide +kernel) fun a unused v rel x hx => {take}",
    )
    return RootApplication(
        _band_prelude(goal, claim, evidence),
        ": ¬ forsetiRoot",
        script,
        (lemma, f"{hopf}.front_truncation"),
    )


# --- the Euler radius: trigonometric streams (task 217) ---------------------------

_TRIG = "Gimle.Asgard.Streams.TrigStream"
_TORUS = "Gimle.Asgard.Streams.Torus"
_NS = "Gimle.Asgard.Streams.NS"


def _trig_parts(goal: StreamGoal) -> tuple[Modes, Fraction]:
    """Read the start of modes and the viscosity a trigonometric proof needs."""
    profile, viscosity = goal.profile, goal.viscosity
    trig = MODELS[goal.model].trig
    if not isinstance(profile, Modes) or viscosity is None or trig is None:
        raise ValueError("a trigonometric proof needs a modes start and a viscosity")
    if goal.basis not in trig.bases:
        raise ValueError("the Euler radius theorems are about the OGF stream")
    return profile, viscosity


def modes_table(start: Modes) -> str:
    """Write the start as the Lean ``Table`` literal of mode-coefficient pairs."""
    pairs = ", ".join(
        f"(({k1}, {k2}), {rational(c)})" for (k1, k2), c in start_table(start)
    )
    return f"[{pairs}]"


def _trig_stream(basis: str, viscosity: Fraction) -> str:
    """Write the Lean term of the vorticity stream from ``forsetiStart``."""
    return f"{_NS}.stream .{basis} {rational(viscosity)} forsetiStart"


def _trig_prelude(
    goal: StreamGoal, start: Modes, viscosity: Fraction, radius: Fraction, contract: str
) -> str:
    """Bind the initial slice and output postcondition to a total circuit contract.

    The postcondition includes convergence, so a divergent totalized ``tsum``
    cannot satisfy a bound by default. Existence and uniqueness come from the
    circuit contract, whose precondition fixes only the initial coefficient.
    """
    trig = MODELS[goal.model].trig
    assert trig is not None
    classical = goal.interpretation in CLASSICAL_EULER
    imports = trig.classical_imports if classical else trig.imports
    guarantee = (
        f"  Gimle.Forseti.Euler.Certified forsetiStart {_real(radius)} output ∧\n"
        if classical
        else f"  (∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ {_real(radius)} →\n"
        f"    HasSum ({_TRIG}.seriesTerm (output 0) t x)\n"
        f"      ({_TRIG}.analyticField (output 0) t x)) ∧\n"
    )
    return (
        f"{_imports(imports)}"
        f"def forsetiTable : {_TORUS}.Table :=\n  {modes_table(start)}\n\n"
        f"noncomputable def forsetiStart : Gimle.Asgard.Streams.TrigStream :=\n"
        f"  fun _ => {_TORUS}.Table.toTrig forsetiTable\n\n"
        "def forsetiPost (output : Gimle.Asgard.Streams.Fourier.Point 1) : Prop :=\n"
        f"{guarantee}"
        f"  {contract}\n\n"
        "def forsetiRoot : Prop :=\n"
        "  Gimle.Forseti.Fourier.Contract\n"
        f"    ({_NS}.vorticityCircuit .{goal.basis} {rational(viscosity)})\n"
        "    (fun input => input 0 0 = forsetiStart 0) forsetiPost\n"
    )


def _trig_contract_lines(basis: str, viscosity: Fraction) -> tuple[str, ...]:
    """Reduce a circuit postcondition to the proved identity of its unique output."""
    return (
        "unfold forsetiRoot",
        "apply (Gimle.Forseti.Fourier.solution_contract "
        f".{basis} {rational(viscosity)} forsetiStart).consequence (fun _ h => h)",
        "intro output same",
        "subst output",
        "unfold forsetiPost",
        "simp only [Matrix.cons_val_zero]",
    )


def _geometric_lines(
    start: Modes,
    basis: str,
    viscosity: Fraction,
    evidence_size: int,
    evidence_weight: Fraction,
    *,
    classical: bool = False,
) -> tuple[str, ...]:
    """Prove ``GeometricBound ω M ρ`` from the start's size and ℓ¹ bounds.

    ``K`` and ``L`` are the evidence's; the kernel decides that the start's
    modes have size at most ``K`` and that ``Table.absSum`` of the start is
    ``L``, and ``norm_num`` folds ``3L`` and ``72LK`` into the root's ``M``
    and ``ρ``.
    """
    big_m, rho = geometric(start)
    stream = _trig_stream(basis, viscosity)
    if classical and big_m == 0:
        return (
            "have zeroStart : forsetiStart 0 = 0 := "
            "Gimle.Forseti.Euler.Table.toTrig_zero "
            "(table := forsetiTable) (by decide +kernel)",
            f"have geo : {_TRIG}.GeometricBound ({stream}) "
            f"{_real(big_m)} {_real(rho)} := by",
            "  simpa [Rat.mkRat_eq_div] using "
            "(Gimle.Forseti.Euler.zero_geometric forsetiStart zeroStart 0)",
        )
    return (
        f"have geo : {_TRIG}.GeometricBound ({stream}) {_real(big_m)} {_real(rho)}"
        " := by",
        "  intro n",
        f"  have h := {_NS}.euler_l1_geometric forsetiStart (K := {evidence_size}) "
        f"(by decide) ({_TORUS}.Table.sizeLE_toTrig (by decide)) "
        f"(L := {rational(evidence_weight)}) "
        f"(({_TORUS}.Table.l1_le_absSum _).trans (le_of_eq (by decide +kernel))) n",
        f"  have e : (3 * ({rational(evidence_weight)} : ℚ) * "
        f"(72 * {rational(evidence_weight)} * (({evidence_size} : ℕ) : ℚ)) ^ n) = "
        f"{rational(big_m)} * {rational(rho)} ^ n := by norm_num [Rat.mkRat_eq_div]",
        "  rw [e] at h",
        f"  have h' : (({_TORUS}.l1 ({stream} n) : ℚ) : ℝ) ≤ "
        f"(({rational(big_m)} * {rational(rho)} ^ n : ℚ) : ℝ) := by exact_mod_cast h",
        "  push_cast at h'",
        "  exact h'",
    )


def _trig_realization_lines(goal: StreamGoal, start: Modes) -> tuple[str, ...]:
    """Prove exactly the chosen reading's convergence or classical guarantee."""
    if goal.interpretation not in CLASSICAL_EULER:
        return (
            f"· exact {_TRIG}.hasSum_analyticField geo "
            "(by norm_num [Rat.mkRat_eq_div]) "
            "(by norm_num [Rat.mkRat_eq_div]) ht",
        )
    claim = goal.claim
    assert isinstance(claim, (TrigTruncationClaim, TrigBandClaim))
    prefix = "Gimle.Forseti.Euler"
    if all(c == 0 for _, _, c in start.terms):
        rho = 1 / (claim.radius + 1)
        return (
            f"· have geoClassical := {prefix}.zero_geometric "
            f"forsetiStart zeroStart {_real(rho)}",
            f"  exact {prefix}.certified forsetiStart",
            f"    (by simp [zeroStart, {_TORUS}.MeanZero])",
            f"    (by simp [zeroStart, {_TORUS}.IsEven])",
            f"    (by rw [zeroStart]; exact {_TORUS}.sizeLE_zero 1) geoClassical",
            "    (by norm_num [Rat.mkRat_eq_div]) (by norm_num [Rat.mkRat_eq_div])",
        )
    return (
        f"· exact {prefix}.certified forsetiStart",
        f"    ({prefix}.Table.meanZero (table := forsetiTable) (by decide +kernel))",
        f"    ({prefix}.Table.isEven (table := forsetiTable) (by decide +kernel))",
        f"    ({_TORUS}.Table.sizeLE_toTrig "
        f"(K := {largest_size(start)}) (by decide)) geo",
        "    (by norm_num [Rat.mkRat_eq_div]) (by norm_num [Rat.mkRat_eq_div])",
    )


def prove_trig_truncation(
    goal: StreamGoal, evidence: RadiusEvidence
) -> RootApplication:
    """Prove a one-axis truncation of the Euler stream at the start's constants.

    Convergence is ``hasSum_analyticField`` from the geometric bound; the
    bound is ``truncationBound_rat`` at the rational ``tailBound``, weakened
    to the claim's ``ε`` by a kernel-decided inequality.

    Raises:
        ValueError: If the goal is not a trig-truncation claim about a modes
            start at viscosity zero.
    """
    start, viscosity = _trig_parts(goal)
    claim = goal.claim
    if type(claim) is not TrigTruncationClaim or viscosity != 0:
        raise ValueError(
            "the Euler radius proof needs a trig-truncation claim at ν = 0"
        )
    big_m, rho = geometric(start)
    r, eps, n = rational(claim.radius), rational(claim.epsilon), int(claim.window)
    contract = (
        f"{_TRIG}.TruncationBound (output 0) {_real(claim.radius)} {n} "
        f"{_real(claim.epsilon)}"
    )
    tail = f"{_TRIG}.tailBound {rational(big_m)} {rational(rho)} {r} {n}"
    return RootApplication(
        _trig_prelude(goal, start, viscosity, claim.radius, contract),
        ": forsetiRoot",
        (
            *_trig_contract_lines(goal.basis, viscosity),
            *_geometric_lines(
                start,
                goal.basis,
                viscosity,
                evidence.size,
                evidence.weight,
                classical=goal.interpretation in CLASSICAL_EULER,
            ),
            (
                "refine ⟨?_, ?_⟩"
                if goal.interpretation in CLASSICAL_EULER
                else "refine ⟨fun t x ht => ?_, ?_⟩"
            ),
            *_trig_realization_lines(goal, start),
            f"· refine ({_TRIG}.truncationBound_rat geo (by decide +kernel) "
            "(by decide +kernel) _).mono ?_",
            f"  exact_mod_cast (show {tail} ≤ {eps} by decide +kernel)",
        ),
        (
            "Gimle.Forseti.Fourier.solution_contract",
            "Gimle.Forseti.Fourier.Contract.consequence",
            *_trig_guarantee_dependencies(goal, start),
            f"{_TRIG}.truncationBound_rat",
            f"{_TRIG}.TruncationBound.mono",
            *_trig_data_dependencies(goal, start),
        ),
    )


def band_total(start: Modes, claim: TrigBandClaim, evidence: BandEvidence) -> Fraction:
    """Return the bound the window certifies, ``Σ_{n<N} a_n rⁿ + tailBound``."""
    big_m, rho = geometric(start)
    r = claim.radius
    spread = sum((a * r**n for n, a in enumerate(evidence.norms)), Fraction(0))
    return spread + tail_bound(big_m, rho, r, evidence.window)


def _trig_guarantee_dependencies(goal: StreamGoal, start: Modes) -> tuple[str, ...]:
    """Name the lemmas used for the requested convergence or classical guarantee."""
    if goal.interpretation not in CLASSICAL_EULER:
        return (f"{_NS}.euler_l1_geometric", f"{_TRIG}.hasSum_analyticField")
    prefix = "Gimle.Forseti.Euler"
    if weight(start) == 0:
        return (f"{prefix}.zero_geometric", f"{prefix}.certified")
    return (f"{_NS}.euler_l1_geometric", f"{prefix}.certified")


def _trig_data_dependencies(goal: StreamGoal, start: Modes) -> tuple[str, ...]:
    """Name the initial-data premises proved in this generated application."""
    ordinary = (f"{_TORUS}.Table.sizeLE_toTrig", f"{_TORUS}.Table.l1_le_absSum")
    if goal.interpretation not in CLASSICAL_EULER:
        return ordinary
    prefix = "Gimle.Forseti.Euler.Table"
    if weight(start) == 0:
        return (f"{prefix}.toTrig_zero", f"{_TORUS}.sizeLE_zero")
    return (*ordinary, f"{prefix}.meanZero", f"{prefix}.isEven")


def prove_trig_band(goal: StreamGoal, evidence: BandEvidence) -> RootApplication:
    """Prove a band on the Euler stream's field from the window's ℓ¹ spread.

    ``abs_analyticField_le`` bounds the sum by ``Σ_{n<N} l1 ω_n rⁿ`` plus the
    tail; each ``l1 ω_n`` is bounded by ``Table.absSum`` of the ``n``-th Picard
    table through ``stream_eq_table``, decided by the kernel; the total ``B``
    is a rational of the prelude, and ``lo ≤ −B``, ``B ≤ hi`` are arithmetic.

    Raises:
        ValueError: If the goal is not a trig-band claim about a modes start
            at viscosity zero.
    """
    start, viscosity = _trig_parts(goal)
    claim = goal.claim
    if (
        type(claim) is not TrigBandClaim
        or viscosity != 0
        or band_of(claim.post) is None
    ):
        raise ValueError("the Euler band proof needs a trig-band claim at ν = 0")
    bounds = band_of(claim.post)
    assert bounds is not None
    lo, hi = bounds
    stream = _trig_stream(goal.basis, viscosity)
    field = f"{_TRIG}.analyticField ({stream}) t x"
    parts = []
    if lo is not None:
        parts.append(f"{_real(lo)} ≤ {_TRIG}.analyticField (output 0) t x")
    if hi is not None:
        parts.append(f"{_TRIG}.analyticField (output 0) t x ≤ {_real(hi)}")
    contract = (
        f"∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ {_real(claim.radius)} →\n    "
        + " ∧ ".join(parts)
    )
    total = band_total(start, claim, evidence)
    n = int(evidence.window)
    norm_lines: list[str] = []
    for degree, norm in enumerate(evidence.norms):
        norm_lines += [
            f"have h{degree} : (({_TORUS}.l1 ({stream} {degree}) : ℚ) : ℝ) ≤ "
            f"{_real(norm)} := by",
            f"  rw [{_NS}.stream_eq_table {rational(viscosity)} forsetiTable "
            "forsetiStart rfl]",
            f"  exact_mod_cast ({_TORUS}.Table.l1_le_absSum _).trans "
            "(le_of_eq (by decide +kernel))",
        ]
    norms = " ".join(f"h{degree}" for degree in range(len(evidence.norms)))
    if lo is not None and hi is not None:
        conclude = "exact ⟨by linarith [hb.1], by linarith [hb.2]⟩"
    elif lo is not None:
        conclude = "linarith [hb.1]"
    else:
        conclude = "linarith [hb.2]"
    return RootApplication(
        _trig_prelude(goal, start, viscosity, claim.radius, contract)
        + f"\ndef forsetiBound : ℚ := {rational(total)}\n",
        ": forsetiRoot",
        (
            *_trig_contract_lines(goal.basis, viscosity),
            *_geometric_lines(
                start,
                goal.basis,
                viscosity,
                evidence.size,
                evidence.weight,
                classical=goal.interpretation in CLASSICAL_EULER,
            ),
            (
                "refine ⟨?_, fun t x ht => ?_⟩"
                if goal.interpretation in CLASSICAL_EULER
                else "refine ⟨fun t x ht => ?_, fun t x ht => ?_⟩"
            ),
            *_trig_realization_lines(goal, start),
            f"· have b := {_TRIG}.abs_analyticField_le (x := x) geo "
            "(by norm_num [Rat.mkRat_eq_div]) "
            f"(by norm_num [Rat.mkRat_eq_div]) ht {n}",
            "  simp only [Finset.sum_range_succ, Finset.sum_range_zero] at b",
            *["  " + line for line in norm_lines],
            f"  have hB : |{field}| ≤ ((forsetiBound : ℚ) : ℝ) := by",
            "    refine b.trans ?_",
            f"    norm_num [forsetiBound, Rat.mkRat_eq_div] at {norms} ⊢",
            "    linarith",
            "  have hb := abs_le.mp hB",
            "  norm_num [forsetiBound, Rat.mkRat_eq_div] at hb ⊢",
            f"  {conclude}",
        ),
        (
            "Gimle.Forseti.Fourier.solution_contract",
            "Gimle.Forseti.Fourier.Contract.consequence",
            *_trig_guarantee_dependencies(goal, start),
            f"{_TRIG}.abs_analyticField_le",
            f"{_NS}.stream_eq_table",
            *_trig_data_dependencies(goal, start),
            *(
                (f"{_TORUS}.Table.l1_le_absSum",)
                if goal.interpretation in CLASSICAL_EULER and weight(start) == 0
                else ()
            ),
        ),
    )
