"""Read and write closed goals as text, for people and agents.

The three deciders take goals as exact values, and their JSON encodings nest
every polynomial as tagged lists, which nobody writes by hand. This module is
a second, textual encoding of the same values:

    entailment x : x*x = 2 |- x < 0

`parse_goal` returns exactly the `Goal`, `TrajectoryGoal` or `StreamGoal` the
text denotes, or raises `GoalTextError`; it never guesses. `render_goal` writes
the forms the parser reads back, so ``parse_goal(render_goal(g)) == g``.
The mapping is structural, not algebraic: ``p <= q`` is the atom
``p - q <= 0`` and ``p <= 0`` (any spelling of zero) is the atom ``p <= 0``,
which is how the goal corpora are written; ``x^2`` is sugar for ``x*x``;
``(-3/2)`` is a negative constant, while ``-3/2`` negates ``3/2``. The words in
`RESERVED` cannot name a coordinate, since a formula reads them as keywords;
a goal that uses one has only its JSON encoding.

A trajectory goal also reads in a short form through the model registry,
``trajectory three-state from (1, 2, -1) at 2 observe V always : V <= 6``: the
interpretation ``continuous-real`` and obligation ``contract``, the initial
values in the model's state order, the observation by name, and for a driven
model ``within B``, the bound on its one bounded driver. It is refused when
the registry cannot read it exactly. `render_goal` always writes the long form,
which needs no registry; `render_short_goal` writes the short one where it
reads back to the same goal.

The text is an input format only. What is proved is the goal value, whose
Lean statement the deciders regenerate; the text carries no authority.
"""

from __future__ import annotations

from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Union

from lark import Lark, Token, Tree
from lark.exceptions import LarkError, UnexpectedInput

from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Context,
    Fls,
    Formula,
    Goal,
    GoalError,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.streams.goal import (
    AXES_CONTEXT,
    FIELD_CONTEXT,
    PROFILE_CONTEXT,
    Claim,
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    FullStreamClaim,
    MildBandClaim,
    MildTruncationClaim,
    Modes,
    Profile,
    StreamGoal,
    TrigBandClaim,
    TrigTruncationClaim,
    TruncationClaim,
    coefficient_context,
)
from glitnir_forseti.trajectory.goal import TrajectoryGoal
from glitnir_forseti.trajectory.models import MODELS as TRAJECTORY_MODELS
from glitnir_forseti.trajectory.models import ModelEntry, ObservationBand

AnyGoal = Union[Goal, TrajectoryGoal, StreamGoal]

#: The longest goal text accepted; the decoders' node limits apply after.
MAX_TEXT = 65536
#: The largest exponent ``^`` expands; ``x^n`` becomes ``n - 1`` products,
#: nested ``n - 1`` deep, so it stays below the default depth limit.
MAX_EXPONENT = 32
#: Words a formula reads as connectives or constants. A coordinate with one of
#: these names has no faithful text, so neither direction accepts it.
RESERVED = frozenset({"and", "or", "not", "true", "false"})
#: Readable names for the grammar's terminals, for "expected one of" hints.
_TERMINALS = {
    "NAME": "a name",
    "ID": "an identifier",
    "NUMBER": "a number",
    "NEGATIVE": "a negative constant like (-3/2)",
    "SIGNED": "a number like -1 or 3/2",
    "INT": "a whole number",
    "RELATION": "a comparison (<=, <, >=, >, =)",
    "TURNSTILE": "|-",
    "QUANTIFIER": "always, at-start or eventually",
}

_RELATIONS = {
    "=": Relation.EQ,
    "<=": Relation.LE,
    "≤": Relation.LE,
    "<": Relation.LT,
    ">=": Relation.GE,
    "≥": Relation.GE,
    ">": Relation.GT,
}
_SYMBOLS = {
    Relation.EQ: "=",
    Relation.LE: "<=",
    Relation.LT: "<",
    Relation.GE: ">=",
    Relation.GT: ">",
}
_ZERO = Const(Fraction(0))
#: What the short trajectory form leaves out: the only supported reading.
SHORT_INTERPRETATION = "continuous-real"
SHORT_OBLIGATION = "contract"


class GoalTextError(GoalError):
    """The text is not a goal; the message names where, when it can."""


