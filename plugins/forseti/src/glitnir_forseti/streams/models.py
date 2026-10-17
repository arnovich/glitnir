"""The Lean constants the stream lanes cite.

Two models on the axes ``[t, x]``. The first is Asgard's two-axis heat
reconstruction, the circuit that rebuilds ``u`` from ``u_xx`` and its boundary
profile; with the profile ``x²`` its solution is ``u = x² + 2t``. Four claims
are supported, each through theorems ``forseti-lean`` proves about that
circuit:

- the full-stream identity (``FormalHeatContract.heat_contract``);
- coefficient claims about the fixture's observed output coefficients
  (``StreamObservation``, through asgard-lean 023's lowering);
- evaluated-field claims on a box, about the heat circuit in either basis
  (``HeatFieldBound``, through asgard-lean 024's realization);
- certified truncation of the analytic tail for an exponential-sum boundary
  (``AnalyticHeat.expSum_truncation``, asgard-lean 025/026).

The second is the viscous Burgers circuit ``u_t = ν u_xx − u u_x`` at a
declared viscosity, whose only lane is the certified truncation of the Cole–Hopf
front: the exponential sum is the profile of the heat potential ``φ``, and
the claim is about every stream the circuit reconstructs from the front's
``t = 0`` slice (``ColeHopf.front_truncation``).

The third is the vorticity stream on the torus (asgard-lean ``Streams.NS``),
on the one axis ``t`` with trigonometric-polynomial coefficients, declared by
its start as a list of modes and a viscosity; its lanes are the Euler radius'
certified truncation and band (``NS.euler_l1_geometric``,
``TrigStream.truncationBound_rat``, ``TrigStream.abs_analyticField_le``), for
``ν = 0`` only. Each root is a total ``Fourier.Contract`` over
``NS.vorticityCircuit``: initial vorticity admits a unique output, and every
output satisfies the requested convergence and bound.

The entries are trusted in the way a replay prelude is: they decide which
constants a goal's words refer to. The identities they supply — coefficient
names, the input halo, the field's coordinate names — are restated as
conjuncts Lean proves. The ``hint`` expressions only steer the certificate
search: Lean checks every certificate against its own leaf goal, so a wrong
hint loses a certificate, never admits one.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Formula,
    Mul,
    Neg,
    Poly,
    Relation,
    Var,
)

_EXAMPLE = "Gimle.Forseti.Examples.StreamObservation"
_CONTRACT = "Gimle.Forseti.Examples.FormalHeatContract"
_FIELD = "Gimle.Forseti.Examples.HeatFieldBound"
_HEAT = "Gimle.Asgard.Examples.FormalHeat"
_RULES = "Gimle.Forseti.StreamObservation"


def _equals(left: Poly, right: Poly) -> Formula:
    """``left = right`` as the two non-strict constraints the checker reads."""
    difference = Add(left, Neg(right))
    return And(Atom(Relation.GE, difference), Atom(Relation.LE, difference))


_X = Var(0)
#: The declared initial profile ``x²``, over the one coordinate ``x``.
PROFILE: Poly = Mul(_X, _X)


@dataclass(frozen=True, slots=True)
class FullStream:
    """The full-stream identity the model's contract states.

    Attributes:
        outputs: The output streams, as polynomials over ``[t, x]``, by port.
        lemma: The Lean contract.
        boundary: The contract's boundary predicate, applied to the outputs
            in `statement`.
        streams: The Lean constants for the outputs, in the same order.
        identity_lines: Tactic lines proving each output constant is realized
            by the goal's polynomial (``Streams.Realizes``), so the root ties
            the goal's polynomials to the constants the contract states.
    """

    outputs: tuple[tuple[str, Poly], ...]
    lemma: str
    boundary: str
    streams: tuple[str, ...]
    identity_lines: tuple[str, ...]

    @property
    def statement(self) -> str:
        """The contract's postcondition, as Lean states it."""
        return f"fun y => y = ![{', '.join(self.streams)}] ∧ {self.boundary} y"


