"""State a decided claim as the property it is about, and bridge to the root.

A decider proves its own statement over generated definitions: ``forsetiRoot``
for a trajectory or stream goal, ``FormulaEntailment P Q`` for an entailment,
with ``P`` and ``Q`` formula syntax. A reader of the notebook cannot see from
that what was proved. This module renders, from the same parsed goal and the
same model registry the lane's prelude is rendered from, the claim written as
ordinary Lean about real numbers and the compiled circuit, and a *bridge*: a
proof that the claim is equivalent to the lane's proposition (``forsetiContract``
or ``FormulaEntailment P Q``). A proof of the claim is the lane's proof
followed by the bridge's backward direction; a refutation is the lane's
refutation followed by its forward one. Both directions are checked in every
block, so a claim never says less or more than the decided proposition.

Nothing here is trusted. The readable statement is what the notebook checks,
and Lean refuses the claim if the bridge does not connect the two. The bridge
never restates the claim: it is posed as ``_ ↔ lane`` and Lean fills the left
side from the goal. It unfolds the lane's definitions with a fixed ``simp only``
set: the formula semantics, one equation per rational constant from its
``mkRat`` cast to the numeral the statement shows, and the rewrites that turn
``p + -q ⋈ 0`` into ``p ⋈ q``. Both sides then have the same normal form, so
the closing step is syntactic or definitional, never a search.

Formulas are rendered structurally, the way `claims.text` writes them: an
atom whose polynomial is ``p + -q`` is ``p ⋈ q``, any other is ``p ⋈ 0``.
Coordinates are the goal's own names.

Stream claims are written the same way. A coefficient claim binds
each coefficient it reads to the output stream's own entry; a full-stream claim
says which polynomial each output realizes, over the axes as polynomials, and
its bridge rewrites the contract's output constants with the identities the
root proves beside it; a truncation claim states convergence and the bound at
every real point of the box. Nothing in them is assumed: each bridge proves
the statement equivalent to the lane's proposition.

A stepped claim is already a statement about the model's Lean constants, for
every shock law of its class; the lane's ``forsetiClaim`` is that very
proposition, so its bridge is definitional. What it adds is the sentence that
says the claim in words.

A hedging claim is likewise a statement about the library's own definitions,
`Covers` and the hedged wealth over a band with rational ends, and is the
lane's ``forsetiClaim``. The exception is a refuted ``covers`` claim: its
refutation proves that no spot-feedback strategy covers the payoff from the
premium, ``∀ δ, ¬ Covers …``, which is more than the band's own hedge failing.
Its claim is stated as ``∃ δ, Covers …``, so the block's ``¬ (claim)`` says all
of that, and its bridge reads the negation off the lane's statement with
``not_exists``. Its sentence reads the claim as a sale, with the financial
assumptions the library makes (zero interest, no costs, a self-financing
account rebalanced at each step, a European payoff on the final spot), and
writes numbers decimal first with the exact value beside. The figures a desk
reads after the verdict (`hedging_figures`) are the binomial price for a
``covers`` claim, and for a refuted ``shortfall`` claim the path the block
exhibits and its exact shortfall, recomputed from the goal and that path, and
checked against the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import NamedTuple, TypedDict, TypeVar

from glitnir_forseti.entailment.render import rational
from glitnir_forseti.entailment.syntax import (
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
    Relation,
    Tru,
    Var,
    walk,
)
from glitnir_forseti.figures import exact, percent, rounded_up, stated
from glitnir_forseti.hedging import numbers as hedging_numbers
from glitnir_forseti.hedging import render as hedging_render
from glitnir_forseti.hedging.goal import HedgingGoal
from glitnir_forseti.hedging.payoffs import PAYOFFS as HEDGING_PAYOFFS
from glitnir_forseti.hedging.payoffs import Payoff as HedgingPayoff
from glitnir_forseti.stepped import render as stepped_render
from glitnir_forseti.stepped.goal import SteppedGoal
from glitnir_forseti.stepped.models import MODELS as STEPPED_MODELS
from glitnir_forseti.streams.goal import (
    CLASSICAL_EULER,
    FIELD_CONTEXT,
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    FullStreamClaim,
    MildBandClaim,
    MildTruncationClaim,
    Modes,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    band_of,
    coefficient_context,
)
from glitnir_forseti.streams.models import MODELS as STREAM_MODELS
from glitnir_forseti.streams.render import (
    ANALYTIC_HEAT,
    COLE_HOPF,
    exp_sum_terms,
    field_domain,
)
from glitnir_forseti.trajectory.goal import TrajectoryGoal
from glitnir_forseti.trajectory.models import MODELS as TRAJECTORY_MODELS
from glitnir_forseti.trajectory.render import initial_point

_SYNTAX = "Gimle.Forseti.Syntax"
_TRAJECTORY = "Gimle.Forseti.Trajectory"
_DYNAMICS = "Gimle.Asgard.Dynamics"
_STREAMS = "Gimle.Asgard.Streams"
_STREAM = "Gimle.Forseti.Stream"

_SYMBOLS = {
    Relation.EQ: "=",
    Relation.LE: "≤",
    Relation.LT: "<",
    Relation.GE: "≥",
    Relation.GT: ">",
}
#: Turn the semantics' ``p + -q ⋈ 0`` into ``p ⋈ q``, ``≥``/``>`` into
#: ``≤``/``<``, and chains of ∧ and ∨ to the right, on both sides alike.
_ARITHMETIC = (
    "← sub_eq_add_neg",
    "sub_zero",
    "sub_nonpos",
    "sub_neg",
    "sub_eq_zero",
    "sub_nonneg",
    "sub_pos",
    "ge_iff_le",
    "gt_iff_lt",
    "and_assoc",
    "or_assoc",
)
_FORMULA = (
    f"{_SYNTAX}.Formula.holds",
    f"{_SYNTAX}.Relation.holds",
    "Gimle.Asgard.Polynomial.Expr.eval",
)
#: Statements longer than this are broken over lines.
_WIDTH = 90
#: Lean words a coordinate name must not be written as; such a name is
#: written in «guillemets», which Lean reads as the same identifier.
_KEYWORDS = frozenset(
    """at by calc do else end exists extends forall fun have if import in
    infix infixl infixr instance let local match mut namespace notation open
    prefix private protected return section show sorry suffices then this
    universe where with deriving from Type Prop Sort using for try catch
    finally unless break continue def theorem lemma example abbrev class
    structure inductive variable attribute export macro macro_rules syntax
    set_option nomatch nofun termination_by decreasing_by noncomputable
    partial unsafe opaque axiom mutual omit include elab builtin_initialize
    initialize deriving""".split()
)


@dataclass(frozen=True)
class Readable:
    """A claim written as its property, and its equivalence with the lane's.

    Attributes:
        statement: The claim's statement, from its colon on, possibly over
            several lines.
        description: A sentence for the claim's docstring saying what the
            statement's names are.
        lane: The lane's proposition the claim is equivalent to. For a
            refuted hedging ``covers`` claim it is the ``∃ δ, Covers …`` claim
            itself, whose negation `refuted` reads off the lane's statement.
        root: The term for ``lane`` given the lane's proof as ``root``.
        refuted: The term for ``¬ lane`` given its refutation as ``refuted``.
        setup: Tactic lines both directions share (named equations).
        to_lane: Tactic lines proving ``lane`` from ``claim``, the statement.
        from_lane: Tactic lines proving the statement from ``lane``.
        identities: Whether the bridge uses ``forsetiIdentities``, which a
            rooted lane proves beside its contract; it is then taken, as
            ``identities``, from ``root`` or ``refuted`` before ``setup``.
    """

    statement: str
    description: str
    lane: str
    root: str
    refuted: str
    setup: tuple[str, ...]
    to_lane: tuple[str, ...]
    from_lane: tuple[str, ...]
    identities: bool = False

    def _bridge(self, use: str, identities: str) -> tuple[str, ...]:
        given = (
            (f"have identities : forsetiIdentities := {identities}",)
            if self.identities
            else ()
        )
        return (
            *given,
            *self.setup,
            f"refine (fun (bridge : _ ↔ {self.lane}) => {use}) ?_",
            "constructor",
            "· intro claim",
            *("  " + line for line in self.to_lane),
            "· intro lane",
            *("  " + line for line in self.from_lane),
        )

    @property
    def from_root(self) -> tuple[str, ...]:
        """Prove the statement, given the lane's proof as ``root``."""
        return self._bridge(
            f"bridge.mpr {self.root}",
            "(show forsetiIdentities ∧ forsetiContract from root).1",
        )

    @property
    def to_root(self) -> tuple[str, ...]:
        """Close ``False``, given ``refuted`` and the statement as ``claim``."""
        return self._bridge(f"{self.refuted} (bridge.mp claim)", "refuted.1")