@cache
def _parser() -> Lark:
    grammar = (Path(__file__).with_name("goal.lark")).read_text(encoding="utf-8")
    return Lark(grammar, parser="lalr", lexer="contextual", maybe_placeholders=False)


def parse_goal(text: str) -> AnyGoal:
    """Parse one goal.

    Raises:
        GoalTextError: The text is not a goal, names an unknown coordinate,
            or builds a value the goal types refuse.
    """
    if type(text) is not str:
        raise GoalTextError("goal text must be a string")
    if len(text) > MAX_TEXT:
        raise GoalTextError(f"goal text exceeds {MAX_TEXT} characters")
    try:
        tree = _parser().parse(text)
    except UnexpectedInput as error:
        raise GoalTextError(
            f"line {error.line}, column {error.column}: unexpected input"
            + _expected(error)
            + "\n"
            + error.get_context(text).rstrip()
        ) from None
    except LarkError as error:
        raise GoalTextError(str(error)) from None
    try:
        root = tree.children[0]
        assert isinstance(root, Tree)
        return _goal(root)
    except RecursionError:
        raise GoalTextError("goal text is nested too deeply") from None
    except GoalTextError:
        raise
    except GoalError as error:
        raise GoalTextError(str(error)) from None


def _expected(error: UnexpectedInput) -> str:
    """Say what the parser would have accepted, in words."""
    names = getattr(error, "accepts", None) or getattr(error, "allowed", None)
    if not names:
        return ""
    grammar = {t.name: t for t in _parser().terminals}
    readable = set()
    for name in names:
        if name in _TERMINALS:
            readable.add(_TERMINALS[name])
        elif name in grammar and grammar[name].pattern.type == "str":
            readable.add(repr(grammar[name].pattern.value))
    return f"; expected {', '.join(sorted(readable))}" if readable else ""


def _context(names: tuple[str, ...]) -> Context:
    """Build a context, refusing names a formula would read as keywords."""
    for name in names:
        if name in RESERVED:
            raise GoalTextError(f"{name!r} is a keyword and cannot name a coordinate")
    return Context(names)


def _goal(tree: Tree) -> AnyGoal:
    if tree.data == "entailment":
        names, antecedent, _, consequent = tree.children
        context = _context(tuple(str(name) for name in names.children))
        return Goal(
            context, _formula(antecedent, context), _formula(consequent, context)
        )
    if tree.data == "trajectory":
        return _trajectory(tree.children)
    if tree.data == "short_trajectory":
        return _short_trajectory(tree.children)
    return _stream(tree.children)


def _trajectory(children: list) -> TrajectoryGoal:
    model, interpretation, obligation, axis, start, *rest = children
    initial = [c for c in rest if isinstance(c, Tree) and c.data == "initial"]
    bounds = [c for c in rest if isinstance(c, Tree) and c.data == "drivers"]
    observation, name, quantifier, post = rest[len(initial) + len(bounds) :]
    drivers = [
        (str(port), _rational(bound))
        for tree in bounds
        for port, bound in (b.children for b in tree.children)
    ]
    drivers.sort()
    if len({port for port, _ in drivers}) != len(drivers):
        raise GoalTextError("bounded drivers are distinct")
    pairs = [
        (str(port), _rational(value)) for port, value in (i.children for i in initial)
    ]
    # The order of initial ports carries no meaning; the goal keeps them sorted.
    pairs.sort()
    ids = [port for port, _ in pairs]
    if len(set(ids)) != len(ids):
        raise GoalTextError("initial ports are distinct")
    context = _context((str(name),))
    return TrajectoryGoal(
        model=str(model),
        interpretation=str(interpretation),
        obligation=str(obligation),
        axis=str(axis),
        start=_rational(start),
        initial=tuple(pairs),
        observation=str(observation),
        name=str(name),
        quantifier=str(quantifier),
        post=_formula(post, context),
        drivers=tuple(drivers),
    )