@dataclass(frozen=True, slots=True)
class Coefficients:
    """The observed output coefficients and the leaf they pose.

    Attributes:
        observed: ``(port, degrees)`` per observed output coefficient.
        output_ports: The circuit's output ports, in port order.
        output_names: The Lean names of those coefficients, in order.
        input_names: The Lean names of the input halo, in order.
        input_slots: ``(port index, degrees)`` of each halo coefficient.
        input_values: Each halo coefficient's value on the admitted input.
        input_facts: The Lean definitions that compute those values.
        antecedent: The leaf antecedent over the halo, as Lean's constant is.
        hint: Each observed coefficient as a polynomial in the halo; a search
            hint only.
        output: The Lean observation of the outputs.
        input: The Lean observation of the halo.
        antecedent_constant: The Lean constant for ``antecedent``.
        inputs_lemma: The theorem that admitted inputs meet the antecedent.
    """

    observed: tuple[tuple[str, tuple[int, int]], ...]
    output_ports: tuple[str, ...]
    output_names: tuple[str, ...]
    input_names: tuple[str, ...]
    input_slots: tuple[tuple[int, tuple[int, int]], ...]
    input_values: tuple[Fraction, ...]
    input_facts: tuple[str, ...]
    antecedent: Formula
    hint: tuple[Poly, ...]
    output: str
    input: str
    antecedent_constant: str
    inputs_lemma: str


@dataclass(frozen=True, slots=True)
class Field:
    """The evaluated field of the heat solution for the declared profile.

    Attributes:
        names: The claim's coordinate names: the axes, then the value.
        port: The observed output port's index.
        expression: The field as a polynomial over ``[t, x, u]``.
        spec: The Lean ``FieldSpec``.
        expression_constant: The Lean constant for ``expression``.
        field_lemma: The theorem giving the solution's field.
        profile: The declared profile, as the Lean term the theorems name.
        solves: The admitted-input predicate, by basis.
        realized: The theorem that the observed port is realized.
        admitted: The theorem that the constructed input is admitted.
    """

    names: tuple[str, ...]
    port: int
    expression: Poly
    spec: str
    expression_constant: str
    field_lemma: str
    profile: str
    solves: str
    realized: str
    admitted: str


@dataclass(frozen=True, slots=True)
class Tail:
    """The certified-truncation lane of a model.

    Attributes:
        imports: The import set the truncation root is checked with.
        bases: The bases the lane's theorems are stated for.
        viscous: Whether a goal for this lane names a viscosity: the heat
            boundary's lane (``AnalyticHeat.expSum_truncation``) does not, the
            Cole–Hopf front's (``ColeHopf.front_truncation``, the exponential
            sum being the potential's profile) does. A goal is dispatched on
            its own viscosity once `supported` has matched it to the lane.
    """

    imports: tuple[str, ...]
    bases: tuple[str, ...]
    viscous: bool


@dataclass(frozen=True, slots=True)
class Trig:
    """The Euler-radius lanes of a trigonometric stream model.

    Attributes:
        imports: The import set the roots are checked with.
        bases: The bases the lane's theorems are stated for.
        classical_imports: Imports for the explicit classical Euler readings.
    """

    imports: tuple[str, ...]
    bases: tuple[str, ...]
    classical_imports: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamModel:
    """A stream model and everything its lanes cite.

    The three heat lanes (full stream, coefficients, field) come together
    with their imports and circuit constants; the truncation lane is `Tail`.
    A lane the model lacks is ``None``, and the service answers a claim for
    it ``UNKNOWN`` as unsupported. ``summary`` is catalogue prose for the
    agent tool, not consumed by any lane.
    """

    key: str
    axes: tuple[str, ...]
    summary: str
    formal_imports: tuple[str, ...] = ()
    full_stream_imports: tuple[str, ...] = ()
    field_imports: tuple[str, ...] = ()
    formal_circuit: str = ""
    field_circuit: str = ""
    admitted: str = ""
    admitted_input: tuple[str, ...] = ()
    full_stream: FullStream | None = None
    coefficients: Coefficients | None = None
    field: Field | None = None
    tail: Tail | None = None
    trig: Trig | None = None