def readable(goal: object, *, refuted: bool = False) -> Readable | None:
    """Render ``goal``'s readable statement and bridges, if it has a form.

    Returns ``None`` for a goal of no kind this module knows, whose lane
    statement is then kept as it is. ``refuted`` asks for the claim a
    refutation is stated against; it is the claim itself for every goal but a
    hedging ``covers`` goal, whose refutation speaks of every spot-feedback
    strategy and so is stated against ``∃ δ, Covers …``.

    Raises:
        KeyError: If a trajectory, stream or stepped goal names an unknown
            model, a stepped goal an observation its model lacks, or a
            hedging goal an unknown payoff.
        ValueError: If a trajectory goal's initial ports are not its model's,
            or a coefficient claim observes a port the model's circuit lacks,
            or a truncation goal's profile is not an exponential sum, or the
            stream model has no lane for the claim.
    """
    if type(goal) is Goal:
        return _entailment(goal)
    if type(goal) is TrajectoryGoal:
        return _trajectory(goal)
    if type(goal) is SteppedGoal:
        return _stepped(goal)
    if type(goal) is HedgingGoal:
        return _hedging(goal, refuted)
    if type(goal) is StreamGoal:
        claim = goal.claim
        if type(claim) is FieldClaim and goal.interpretation == "analytic-field":
            return _analytic_field(goal, claim)
        if type(claim) is FieldClaim:
            return _field(goal, claim)
        if type(claim) is CoefficientClaim:
            return _coefficients(goal, claim)
        if type(claim) is FullStreamClaim:
            return _full_stream(goal, claim)
        if type(claim) is TruncationClaim:
            return _truncation(goal, claim)
        if isinstance(claim, (MildTruncationClaim, MildBandClaim)):
            return _mild(goal, claim)
        if isinstance(claim, (TrigTruncationClaim, TrigBandClaim)):
            return _trig(goal, claim)
    return None


# --- rendering ----------------------------------------------------------------


def identifier(name: str) -> str:
    """Write a coordinate name as a Lean identifier."""
    return f"«{name}»" if name in _KEYWORDS else name


_Lane = TypeVar("_Lane")


def _lane(lane: _Lane | None, model: str, name: str) -> _Lane:
    """Return a model's lane, or refuse a claim about a lane it lacks."""
    if lane is None:
        raise ValueError(f"the stream model {model!r} has no {name} lane")
    return lane


def number(value: Fraction) -> str:
    """Write an exact rational as a real numeral: ``2``, ``-1``, ``1/3``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _constant(value: Fraction) -> str:
    """Write a numeral as an operand, a fraction in parentheses."""
    text = number(value)
    return f"({text})" if value.denominator != 1 else text


def _argument(value: Fraction) -> str:
    """Write a numeral as a function's argument: ``2``, ``(1/3)``, ``(-1)``."""
    return f"({number(value)})" if value < 0 else _constant(value)


def poly(value: Poly, names: tuple[str, ...], stream: bool = False) -> str:
    """Write a polynomial as real arithmetic over ``names``.

    With ``stream``, it is a ``Streams.Poly``, which has no division: a
    fraction is written as the constant polynomial ``MvPolynomial.C (1/3)``.
    """
    return _poly(value, names, 0, stream)


# Levels: 0 sum, 1 product, 2 operand of a unary minus or a product's right,
# 3 a function's argument.
def _poly(value: Poly, names: tuple[str, ...], level: int, stream: bool) -> str:
    if type(value) is Var:
        return identifier(names[value.index])
    if type(value) is Const and stream and value.value.denominator != 1:
        text = f"MvPolynomial.C ({number(value.value)})"
        return f"({text})" if level > 2 else text
    if type(value) is Const:
        # `x * 1/3` would read as `(x * 1) / 3`, and `x - -1` as a typo.
        fraction = value.value.denominator != 1 and level >= 2
        negative = value.value < 0 and level > 0
        text = number(value.value)
        return f"({text})" if fraction or negative else text
    if type(value) is Neg:
        text = "-" + _poly(value.argument, names, 2, stream)
        return f"({text})" if level > 0 else text
    if type(value) is Add:
        if type(value.right) is Neg:
            text = (
                f"{_poly(value.left, names, 0, stream)} - "
                f"{_poly(value.right.argument, names, 1, stream)}"
            )
        else:
            text = (
                f"{_poly(value.left, names, 0, stream)} + "
                f"{_poly(value.right, names, 1, stream)}"
            )
        return f"({text})" if level > 0 else text
    if type(value) is Mul:
        text = (
            f"{_poly(value.left, names, 1, stream)} * "
            f"{_poly(value.right, names, 2, stream)}"
        )
        return f"({text})" if level > 1 else text
    raise TypeError(f"not a polynomial: {value!r}")


def formula(value: Formula, names: tuple[str, ...]) -> str:
    """Write a formula as a proposition about reals over ``names``."""
    return _formula(value, names, 0)


# Levels: 0 disjunction, 1 conjunction, 2 negation or atom. A chain of one
# connective is written flat whatever its grouping: Lean reads it to the right,
# and the bridge regroups both sides with `and_assoc` and `or_assoc`.
def _formula(value: Formula, names: tuple[str, ...], level: int) -> str:
    if type(value) is Tru:
        return "True"
    if type(value) is Fls:
        return "False"
    if type(value) is Not:
        return "¬" + _formula(value.argument, names, 3)
    if isinstance(value, (And, Or)):
        own = 1 if type(value) is And else 0
        symbol = "∧" if own else "∨"
        text = (
            f"{_formula(value.left, names, own)} {symbol} "
            f"{_formula(value.right, names, own)}"
        )
        return f"({text})" if level > own else text
    if type(value) is Atom:
        text = _atom(value, names)
        return f"({text})" if level > 2 else text
    raise TypeError(f"not a formula: {value!r}")