def _short_trajectory(children: list) -> TrajectoryGoal:
    """Read the short form through the registry; refuse what it lacks."""
    model, *values = children
    within = [c for c in values if isinstance(c, Tree) and c.data == "within"]
    values = [c for c in values if not (isinstance(c, Tree) and c.data == "within")]
    start, name, quantifier, post = values[-4:]
    values = values[:-4]
    entry = TRAJECTORY_MODELS.get(str(model))
    if entry is None:
        raise GoalTextError(
            f"no trajectory model {str(model)!r}; the short form needs a"
            f" registered one: {', '.join(TRAJECTORY_MODELS)}"
        )
    if len(values) != len(entry.initial_ids):
        raise GoalTextError(
            f"{entry.key} takes {len(entry.initial_ids)} initial values, for"
            f" {', '.join(entry.initial_ids)} in that order; got {len(values)}"
        )
    band = _band_named(entry, str(name))
    pairs = sorted(zip(entry.initial_ids, (_rational(v) for v in values)))
    drivers: tuple[tuple[str, Fraction], ...] = ()
    if within:
        if entry.drivers is None:
            raise GoalTextError(f"{entry.key} has no drivers; drop `within`")
        drivers = ((entry.drivers.bounded, _rational(within[0].children[0])),)
    return TrajectoryGoal(
        model=entry.key,
        interpretation=SHORT_INTERPRETATION,
        obligation=SHORT_OBLIGATION,
        axis=entry.axis,
        start=_rational(start),
        initial=tuple(pairs),
        observation=band.port,
        name=band.name,
        quantifier=str(quantifier),
        post=_formula(post, _context((band.name,))),
        drivers=drivers,
    )


def _band_named(entry: ModelEntry, name: str) -> ObservationBand:
    """Return the one observation of ``entry`` called ``name``."""
    bands = [b for b in entry.observations.values() if b.name == name]
    if len(bands) > 1:
        raise GoalTextError(
            f"{entry.key} has {len(bands)} observations named {name!r}; the name"
            " is ambiguous, so write the long form with the port"
        )
    if not bands:
        names = ", ".join(sorted({b.name for b in entry.observations.values()}))
        raise GoalTextError(
            f"{entry.key} has no observation named {name!r};"
            f" its observations are {names}"
        )
    return bands[0]


def _stream(children: list) -> StreamGoal:
    model, *rest = children
    viscosity = None
    if isinstance(rest[0], Tree) and rest[0].data == "viscosity":
        viscosity = _rational(rest[0].children[0])
        rest = rest[1:]
    interpretation, basis, declared, claim = rest
    if declared.data != "start" and (declared.data == "potential") != (
        viscosity is not None
    ):
        raise GoalTextError(
            "a viscous model's exponential sum is the Cole–Hopf potential's: write "
            "`potential` after `viscosity`, and `profile` for a model without one"
        )
    if declared.data == "start" and viscosity is None:
        raise GoalTextError("a start of modes names its viscosity; ν = 0 is Euler")
    return StreamGoal(
        model=str(model),
        interpretation=str(interpretation),
        basis=str(basis),
        profile=_profile(declared.children[0]),
        claim=_claim(claim),
        viscosity=viscosity,
    )


def _profile(node: Tree | Token) -> Profile:
    if isinstance(node, Tree) and node.data == "modes":
        return Modes(
            tuple(
                (_integer(k1), _integer(k2), _rational(c))
                for k1, k2, c in (term.children for term in node.children)
            )
        )
    if isinstance(node, Tree) and node.data == "exponentials":
        return ExpSum(
            tuple(
                (_rational(c), _rational(a))
                for c, a in (term.children for term in node.children)
            )
        )
    return _poly(node, PROFILE_CONTEXT)