_T2, _X2 = Var(0), Var(1)
_B, _U = Var(0), Var(1)

HEAT = StreamModel(
    key="formal-heat",
    axes=("t", "x"),
    summary=(
        "profile x*x; output ports u_xx, u; a truncation claim's exponential "
        "sum is the boundary u(0, x)"
    ),
    formal_imports=("Gimle.Forseti.Syntax.Certificate", _EXAMPLE),
    full_stream_imports=(
        "Gimle.Forseti.Syntax.Certificate",
        _EXAMPLE,
        "Gimle.Asgard.Examples.PolynomialHeat",
    ),
    field_imports=("Gimle.Forseti.Syntax.Certificate", _FIELD),
    formal_circuit=f"{_HEAT}.circuit",
    field_circuit="Gimle.Asgard.Streams.Heat.circuit",
    admitted=f"{_CONTRACT}.admitted",
    admitted_input=(f"{_HEAT}.heat", f"{_HEAT}.boundary", "0"),
    full_stream=FullStream(
        outputs=(
            ("u_xx", Const(Fraction(2))),
            ("u", Add(Mul(_X2, _X2), Mul(Const(Fraction(2)), _T2))),
        ),
        lemma=f"{_CONTRACT}.heat_contract",
        boundary=f"Gimle.Forseti.Stream.Boundary 0 1 {_HEAT}.boundary",
        streams=(f"{_HEAT}.two", f"{_HEAT}.heat"),
        identity_lines=(
            "refine ⟨?_, ?_⟩",
            "· unfold Gimle.Asgard.Streams.Realizes",
            "  funext n",
            "  rw [Gimle.Asgard.Streams.ofPoly_ogf_apply]",
            f"  simp only [{_HEAT}.two, MvPolynomial.coeff_C]",
            "  by_cases h : n 0 = 0 ∧ n 1 = 0",
            "  · have zero : n = 0 := by ext i; fin_cases i <;> simp [h.1, h.2]",
            "    simp [h, zero, Rat.mkRat_eq_div]",
            "  · have other : (0 : Fin 2 →₀ ℕ) ≠ n := fun e => h (by subst e; simp)",
            "    simp [h, other]",
            "· rw [Gimle.Asgard.Examples.PolynomialHeat.original_heat]",
            "  unfold Gimle.Asgard.Streams.Realizes Gimle.Asgard.Streams.Heat.stream",
            "  rw [Gimle.Asgard.Examples.PolynomialHeat.square]",
            "  congr 1",
            "  simp only [Rat.mkRat_eq_div]",
            "  norm_num",
            (
                "  rw [show (MvPolynomial.C 2 : Gimle.Asgard.Streams.Poly 2) = 2 "
                "from map_ofNat _ 2]"
            ),
            "  ring",
        ),
    ),
    coefficients=Coefficients(
        observed=(("u", (1, 0)), ("u", (0, 2)), ("u_xx", (0, 0))),
        output_ports=("u_xx", "u"),
        output_names=("u[t^1, x^0]", "u[t^0, x^2]", "u_xx[t^0, x^0]"),
        input_names=("boundary[t^0, x^2]", "u[t^0, x^2]"),
        input_slots=((1, (0, 2)), (0, (0, 2))),
        input_values=(Fraction(1), Fraction(1)),
        input_facts=(f"{_HEAT}.boundary", f"{_HEAT}.heat"),
        antecedent=And(
            _equals(_U, Const(Fraction(1))), _equals(_B, Const(Fraction(1)))
        ),
        hint=(Mul(Const(Fraction(2)), _U), _B, Mul(Const(Fraction(2)), _U)),
        output=f"{_EXAMPLE}.heatOutput",
        input=f"{_EXAMPLE}.heatInput",
        antecedent_constant=f"{_EXAMPLE}.heatAntecedent",
        inputs_lemma=f"{_EXAMPLE}.heat_inputs",
    ),
    field=Field(
        names=("t", "x", "u"),
        port=1,
        expression=Add(Mul(_X2, _X2), Mul(Const(Fraction(2)), _T2)),
        spec=f"{_FIELD}.unitSpec",
        expression_constant=f"{_FIELD}.unitExpr",
        field_lemma=f"{_FIELD}.unit_field",
        profile="(_root_.Polynomial.X ^ 2)",
        solves=f"{_FIELD}.Solves",
        realized=f"{_FIELD}.solution_realized",
        admitted=f"{_FIELD}.solves_solution",
    ),
    tail=Tail(
        imports=("Gimle.Asgard.Streams.AnalyticHeat",),
        bases=("ogf", "egf"),
        viscous=False,
    ),
)