def _atom(value: Atom, names: tuple[str, ...]) -> str:
    symbol = _SYMBOLS[value.relation]
    polynomial = value.polynomial
    if type(polynomial) is Add and type(polynomial.right) is Neg:
        left, right = polynomial.left, polynomial.right.argument
    else:
        left, right = polynomial, Const(Fraction(0))
    lhs = poly(left, names)
    if not _has_variable(left) and not _has_variable(right):
        # A comparison of numerals alone would otherwise be read over ℕ.
        lhs = f"({lhs} : ℝ)"
    return f"{lhs} {symbol} {poly(right, names)}"


def _has_variable(value: Poly) -> bool:
    return any(type(node) is Var for node in walk(value))


def _constants(*values: Formula | Poly) -> list[Fraction]:
    """Every distinct rational constant, in first-seen order."""
    seen: dict[Fraction, None] = {}
    for value in values:
        for node in walk(value):
            if type(node) is Const:
                seen.setdefault(node.value, None)
    return list(seen)


def _casts(values: list[Fraction]) -> tuple[list[str], list[str]]:
    """One equation per constant, from its ``mkRat`` cast to its numeral.

    Returns the ``have`` lines and the names to hand to ``simp only``.
    """
    lines, names = [], []
    for index, value in enumerate(values):
        name = f"forsetiCast{index}"
        lines.append(
            f"have {name} : (({rational(value)} : ℚ) : ℝ) = {_constant(value)} := "
            "by norm_num [Rat.mkRat_eq_div]"
        )
        names.append(name)
    return lines, names


def _simp(names: list[str] | tuple[str, ...], where: str = "") -> str:
    """One ``simp only`` over a fixed set; not every lemma fires for every goal."""
    at = f" at {where}" if where else ""
    return (
        "set_option linter.unusedSimpArgs false in "
        f"simp only [{', '.join(names)}]{at}"
    )


# --- entailment ---------------------------------------------------------------

#: ``⟨k + 1, h⟩ = Fin.succ ⟨k, _⟩``, so ``Fin.cons`` reads a coordinate.
_SUCC = (
    "have forsetiSucc : ∀ {n : Nat} (k : Nat) (h : k + 1 < n + 1), "
    "(⟨k + 1, h⟩ : Fin (n + 1)) = Fin.succ ⟨k, Nat.lt_of_succ_lt_succ h⟩ := "
    "fun _ _ => rfl"
)


def _entailment(goal: Goal) -> Readable:
    names = goal.context.names
    binders = " ".join(identifier(name) for name in names)
    antecedent = formula(goal.antecedent, names)
    consequent = formula(goal.consequent, names)
    statement = f":\n    ∀ {binders} : ℝ, {antecedent} → {consequent}"
    if len(statement) > _WIDTH:
        statement = f":\n    ∀ {binders} : ℝ,\n      {antecedent} →\n      {consequent}"
    casts, cast_names = _casts(_constants(goal.antecedent, goal.consequent))
    unfold = [
        f"{_SYNTAX}.FormulaEntailment",
        "forsetiAntecedent",
        "forsetiConsequent",
        *_FORMULA,
        "Fin.forall_fin_succ_pi",
        "Fin.forall_fin_zero_pi",
        "forsetiSucc",
        "Fin.cons_succ",
        "Fin.zero_eta",
        "Fin.cons_zero",
        *cast_names,
        *_ARITHMETIC,
        "imp_self",
    ]
    kind = "is a real number" if len(names) == 1 else "are real numbers"
    return Readable(
        statement,
        f"{', '.join(names)} {kind}.",
        lane=f"{_SYNTAX}.FormulaEntailment forsetiAntecedent forsetiConsequent",
        root="root",
        refuted="refuted",
        setup=(_SUCC, *casts),
        to_lane=("revert claim", _simp(unfold)),
        from_lane=("revert lane", _simp(unfold)),
    )


# --- trajectory ---------------------------------------------------------------


class _Rooted(TypedDict):
    lane: str
    root: str
    refuted: str


#: A rooted lane proves ``forsetiIdentities ∧ forsetiContract``; the claim is
#: equivalent to the contract, and the identities are proved alongside it.
_ROOTED: _Rooted = {
    "lane": "forsetiContract",
    "root": "(show forsetiIdentities ∧ forsetiContract from root).2",
    "refuted": "refuted.2",
}


def _trajectory(goal: TrajectoryGoal) -> Readable:
    entry = TRAJECTORY_MODELS[goal.model]
    band = entry.observations[goal.observation]
    point = initial_point(goal, entry)
    start = number(goal.start)
    name = identifier(goal.name)
    output = f"{_DYNAMICS}.Signal {entry.outputs}"
    drivers = entry.drivers
    # An undriven model's admitted inputs are the initial wires alone
    # (`Initialized … (fun _ => True)`); a driven model's input carries its
    # driver wires first, and the claim states the precondition the goal
    # names: admitted by asgard, and the bounded driver inside the band.
    width = 0 if drivers is None else len(drivers.ports)
    premises = ""
    bound: Fraction | None = None
    if drivers is not None:
        ((_, bound),) = goal.drivers
        premises = (
            f"      {entry.evolution}.Admitted {drivers.bindings}\n"
            f"        ({_DYNAMICS}.signalLeft u) →\n"
            f"      (∀ t ≥ {start}, |{_DYNAMICS}.signalLeft u t {drivers.index}| ≤ "
            f"{_constant(bound)}) →\n"
        )
    statement = (
        ":\n"
        f"    have circuit := {entry.circuit}\n"
        f'    have time : {_DYNAMICS}.TimeDomain := ⟨"{goal.axis}", {start}⟩\n'
        f"    ∀ u : {_DYNAMICS}.Signal ({width} + {entry.states}),\n"
        f"{premises}"
        f"      {_DYNAMICS}.signalRight u {_argument(goal.start)} = "
        f"![{', '.join(number(v) for v in point)}] →\n"
        f"      (∃ y : {output}, circuit.Rel time u y) ∧\n"
        f"      (∀ y y' : {output}, circuit.Rel time u y → circuit.Rel time u y' →\n"
        f"        ∀ t ≥ {start}, y t = y' t) ∧\n"
        f"      (∀ y : {output}, circuit.Rel time u y →\n"
        f"        ∀ t ≥ {start}, have {name} := y t {band.index}; "
        f"{formula(goal.post, (goal.name,))})"
    )
    ports = ", ".join(entry.initial_ids)
    description = (
        f"{goal.name} is output {band.index} of the compiled circuit, the port "
        f"{goal.observation}; the input's initial wires ({ports}) start at "
        f"({', '.join(number(v) for v in point)}) at {goal.axis} = {start}."
    )
    if drivers is not None and bound is not None:
        description += (
            f" The input's driver wires ({', '.join(drivers.ports)}) are admitted, "
            f"with |{drivers.bounded}| ≤ {number(bound)}."
        )
    constants = [goal.post, Const(goal.start), *map(Const, point)]
    if bound is not None:
        constants.append(Const(bound))
    casts, cast_names = _casts(_constants(*constants))
    unfold = [
        f"{_TRAJECTORY}.Realizable",
        f"{_TRAJECTORY}.Unique",
        f"{_TRAJECTORY}.Holds",
        f"{_TRAJECTORY}.Initialized",
        f"{_TRAJECTORY}.Always",
        "Set.EqOn",
        f"{_DYNAMICS}.TimeDomain.domain",
        "Set.mem_Ici",
        "forsetiTime",
        "forsetiInitial",
        "forsetiPost",
        "forsetiObservation",
        *(("forsetiDrivers",) if drivers is not None else ()),
        *_FORMULA,
        "true_and",
        *cast_names,
        *_ARITHMETIC,
    ]
    # The lane's precondition is one conjunction, `and_assoc` above puts it to
    # the right; the statement takes the same parts as separate premises.
    if drivers is None:
        given, taken = "start", "start"
    else:
        given, taken = "⟨admit, band, start⟩", "admit band start"
    # The closing steps are definitional: the output index is `3 : Fin 4` in
    # the statement and `⟨3, _⟩` in the lane's, over types equal by `decide`;
    # so are the driver's width and index.
    return Readable(
        statement,
        description,
        setup=tuple(casts),
        to_lane=(
            "unfold forsetiContract",
            _simp(unfold, "claim"),
            f"refine ⟨?_, ?_, ?_⟩ <;> {_simp(unfold)}",
            f"· exact fun u {given} => (claim u {taken}).1",
            f"· exact fun u {given} => (claim u {taken}).2.1",
            f"· exact fun u {given} => (claim u {taken}).2.2",
        ),
        from_lane=(
            "unfold forsetiContract at lane",
            "obtain ⟨realizable, unique, holds⟩ := lane",
            _simp(unfold, "realizable unique holds ⊢"),
            f"exact fun u {taken} => "
            f"⟨realizable u {given}, unique u {given}, holds u {given}⟩",
        ),
        **_ROOTED,
    )