def _claim(tree: Tree) -> Claim:
    if tree.data == "field":
        port, time_name, time, space_name, space, post = tree.children
        if (str(time_name), str(space_name)) != ("t", "x"):
            raise GoalTextError("a field is bounded over t, then x")
        return FieldClaim(
            str(port), _interval(time), _interval(space), _formula(post, FIELD_CONTEXT)
        )
    if tree.data == "coefficients":
        *observed, post = tree.children
        pairs = tuple(
            (str(port), (int(str(t)), int(str(x))))
            for port, t, x in (c.children for c in observed)
        )
        return CoefficientClaim(pairs, _formula(post, coefficient_context(len(pairs))))
    if tree.data == "truncation":
        port, n_t, n_x, r_t, r_x, epsilon = tree.children
        return TruncationClaim(
            str(port),
            (int(str(n_t)), int(str(n_x))),
            (_rational(r_t), _rational(r_x)),
            _rational(epsilon),
        )
    if tree.data == "mild_truncation":
        port, n, t, epsilon = tree.children
        return MildTruncationClaim(
            str(port), int(str(n)), _rational(t), _rational(epsilon)
        )
    if tree.data == "mild_band":
        port, t, post = tree.children
        return MildBandClaim(str(port), _rational(t), _formula(post, FIELD_CONTEXT))
    if tree.data == "trig_truncation":
        port, n, r, epsilon = tree.children
        return TrigTruncationClaim(
            str(port), int(str(n)), _rational(r), _rational(epsilon)
        )
    if tree.data == "trig_band":
        port, r, post = tree.children
        return TrigBandClaim(str(port), _rational(r), _formula(post, FIELD_CONTEXT))
    return FullStreamClaim(
        tuple(
            (str(port), _poly(stream, AXES_CONTEXT))
            for port, stream in (o.children for o in tree.children)
        )
    )


def _interval(tree: Tree) -> tuple[Fraction, Fraction]:
    low, high = tree.children
    return _rational(low), _rational(high)


def _formula(node: Tree | Token, context: Context) -> Formula:
    assert isinstance(node, Tree)
    kind = node.data
    if kind == "true_":
        return Tru()
    if kind == "false_":
        return Fls()
    if kind == "not_":
        return Not(_formula(node.children[0], context))
    if kind in ("and_", "or_"):
        left, right = node.children
        build = And if kind == "and_" else Or
        return build(_formula(left, context), _formula(right, context))
    lhs, relation, rhs = node.children
    difference = _poly(lhs, context)
    if not (isinstance(rhs, Tree) and rhs.data == "const" and _is_zero(rhs)):
        difference = Add(difference, Neg(_poly(rhs, context)))
    return Atom(_RELATIONS[str(relation)], difference)


def _poly(node: Tree | Token, context: Context) -> Poly:
    assert isinstance(node, Tree)
    kind = node.data
    if kind == "var":
        name = str(node.children[0])
        if name not in context.names:
            raise GoalTextError(
                f"unknown coordinate {name!r}; this goal's are "
                + ", ".join(context.names)
            )
        return context.var(name)
    if kind == "const":
        return Const(_constant(node))
    if kind == "neg":
        return Neg(_poly(node.children[0], context))
    if kind == "pow":
        base, exponent = node.children
        count = int(str(exponent))
        if not 1 <= count <= MAX_EXPONENT:
            raise GoalTextError(f"an exponent is between 1 and {MAX_EXPONENT}")
        factor = _poly(base, context)
        product = factor
        for _ in range(count - 1):
            product = Mul(product, factor)
        return product
    left, right = (_poly(child, context) for child in node.children)
    if kind == "add":
        return Add(left, right)
    if kind == "sub":
        return Add(left, Neg(right))
    return Mul(left, right)


def _constant(node: Tree) -> Fraction:
    """Read a constant: a number, or ``(-number)`` with any spacing."""
    text = "".join(str(node.children[0]).split())
    return _rational(text.removeprefix("(").removesuffix(")"))


def _is_zero(node: Tree) -> bool:
    """Tell whether a comparison's right side is zero, however spelled."""
    return _constant(node) == 0


def _integer(token: object) -> int:
    """Read a mode index; a fraction or a word is refused with its text."""
    text = str(token)
    try:
        return int(text)
    except ValueError:
        raise GoalTextError(f"not an integer mode index: {text!r}") from None


def _rational(token: object) -> Fraction:
    text = str(token)
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise GoalTextError(f"not an exact rational: {text!r}") from None
    return value