BURGERS = StreamModel(
    key="burgers",
    axes=("t", "x"),
    summary=(
        "viscosity ν > 0; truncation claims (interpretation analytic-tail) and "
        "field bands `field u over t in [-r, r], x in [-s, s] : u >= lo and u <= "
        "hi`, one side or both (interpretation analytic-field), about the output "
        "u only, in basis ogf; the exponential sum is the Cole–Hopf potential "
        "φ(0, x), with Σ cᵢ ≠ 0, and the stream is the front −2ν φ_x/φ "
        "reconstructed from its own t = 0 slice"
    ),
    tail=Tail(imports=("Gimle.Asgard.Streams.ColeHopf",), bases=("ogf",), viscous=True),
)

VORTICITY = StreamModel(
    key="vorticity",
    axes=("t",),
    summary=(
        "viscosity ν ≥ 0, ν = 0 the Euler equation; the start is a list of modes "
        "`start modes [k1, k2, c], …` meaning Σ c·e^{i k·x} on the torus; "
        "truncation claims `truncation "
        "u window "
        "N radius r epsilon ε` (interpretation trig-truncation) and bands `band u "
        "radius r : u >= lo and u <= hi` (interpretation trig-band) about the sum of "
        "the field's "
        "t-series on |t| ≤ r, every x, in basis ogf. Explicit euler-truncation and "
        "euler-band readings also certify the classical vorticity equation, "
        "initial values and invariants for mean-zero, even starts; the trig "
        "readings only certify the real cosine field's convergence and bound. "
        "Zero data admit any finite radius in the classical readings; "
        "ν > 0 is UNKNOWN, since no Taylor radius "
        "is certified for it; a band is never refuted here. "
        "The mild-truncation and mild-band readings use basis interaction and "
        "time T on [0,T], at every ν ≥ 0, and check evenness and zero mean. "
        "Both physical bounds retain the full typed mild circuit contract, "
        "classical realization on the interior and spectral nonincrease on [0,T]"
    ),
    trig=Trig(
        imports=(
            "Gimle.Forseti.Fourier",
            "Gimle.Asgard.Streams.EulerRadius",
            "Gimle.Asgard.Streams.VorticityTable",
        ),
        bases=("ogf",),
        classical_imports=("Gimle.Forseti.Euler",),
    ),
)

MODELS: dict[str, StreamModel] = {
    HEAT.key: HEAT,
    BURGERS.key: BURGERS,
    VORTICITY.key: VORTICITY,
}
#: The coefficient lane's rules.
RULES = _RULES


def replay_imports() -> tuple[tuple[str, ...], ...]:
    """Every import set a stream or field replay uses, for the replay pin."""
    found: set[tuple[str, ...]] = {
        ("Gimle.Forseti.MildVorticity",),
    }
    for model in MODELS.values():
        found |= {model.formal_imports, model.full_stream_imports, model.field_imports}
        if model.tail is not None:
            found.add(model.tail.imports)
        if model.trig is not None:
            found.add(model.trig.imports)
            found.add(model.trig.classical_imports)
    found.discard(())
    return tuple(sorted(found))