# --- stepped ------------------------------------------------------------------


def _stepped(goal: SteppedGoal) -> Readable:
    """State a stepped claim as the lane's proposition, with what it says."""
    entry = STEPPED_MODELS[goal.model]
    observation = entry.observations[goal.observation]
    if goal.law == "two-moment":
        shocks = (
            "independent, identically distributed shocks drawn from any centred law"
            f" with variance {number(goal.variance)}"
        )
    else:
        shocks = (
            "independent, identically distributed shocks drawn from any sub-Gaussian"
            f" law with proxy {number(goal.variance)}"
        )
    quantity = f"{observation.kind} {goal.observation}"
    bound = number(goal.bound)
    if goal.barrier is None:
        said = f"the expected {quantity} at mark {goal.horizon} is at most {bound}"
    elif goal.claim == "horizon":
        said = (
            f"the probability that the {quantity} breaches {number(goal.barrier)}"
            f" within {goal.horizon} marks is at most {bound}"
        )
    else:
        said = (
            f"at each mark up to {goal.horizon}, taken separately, the probability"
            f" that the {quantity} breaches {number(goal.barrier)} is at most {bound}"
        )
    step = stepped_render.short(entry, entry.step_name)
    start = stepped_render.short(entry, entry.initial_name)
    names = (
        f"`{step}` is the model's step of size {number(entry.delta)} with the shock"
        f" in its driver slot, from `{start}` = "
        f"({', '.join(number(value) for value in entry.initial)})"
    )
    if goal.barrier is not None:
        safe = stepped_render.short(entry, observation.lean_name)
        names += (
            f"; `{safe} {number(goal.barrier)}` is the band"
            f" |{goal.observation}| ≤ {number(goal.barrier)}"
        )
    return Readable(
        ":\n    " + stepped_render.claim(goal, entry, "      "),
        f"For {shocks}, {said}. {names}.",
        lane=stepped_render.CLAIM,
        root="root",
        refuted="refuted",
        setup=(),
        to_lane=("exact claim",),
        from_lane=("exact lane",),
    )


# --- hedging ------------------------------------------------------------------


def _hedging(goal: HedgingGoal, refuted: bool) -> Readable:
    """State a hedging claim as the lane's proposition, with what it says.

    The sentence reads the claim as a sale, with the library's assumptions:
    the seller takes in the premium and holds the hedge in a self-financing
    account, rebalanced at each step with zero interest and no costs, against
    a European payoff on the final spot.
    """
    payoff = HEDGING_PAYOFFS[goal.payoff]
    paid = f"the payoff of the {goal.payoff} struck at {stated(goal.strike)}"
    rebalanced = (
        f"rebalanced at each of {goal.steps} steps with zero interest and no costs"
    )
    if goal.claim == "covers":
        assert goal.premium is not None
        premium = stated(goal.premium)
        paths = _paths(goal, goal.down, goal.up)
        if refuted:
            some = hedging_render.some_cover_claim(goal, payoff, "      ")
            return Readable(
                ":\n    " + some,
                "Some spot-feedback strategy, holding over each step a number of"
                " units set by the step and the spot, keeps the seller's account"
                f" at least at {paid} {paths}, from a premium of {premium},"
                f" {rebalanced}.",
                lane=" ".join(line.strip() for line in some.split("\n")),
                root="root",
                # The lane proves `∀ δ, ¬ Covers …`; `not_exists` makes it `¬ lane`.
                refuted="(not_exists.mpr refuted)",
                setup=(),
                to_lane=("exact claim",),
                from_lane=("exact lane",),
            )
        said = (
            f"Sold for a premium of {premium} and hedged at the band's binomial"
            f" hedge ratio, {rebalanced}, the seller's account ends at least at"
            f" {paid} {paths}."
        )
        statement = hedging_render.covers_claim(goal, payoff, "      ")
    else:
        assert goal.bound is not None
        bound = goal.bound
        if bound > 0:
            ends = f"ends at most {stated(bound)} below {paid}"
        elif bound < 0:
            ends = f"ends at least {stated(-bound)} above {paid}"
        else:
            ends = f"ends at least at {paid}"
        assert goal.against_up is not None and goal.against_down is not None
        said = (
            f"The hedge built for returns within {_returns(goal.down, goal.up)} a"
            f" step, started from its own price and {rebalanced}, {ends} (the"
            f" seller's account less the payoff is at least {exact(-bound)})"
            f" {_paths(goal, goal.against_down, goal.against_up)}."
        )
        statement = hedging_render.shortfall_claim(goal, payoff, "      ")
    return Readable(
        ":\n    " + statement,
        said,
        lane=hedging_render.CLAIM,
        root="root",
        refuted="refuted",
        setup=(),
        to_lane=("exact claim",),
        from_lane=("exact lane",),
    )


def hedging_figures(
    goal: HedgingGoal, status: str, returns: tuple[Fraction, ...] | None = None
) -> str:
    """State the figures behind a hedging verdict, recomputed from the goal.

    A ``covers`` verdict gives the binomial price at the band, with, for a
    proof, the price rounded up to four places as a premium that covers. A
    refuted ``shortfall`` claim names the path its block exhibits, whose
    ``returns`` its prelude lists, and that path's exact shortfall. Any other
    verdict has none, and the result is empty.

    Raises:
        KeyError: If the goal names an unknown payoff.
        ValueError: If the verdict disagrees with the figures: a proved
            premium below the binomial price, or a refuted one at least it; or
            a refuted ``shortfall`` claim with no path of its steps, with a
            return off the against band's ends, or whose path does not break
            the bound.
    """
    payoff = HEDGING_PAYOFFS[goal.payoff]
    if goal.claim == "covers":
        assert goal.premium is not None
        fair = hedging_numbers.price(
            payoff, goal.strike, goal.spot, goal.up, goal.down, goal.steps
        )
        if (fair > goal.premium) != (status == "REFUTED"):
            raise ValueError(
                f"a {status} premium of {stated(goal.premium)} disagrees with the"
                f" binomial price {stated(fair)}"
            )
        if status == "REFUTED":
            return (
                "The premium is below the binomial price at the band,"
                f" {stated(fair)}, the least premium from which any spot-feedback"
                " strategy covers the payoff."
            )
        up = rounded_up(fair)
        covering = (
            f", {up} (the price rounded up to four places) among them" if up else ""
        )
        return (
            f"The binomial price at the band is {stated(fair)}; any premium of at"
            f" least the price covers the payoff{covering}."
        )
    if status != "REFUTED":
        return ""
    return _exhibited(goal, payoff, returns)