def render_goal(goal: AnyGoal) -> str:
    """Write ``goal`` in the form `parse_goal` reads back to an equal value.

    Raises:
        GoalTextError: A coordinate is named by a keyword (`RESERVED`), so no
            text reads back to this goal; use its JSON encoding.
    """
    coordinates: tuple[str, ...] = ()
    if type(goal) is Goal:
        coordinates = goal.context.names
    elif type(goal) is TrajectoryGoal:
        coordinates = (goal.name,)
    for name in coordinates:
        if name in RESERVED:
            raise GoalTextError(f"{name!r} is a keyword; this goal has no text form")
    if type(goal) is Goal:
        names = ", ".join(goal.context.names)
        return (
            f"entailment {names} : {render_formula(goal.antecedent, goal.context)}"
            f" |- {render_formula(goal.consequent, goal.context)}"
        )
    if type(goal) is TrajectoryGoal:
        initial = ", ".join(f"{port} = {_number(v)}" for port, v in goal.initial)
        bounds = ", ".join(f"{port} within {_number(b)}" for port, b in goal.drivers)
        drivers = f" drivers {bounds}" if goal.drivers else ""
        return (
            f"trajectory {goal.model} interpretation {goal.interpretation}"
            f" obligation {goal.obligation} start {goal.axis} = {_number(goal.start)}"
            f" initial {initial}{drivers} observe {goal.observation} as {goal.name}"
            f" {goal.quantifier} : {render_formula(goal.post, goal.context)}"
        )
    if type(goal) is StreamGoal:
        viscosity, declared = "", "profile"
        if goal.viscosity is not None:
            viscosity, declared = f" viscosity {_number(goal.viscosity)}", "potential"
        if isinstance(goal.profile, Modes):
            declared = "start"
        return (
            f"stream {goal.model}{viscosity} interpretation {goal.interpretation}"
            f" basis {goal.basis} {declared} {_render_profile(goal.profile)}"
            f" {_render_claim(goal.claim)}"
        )
    raise TypeError("not a goal")


def render_short_goal(goal: AnyGoal) -> str | None:
    """Write a trajectory goal in the short form, when that reads back to it.

    The short form leaves the interpretation, obligation, axis and ports to the
    model registry, so it exists only for a registered model read the default
    way, with exactly the model's initial ports and an observation its name
    picks out. Every other goal, and every non-trajectory goal, has only the
    long form `render_goal` writes.

    Raises:
        TypeError: ``goal`` is not a goal.
        GoalTextError: The coordinate is a keyword (`RESERVED`).
    """
    if type(goal) not in (Goal, TrajectoryGoal, StreamGoal):
        raise TypeError("not a goal")
    if type(goal) is not TrajectoryGoal:
        return None
    entry = TRAJECTORY_MODELS.get(goal.model)
    if (
        entry is None
        or (goal.interpretation, goal.obligation)
        != (SHORT_INTERPRETATION, SHORT_OBLIGATION)
        or goal.axis != entry.axis
        or {port for port, _ in goal.initial} != set(entry.initial_ids)
        or len(goal.initial) != len(entry.initial_ids)
    ):
        return None
    try:
        band = _band_named(entry, goal.name)
    except GoalTextError:
        return None
    if band.port != goal.observation:
        return None
    within = ""
    if goal.drivers:
        if entry.drivers is None or [p for p, _ in goal.drivers] != [
            entry.drivers.bounded
        ]:
            return None
        within = f" within {_number(goal.drivers[0][1])}"
    if goal.name in RESERVED:
        raise GoalTextError(f"{goal.name!r} is a keyword; this goal has no text form")
    values = dict(goal.initial)
    initial = ", ".join(_number(values[port]) for port in entry.initial_ids)
    return (
        f"trajectory {goal.model} from ({initial}) at {_number(goal.start)}"
        f"{within} observe {goal.name} {goal.quantifier} :"
        f" {render_formula(goal.post, goal.context)}"
    )


def _render_profile(profile: Profile) -> str:
    if isinstance(profile, Modes):
        terms = ", ".join(f"[{k1}, {k2}, {_number(c)}]" for k1, k2, c in profile.terms)
        return f"modes {terms}"
    if isinstance(profile, ExpSum):
        terms = ", ".join(f"[{_number(c)}, {_number(a)}]" for c, a in profile.terms)
        return f"exponentials {terms}"
    return render_poly(profile, PROFILE_CONTEXT)


def _render_claim(claim: Claim) -> str:
    if type(claim) is MildTruncationClaim:
        return (
            f"truncation {claim.port} window {claim.window}"
            f" time {_number(claim.time)} epsilon {_number(claim.epsilon)}"
        )
    if type(claim) is MildBandClaim:
        return (
            f"band {claim.port} time {_number(claim.time)}"
            f" : {render_formula(claim.post, FIELD_CONTEXT)}"
        )
    if type(claim) is TrigTruncationClaim:
        return (
            f"truncation {claim.port} window {claim.window}"
            f" radius {_number(claim.radius)} epsilon {_number(claim.epsilon)}"
        )
    if type(claim) is TrigBandClaim:
        return (
            f"band {claim.port} radius {_number(claim.radius)}"
            f" : {render_formula(claim.post, FIELD_CONTEXT)}"
        )
    if type(claim) is TruncationClaim:
        (n_t, n_x), (r_t, r_x) = claim.window, claim.radius
        return (
            f"truncation {claim.port} window {n_t}, {n_x}"
            f" radius {_number(r_t)}, {_number(r_x)} epsilon {_number(claim.epsilon)}"
        )
    if type(claim) is FieldClaim:
        (t0, t1), (x0, x1) = claim.time, claim.space
        return (
            f"field {claim.port} over t in [{_number(t0)}, {_number(t1)}],"
            f" x in [{_number(x0)}, {_number(x1)}]"
            f" : {render_formula(claim.post, FIELD_CONTEXT)}"
        )
    if type(claim) is CoefficientClaim:
        observed = ", ".join(f"{port}({t}, {x})" for port, (t, x) in claim.observed)
        context = coefficient_context(len(claim.observed))
        return f"coefficients {observed} : {render_formula(claim.post, context)}"
    assert type(claim) is FullStreamClaim
    outputs = ", ".join(
        f"{port} = {render_poly(stream, AXES_CONTEXT)}"
        for port, stream in claim.outputs
    )
    return f"full-stream {outputs}"


def render_formula(formula: Formula, context: Context) -> str:
    """Write a formula over ``context``; `parse_goal` reads it back unchanged."""
    return _render_formula(formula, context, 0)


# Formula levels: 0 disjunction, 1 conjunction, 2 negation or literal.
def _render_formula(formula: Formula, context: Context, level: int) -> str:
    if type(formula) is Tru:
        return "true"
    if type(formula) is Fls:
        return "false"
    if type(formula) is Not:
        return "not " + _render_formula(formula.argument, context, 2)
    if isinstance(formula, (And, Or)):
        own = 1 if type(formula) is And else 0
        word = "and" if own else "or"
        text = (
            f"{_render_formula(formula.left, context, own)} {word} "
            f"{_render_formula(formula.right, context, own + 1)}"
        )
        return f"({text})" if level > own else text
    assert type(formula) is Atom
    symbol = _SYMBOLS[formula.relation]
    poly = formula.polynomial
    if type(poly) is Add and type(poly.right) is Neg and poly.right.argument != _ZERO:
        left = _render_poly(poly.left, context, 0)
        return f"{left} {symbol} {_render_poly(poly.right.argument, context, 0)}"
    return f"{_render_poly(poly, context, 0)} {symbol} 0"


def render_poly(poly: Poly, context: Context) -> str:
    """Write a polynomial over ``context``; `parse_goal` reads it back unchanged."""
    return _render_poly(poly, context, 0)


# Polynomial levels: 0 sum, 1 product, 2 unary, 3 primary.
def _render_poly(poly: Poly, context: Context, level: int) -> str:
    if type(poly) is Var:
        return context.names[poly.index]
    if type(poly) is Const:
        if poly.value < 0:
            return f"({_number(poly.value)})"
        return _number(poly.value)
    if type(poly) is Neg:
        text = "-" + _render_poly(poly.argument, context, 2)
        return f"({text})" if level > 2 else text
    if type(poly) is Add:
        if type(poly.right) is Neg:
            text = (
                f"{_render_poly(poly.left, context, 0)} - "
                f"{_render_poly(poly.right.argument, context, 1)}"
            )
        else:
            text = (
                f"{_render_poly(poly.left, context, 0)} + "
                f"{_render_poly(poly.right, context, 1)}"
            )
        return f"({text})" if level > 0 else text
    assert type(poly) is Mul
    text = (
        f"{_render_poly(poly.left, context, 1)}*{_render_poly(poly.right, context, 2)}"
    )
    return f"({text})" if level > 1 else text


def _number(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else str(value)