def _returns(down: Fraction, up: Fraction) -> str:
    """Write a band's returns: ``−2 % and +2 % (−1/50 and +1/50)``."""
    return (
        f"{percent(-down)} and {percent(up)} ({exact(-down)} and"
        f" {exact(up, plus=True)})"
    )


def _paths(goal: HedgingGoal, down: Fraction, up: Fraction) -> str:
    """Write the paths a claim speaks of: from the spot, each return in a band."""
    return (
        f"on every path from a spot of {stated(goal.spot)} whose simple returns"
        f" stay within {_returns(down, up)} a step"
    )


def _exhibited(
    goal: HedgingGoal, payoff: HedgingPayoff, returns: tuple[Fraction, ...] | None
) -> str:
    """Name the path a ``shortfall`` refutation exhibits, and its exact shortfall.

    The shortfall is recomputed from the goal and the path, exactly.

    Raises:
        ValueError: If there is no path of the goal's steps, a return is not
            an end of the against band, or the path does not break the bound.
    """
    assert goal.against_up is not None and goal.against_down is not None
    assert goal.bound is not None
    if returns is None or len(returns) != goal.steps:
        raise ValueError("a shortfall refutation exhibits a path of every step")
    if any(r not in (goal.against_up, -goal.against_down) for r in returns):
        raise ValueError("the exhibited path leaves the against band's ends")
    wealth, final = hedging_numbers.wealth(
        payoff, goal.strike, goal.spot, goal.up, goal.down, goal.steps, returns
    )
    shortfall = wealth - payoff.value(goal.strike, final)
    if not shortfall < -goal.bound:
        raise ValueError(
            f"the exhibited path ends at {stated(shortfall)}, which does not"
            f" break the bound {exact(goal.bound)}"
        )
    path = hedging_render.path_words(
        [r > 0 for r in returns], goal.against_up, goal.against_down
    )
    return (
        f"On {path}, the seller's account less the payoff ends at"
        f" {stated(shortfall)}, below {exact(-goal.bound)}."
    )


# --- stream field -------------------------------------------------------------


def _field(goal: StreamGoal, claim: FieldClaim) -> Readable:
    model = STREAM_MODELS[goal.model]
    f = _lane(model.field, model.key, "field")
    names = FIELD_CONTEXT.names
    circuit = f"({model.field_circuit} .{goal.basis})"
    t, x, u = (identifier(name) for name in names)
    domain = field_domain(claim)
    statement = (
        ":\n"
        f"    ∀ input, {f.solves} .{goal.basis} {f.profile} input →\n"
        f"      {circuit}.Defined input ∧\n"
        f"      ∃ q, {_STREAMS}.Realizes .{goal.basis} "
        f"({circuit}.value input {f.port}) q ∧\n"
        f"        ∀ {t} {x} : ℝ, {formula(domain, names)} →\n"
        f"          have {u} := {_STREAMS}.field q ![{t}, {x}]; "
        f"{formula(claim.post, names)}"
    )
    description = (
        f"{claim.port} is output {f.port} of the heat circuit; q is the "
        f"polynomial that realizes it and {names[2]} its field at ({t}, {x})."
    )
    casts, cast_names = _casts(_constants(domain, claim.post))
    point = f"{f.spec}.fieldPoint"
    coordinates = (
        f"have forsetiT : ∀ z q p, {point} z ![] q ⟨0, p⟩ = z 0 := fun _ _ _ => rfl",
        f"have forsetiX : ∀ z q p, {point} z ![] q ⟨1, p⟩ = z 1 := fun _ _ _ => rfl",
        f"have forsetiU : ∀ z q p, {point} z ![] q ⟨2, p⟩ = "
        f"{_STREAMS}.field q z := fun _ _ _ => rfl",
    )
    unfold = [
        "forsetiDomain",
        "forsetiPost",
        *_FORMULA,
        "forsetiT",
        "forsetiX",
        "forsetiU",
        "Matrix.cons_val_zero",
        "Matrix.cons_val_one",
        "Matrix.head_cons",
        *cast_names,
        *_ARITHMETIC,
    ]
    return Readable(
        statement,
        description,
        setup=(*coordinates, *casts),
        to_lane=(
            "intro input admitted",
            "obtain ⟨defined, q, realized, bound⟩ := claim input admitted",
            "refine ⟨defined, q, realized, fun z inside => ?_⟩",
            "have holds := bound (z 0) (z 1)",
            "rw [show ![z 0, z 1] = z from by funext i; fin_cases i <;> rfl] at holds",
            _simp(unfold, "holds inside ⊢"),
            "exact holds inside",
        ),
        from_lane=(
            "intro input admitted",
            "obtain ⟨defined, q, realized, bound⟩ := lane input admitted",
            "refine ⟨defined, q, realized, fun t x inside => ?_⟩",
            "have holds := bound ![t, x]",
            _simp(unfold, "holds inside ⊢"),
            "exact holds inside",
        ),
        **_ROOTED,
    )


# --- stream coefficients ------------------------------------------------------

_TO_INDEX = f"{_STREAMS}.Lowering.Degrees.toIndex"


def _variables(*values: Formula | Poly) -> list[int]:
    """Return the coordinates the values read, in order."""
    return sorted(
        {node.index for value in values for node in walk(value) if type(node) is Var}
    )


def _listed(items: list[str]) -> str:
    """Join as ``a``, ``a and b``, ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _coefficients(goal: StreamGoal, claim: CoefficientClaim) -> Readable:
    model = STREAM_MODELS[goal.model]
    c = _lane(model.coefficients, model.key, "coefficient")
    names = coefficient_context(len(claim.observed)).names
    entries = [
        f"y {c.output_ports.index(port)} ({_TO_INDEX} ![{t}, {x}])"
        for port, (t, x) in claim.observed
    ]
    read = _variables(claim.post)
    # Only what the post reads is bound: Lean warns about an unused `have`.
    bound = "".join(
        f"        have {identifier(names[i])} : ℝ := {entries[i]}\n" for i in read
    )
    statement = (
        ":\n"
        f"    have circuit := {model.formal_circuit}\n"
        f"    ∀ input, {model.admitted} input →\n"
        "      circuit.Defined input ∧\n"
        "        have y := circuit.value input\n"
        f"{bound}"
        f"        {formula(claim.post, names)}"
    )
    observed = [
        f"{names[i]} is {port}[t^{t}, x^{x}]"
        for i, (port, (t, x)) in enumerate(claim.observed)
    ]
    description = (
        f"{_listed(observed)}: coefficients of the compiled circuit's outputs "
        f"y = [{', '.join(c.output_ports)}], for an input the model admits."
    )
    # Each bound name is the observation's own point at its slot, by `rfl`: an
    # entry naming another port or degree is refused here.
    slots = tuple(
        f"have forsetiC{i} : ∀ y p, {c.output}.point y ⟨{i}, p⟩ = "
        f"(({entries[i]} : ℚ) : ℝ) := fun _ _ => rfl"
        for i in read
    )
    casts, cast_names = _casts(_constants(claim.post))
    unfold = [
        "forsetiContract",
        f"{_STREAM}.StreamHoare",
        "Gimle.Forseti.StreamObservation.Observation.Observes",
        "forsetiPost",
        *_FORMULA,
        *(f"forsetiC{i}" for i in read),
        *cast_names,
        *_ARITHMETIC,
        "imp_self",
    ]
    return Readable(
        statement,
        description,
        setup=(*slots, *casts),
        to_lane=("revert claim", _simp(unfold)),
        from_lane=("revert lane", _simp(unfold)),
        **_ROOTED,
    )


# --- full stream --------------------------------------------------------------


def _full_stream(goal: StreamGoal, claim: FullStreamClaim) -> Readable:
    model = STREAM_MODELS[goal.model]
    stream = _lane(model.full_stream, model.key, "full-stream")
    axes = model.axes
    polynomial = f"{_STREAMS}.Poly {len(axes)}"
    streams = [p for _, p in claim.outputs]
    axis_lines = "".join(
        f"    have {identifier(axes[i])} : {polynomial} := MvPolynomial.X {i}\n"
        for i in _variables(*streams)
    )
    realized = "".join(
        f"        {_STREAMS}.Realizes .{goal.basis} (y {k}) "
        f"{_poly(p, axes, 3, True)} ∧\n"
        for k, p in enumerate(streams)
    )
    statement = (
        ":\n"
        f"    have circuit := {model.formal_circuit}\n"
        f"{axis_lines}"
        f"    ∀ input, {model.admitted} input →\n"
        "      circuit.Defined input ∧\n"
        "        have y := circuit.value input\n"
        f"{realized}"
        f"        {stream.boundary} y"
    )
    outputs = [f"y {k} is {port}" for k, (port, _) in enumerate(claim.outputs)]
    description = (
        f"{_listed(outputs)}, the compiled circuit's outputs; "
        f"{_listed(list(axes))} are its axes as polynomials; the last conjunct "
        "says the boundary profile is kept."
    )
    # One equation per constant, from the root's `C (mkRat …)` to how the
    # statement writes it; then the identities read `two = ofPoly .ogf 2`.
    constants = _constants(*streams)
    equations = tuple(
        f"have forsetiPoly{i} : (MvPolynomial.C {rational(value)} : {polynomial}) = "
        f"{_poly(Const(value), (), 0, True)} := "
        "by norm_num [Rat.mkRat_eq_div, map_ofNat]"
        for i, value in enumerate(constants)
    )
    rewrites = [
        f"{_STREAMS}.Realizes",
        *(f"forsetiPoly{i}" for i in range(len(constants))),
        "← sub_eq_add_neg",
    ]
    unfold = [
        "forsetiContract",
        f"{_STREAM}.StreamHoare",
        *rewrites,
        "identities",
        "funext_iff",
        "Fin.forall_fin_one",
        "Fin.forall_fin_two",
        "Matrix.cons_val_fin_one",
        "Matrix.cons_val_zero",
        "Matrix.cons_val_one",
        "Matrix.head_cons",
        "and_assoc",
        "imp_self",
    ]
    return Readable(
        statement,
        description,
        setup=(
            *equations,
            _simp(["forsetiIdentities", *rewrites], "identities"),
        ),
        to_lane=("revert claim", _simp(unfold)),
        from_lane=("revert lane", _simp(unfold)),
        identities=True,
        **_ROOTED,
    )


# --- truncation ---------------------------------------------------------------


class _TruncationWords(NamedTuple):
    """The model-specific parts of a readable truncation statement."""

    other: str  # the circuit's other output: u_xx for heat, u_t for Burgers
    relation: str
    heads: str
    what: str
    equations: tuple[str, ...]
    rewrites: str


def _heat_truncation_words(basis: str, terms: str, rendered: str) -> _TruncationWords:
    other = "u_xx"
    return _TruncationWords(
        other,
        f"circuit.Rel ![u, boundary, unused] ![{other}, u]",
        f"    have circuit := {_STREAMS}.Heat.circuit {basis}\n"
        f"    have boundary := {ANALYTIC_HEAT}.boundary {basis}\n"
        f"      ({ANALYTIC_HEAT}.expSum [{terms}])\n",
        "u is the stream the heat circuit rebuilds from the boundary profile "
        "Σ cᵢ·e^(aᵢx) given by the pairs (cᵢ, aᵢ), u_xx its other output; ",
        (
            f"have forsetiProfile : {ANALYTIC_HEAT}.expSum {rendered} = "
            f"{ANALYTIC_HEAT}.expSum [{terms}] := by norm_num [Rat.mkRat_eq_div]",
        ),
        "rw [forsetiProfile]",
    )


def _front_truncation_words(
    basis: str, terms: str, rendered: str, viscosity: Fraction
) -> _TruncationWords:
    other = "u_t"
    nu = _argument(viscosity)
    return _TruncationWords(
        other,
        f"circuit.Rel ![u, slice, unused] ![{other}, u]",
        f"    have circuit := {_STREAMS}.Burgers.circuit {basis} {nu}\n"
        f"    have slice := {COLE_HOPF}.zeroSlice\n"
        f"      ({COLE_HOPF}.front {nu} [{terms}])\n",
        f"u is the stream the Burgers circuit at viscosity {number(viscosity)} "
        "rebuilds from the t = 0 slice of the Cole–Hopf front −2ν φ_x/φ of the "
        "potential Σ cᵢ·e^(aᵢx) given by the pairs (cᵢ, aᵢ), u_t its other "
        "output; ",
        (
            f"have forsetiFront : {COLE_HOPF}.front {rational(viscosity)} "
            f"{rendered} = {COLE_HOPF}.front {nu} [{terms}] := by "
            "norm_num [Rat.mkRat_eq_div]",
            f"have forsetiCircuit : {_STREAMS}.Burgers.circuit {basis} "
            f"{rational(viscosity)} = {_STREAMS}.Burgers.circuit {basis} {nu} "
            ":= by norm_num [Rat.mkRat_eq_div]",
        ),
        "rw [forsetiFront, forsetiCircuit]",
    )


def _truncation(goal: StreamGoal, claim: TruncationClaim) -> Readable:
    profile = goal.profile
    if not isinstance(profile, ExpSum):
        raise ValueError("a truncation claim is about an exponential-sum profile")
    model = STREAM_MODELS[goal.model]
    t, x = (identifier(name) for name in model.axes)
    basis = f".{goal.basis}"
    point = f"![{t}, {x}]"
    (r_t, r_x), (n_t, n_x) = claim.radius, claim.window
    terms = ", ".join(f"({number(c)}, {number(a)})" for c, a in profile.terms)
    rendered = exp_sum_terms(profile)
    field = f"{_STREAMS}.analyticField {basis} u {point}"
    box = f"|{t}| ≤ {number(r_t)} ∧ |{x}| ≤ {number(r_x)}"
    words = (
        _heat_truncation_words(basis, terms, rendered)
        if goal.viscosity is None
        else _front_truncation_words(basis, terms, rendered, goal.viscosity)
    )
    other, relation, heads, what, equations, rewrites = words
    statement = (
        ":\n"
        f"{heads}"
        f"    (∃ u unused {other} : {_STREAMS}.Stream 2,\n"
        f"      {relation}) ∧\n"
        f"    ∀ u unused {other} : {_STREAMS}.Stream 2,\n"
        f"      {relation} →\n"
        f"      ∀ {t} {x} : ℝ, {box} →\n"
        f"        HasSum ({_STREAMS}.seriesTerm {basis} u {point})\n"
        f"          ({field}) ∧\n"
        f"        |{field} -\n"
        f"          {_STREAMS}.windowField {basis} ![{n_t}, {n_x}] u {point}| ≤ "
        f"{number(claim.epsilon)}"
    )
    description = (
        f"{what}its series converges on the box, and its window ({n_t}, {n_x}) "
        f"is within {number(claim.epsilon)} of the sum there."
    )
    casts, _ = _casts([claim.epsilon])
    setup = (
        *equations,
        f"have forsetiInside : ∀ {t} {x} : ℝ, forsetiBox.Mem {point} ↔ {box} := by",
        f"  intro {t} {x}",
        f"  norm_num [{_STREAMS}.Box.Mem, Fin.forall_fin_two, forsetiBox, "
        "Rat.mkRat_eq_div]",
        *casts,
    )
    return Readable(
        statement,
        description,
        lane="forsetiRoot",
        root="root",
        refuted="refuted",
        setup=setup,
        to_lane=(
            "dsimp only at claim",
            "unfold forsetiRoot",
            rewrites,
            "obtain ⟨realized, every⟩ := claim",
            f"refine ⟨realized, fun u unused {other} rel => "
            "⟨fun z inside => ?_, fun z inside => ?_⟩⟩",
            "all_goals",
            "  have point : ![z 0, z 1] = z := by funext i; fin_cases i <;> rfl",
            f"  have holds := every u unused {other} rel (z 0) (z 1) "
            "((forsetiInside _ _).mp (point ▸ inside))",
            "  rw [point] at holds",
            "· exact holds.1",
            "· rw [forsetiCast0]",
            "  exact holds.2",
        ),
        from_lane=(
            "unfold forsetiRoot at lane",
            f"{rewrites} at lane",
            "dsimp only",
            "obtain ⟨realized, every⟩ := lane",
            f"refine ⟨realized, fun u unused {other} rel {t} {x} inside => ?_⟩",
            f"obtain ⟨sums, bound⟩ := every u unused {other} rel",
            f"have mem := (forsetiInside {t} {x}).mpr inside",
            "have close := bound _ mem",
            "rw [forsetiCast0] at close",
            "exact ⟨sums _ mem, close⟩",
        ),
    )


# --- field bands on the front ------------------------------------------------


def _analytic_field(goal: StreamGoal, claim: FieldClaim) -> Readable:
    """Write a band on the analytic field of every stream the circuit reconstructs."""
    profile = goal.profile
    if not isinstance(profile, ExpSum):
        raise ValueError("an analytic-field claim is about an exponential-sum profile")
    if goal.viscosity is None:
        raise ValueError("an analytic-field claim names the front's viscosity")
    bounds = band_of(claim.post)
    if bounds is None:
        raise ValueError("an analytic-field claim is a band lo ≤ u ≤ hi")
    lo, hi = bounds
    model = STREAM_MODELS[goal.model]
    t, x = (identifier(name) for name in model.axes)
    basis = f".{goal.basis}"
    point = f"![{t}, {x}]"
    (_, r_t), (_, r_x) = claim.time, claim.space
    terms = ", ".join(f"({number(c)}, {number(a)})" for c, a in profile.terms)
    rendered = exp_sum_terms(profile)
    other, relation, heads, what, equations, rewrites = _front_truncation_words(
        basis, terms, rendered, goal.viscosity
    )
    box = f"|{t}| ≤ {number(r_t)} ∧ |{x}| ≤ {number(r_x)}"
    field = f"{_STREAMS}.analyticField {basis} u {point}"
    parts = []
    if lo is not None:
        parts.append(f"{number(lo)} ≤ {field}")
    if hi is not None:
        parts.append(f"{field} ≤ {number(hi)}")
    band = " ∧\n        ".join(parts)
    statement = (
        ":\n"
        f"{heads}"
        f"    ∀ u unused {other} : {_STREAMS}.Stream 2,\n"
        f"      {relation} →\n"
        f"      ∀ {t} {x} : ℝ, {box} →\n"
        f"        HasSum ({_STREAMS}.seriesTerm {basis} u {point})\n"
        f"          ({field}) ∧\n"
        f"        {band}"
    )
    description = (
        f"{what}its series converges at every point of the box, and its sum, "
        f"the analytic field, stays within the band there."
    )
    casts, cast_names = _casts([value for value in (lo, hi) if value is not None])
    setup = (
        *equations,
        f"have forsetiInside : ∀ {t} {x} : ℝ, forsetiBox.Mem {point} ↔ {box} := by",
        f"  intro {t} {x}",
        f"  norm_num [{_STREAMS}.Box.Mem, Fin.forall_fin_two, forsetiBox, "
        "Rat.mkRat_eq_div]",
        *casts,
    )
    simp = _simp(cast_names)
    return Readable(
        statement,
        description,
        lane="forsetiRoot",
        root="root",
        refuted="refuted",
        setup=setup,
        to_lane=(
            "dsimp only at claim",
            "unfold forsetiRoot",
            rewrites,
            f"intro u unused {other} rel z inside",
            "have point : ![z 0, z 1] = z := by funext i; fin_cases i <;> rfl",
            f"have holds := claim u unused {other} rel (z 0) (z 1) "
            "((forsetiInside _ _).mp (point ▸ inside))",
            "rw [point] at holds",
            simp,
            "exact holds",
        ),
        from_lane=(
            "unfold forsetiRoot at lane",
            f"{rewrites} at lane",
            "dsimp only",
            f"intro u unused {other} rel {t} {x} inside",
            f"have holds := lane u unused {other} rel {point} "
            f"((forsetiInside {t} {x}).mpr inside)",
            _simp(cast_names, "holds"),
            "exact holds",
        ),
    )


# --- the Euler stream on the torus ---------------------------------------------

_TRIG = f"{_STREAMS}.TrigStream"


def _trig(goal: StreamGoal, claim: TrigTruncationClaim | TrigBandClaim) -> Readable:
    """State the total circuit contract with literal initial modes and bounds.

    The public statement and replay root independently name the circuit,
    initial-slice admission and output property. Their bridge unfolds only
    the generated definitions and converts exact rational constants.
    """
    start = goal.profile
    if not isinstance(start, Modes):
        raise ValueError("a trigonometric claim is about a start of modes")
    if goal.viscosity is None:
        raise ValueError("a trigonometric claim names its viscosity")
    table = ", ".join(f"(({k1}, {k2}), {_constant(c)})" for k1, k2, c in start.terms)
    initial = f"{_STREAMS}.Torus.Table.toTrig [{table}]"
    circuit = (
        f"{_STREAMS}.NS.vorticityCircuit .{goal.basis} {_argument(goal.viscosity)}"
    )
    r = claim.radius
    series = (
        f"HasSum ({_TRIG}.seriesTerm ω t x)\n"
        f"            ({_TRIG}.analyticField ω t x)"
    )
    if isinstance(claim, TrigTruncationClaim):
        constants = [r, claim.epsilon]
        rest = (
            f"{_TRIG}.TruncationBound ω {_argument(r)} {claim.window} "
            f"{_argument(claim.epsilon)}"
        )
        what = (
            f"its series converges for |t| ≤ {number(r)}, and the window through "
            f"t^{claim.window - 1} is within {number(claim.epsilon)} of the sum there."
        )
    else:
        bounds = band_of(claim.post)
        if bounds is None:
            raise ValueError("a trig-band claim is a band lo ≤ u ≤ hi")
        lo, hi = bounds
        constants = [r, *(value for value in (lo, hi) if value is not None)]
        parts = []
        if lo is not None:
            parts.append(f"{_constant(lo)} ≤ {_TRIG}.analyticField ω t x")
        if hi is not None:
            parts.append(f"{_TRIG}.analyticField ω t x ≤ {_constant(hi)}")
        rest = (
            f"∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ {_constant(r)} →\n"
            "          " + " ∧ ".join(parts)
        )
        what = (
            f"its series converges for |t| ≤ {number(r)}, and its sum stays within "
            "the band there."
        )
    classical = goal.interpretation in CLASSICAL_EULER
    guarantee = (
        f"        Gimle.Forseti.Euler.Certified (fun _ => {initial}) "
        f"{_argument(r)} output ∧\n"
        if classical
        else f"        (∀ (t : ℝ) (x : ℝ × ℝ), |t| ≤ {_constant(r)} →\n"
        f"          {series}) ∧\n"
    )
    statement = (
        ":\n"
        f"    Gimle.Forseti.Fourier.Contract ({circuit})\n"
        f"      (fun input => input 0 0 = {initial})\n"
        "      (fun output =>\n"
        "        have ω := output 0;\n"
        f"{guarantee}"
        f"        {rest})"
    )
    description = (
        f"The Fourier feedback circuit (ν = {number(goal.viscosity)}; ν = 0 is "
        "the Euler equation) admits every input whose initial vorticity is "
        "Σ c·e^(i k·x) over the listed ((k₁, k₂), c); higher boundary "
        f"coefficients are free. In basis {goal.basis}, a unique formal output "
        f"exists and every output ω satisfies the bound: {what} "
        "This claim does not assert classical PDE realization; that theorem "
        "additionally needs an even, mean-zero start."
    )
    if classical:
        description = (
            f"The Euler Fourier feedback circuit admits the listed initial modes, with "
            "higher boundary coefficients free. Its unique formal output has the "
            "prescribed initial field, mean-zero and even coefficients, and a "
            "convergent classical Euler field on the closed interval "
            f"|t| ≤ {number(r)}, "
            f"including both endpoints. In addition, {what} "
            "Uniqueness concerns the formal stream; no classical uniqueness "
            "or maximal lifetime is claimed."
        )
    casts, cast_names = _casts(constants)
    setup = (
        f"have forsetiInitial : forsetiStart 0 = {initial} := by",
        "  unfold forsetiStart forsetiTable",
        "  norm_num [Rat.mkRat_eq_div]",
        f"have forsetiViscosity : ({rational(goal.viscosity)} : ℚ) = "
        f"{_constant(goal.viscosity)} := by norm_num [Rat.mkRat_eq_div]",
        *casts,
    )
    names = ["forsetiInitial", "forsetiViscosity", *cast_names]
    if classical:
        setup += (
            f"have forsetiBoundary : forsetiStart = (fun _ => {initial}) := by",
            "  funext n",
            "  unfold forsetiStart forsetiTable",
            "  norm_num [Rat.mkRat_eq_div]",
        )
        # Rewriting the rational viscosity first can expose ``↑(0 : ℚ)``
        # inside a zero radius, error or band endpoint before its cast lemma.
        names.extend(("forsetiBoundary", "Rat.cast_zero"))
    simp = _simp(names)
    return Readable(
        statement,
        description,
        lane="forsetiRoot",
        root="root",
        refuted="refuted",
        setup=setup,
        to_lane=(
            "dsimp only at claim",
            "unfold forsetiRoot forsetiPost",
            simp,
            "exact claim",
        ),
        from_lane=(
            "unfold forsetiRoot forsetiPost at lane",
            _simp(names, "lane"),
            "dsimp only",
            "exact lane",
        ),
    )


# --- physical mild circuit ----------------------------------------------------


def _mild(goal: StreamGoal, claim: MildTruncationClaim | MildBandClaim) -> Readable:
    """State the literal forward-time contract independently of its proof route."""
    if not isinstance(goal.profile, Modes) or goal.viscosity is None:
        raise ValueError("a mild claim names its finite start and viscosity")
    mild = f"{_STREAMS}.Mild"
    certified = "Gimle.Forseti.MildVorticity.Certified"
    table = ", ".join(
        f"(({k1}, {k2}), {_constant(c)})" for k1, k2, c in goal.profile.terms
    )
    initial = f"({_STREAMS}.Torus.Table.toTrig [{table}])"
    nu, horizon = _argument(goal.viscosity), _argument(claim.time)
    constants = [claim.time]
    if type(claim) is MildTruncationClaim:
        constants.append(claim.epsilon)
        post = (
            f"{mild}.MildTruncationBound {nu} (output 0) {horizon} {claim.window}"
            f" {_argument(claim.epsilon)}"
        )
        detail = (
            f"The first {claim.window} physical interaction terms approximate the sum"
            f" within {number(claim.epsilon)}."
        )
    else:
        assert type(claim) is MildBandClaim
        bounds = band_of(claim.post)
        if bounds is None:
            raise ValueError("a mild-band claim is a band on u")
        lo, hi = bounds
        constants.extend(value for value in bounds if value is not None)
        field = f"{mild}.analyticField {nu} (output 0) t x"
        sides = ([] if lo is None else [f"{_constant(lo)} ≤ {field}"]) + (
            [] if hi is None else [f"{field} ≤ {_constant(hi)}"]
        )
        post = f"∀ t ∈ Set.Icc 0 {horizon}, ∀ x : ℝ × ℝ, " + " ∧ ".join(sides)
        detail = "The physical field remains in the requested band."
    statement = (
        ":\n"
        f"    Gimle.Forseti.Mild.Contract ({mild}.mildCircuit {nu})\n"
        f"      (fun input => input 0 0 = {mild}.embed {initial})\n"
        f"      (fun output => {certified} {nu} {initial} {horizon} output ∧\n"
        f"        ({post}))"
    )
    description = (
        f"The typed mild feedback circuit at viscosity {number(goal.viscosity)} "
        "admits every input with the listed even, mean-zero initial vorticity; "
        "higher boundary coefficients are free. A unique formal output exists. "
        f"On 0 ≤ t ≤ {number(claim.time)}, every output has absolute convergence, "
        "Fourier reconstruction, continuous spatial derivatives, and nonincreasing "
        "spectral energy and enstrophy. The time derivative, classical PDE and "
        "dissipation derivative identities hold in the interval's interior; the "
        "time derivative extends continuously to its endpoints, and a positive "
        f"horizon also gives a right derivative at zero. {detail} "
        "The window counts interaction terms, each already a function of physical time."
    )
    casts, names = _casts(constants)
    setup = (
        f"have forsetiInitial : forsetiP = {initial} := by",
        "  unfold forsetiP forsetiTable",
        "  norm_num [Rat.mkRat_eq_div]",
        (
            f"have forsetiViscosity : ({rational(goal.viscosity)} : ℚ) ="
            f" {_constant(goal.viscosity)} := by norm_num [Rat.mkRat_eq_div]"
        ),
        *casts,
    )
    names = ["forsetiInitial", "forsetiViscosity", *names, "Rat.cast_zero"]
    return Readable(
        statement,
        description,
        lane="forsetiRoot",
        root="root",
        refuted="refuted",
        setup=setup,
        to_lane=("unfold forsetiRoot forsetiPost", _simp(names), "exact claim"),
        from_lane=(
            "unfold forsetiRoot forsetiPost at lane",
            _simp(names, "lane"),
            "exact lane",
        ),
    )
