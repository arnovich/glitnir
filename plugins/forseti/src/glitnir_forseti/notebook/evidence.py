"""Simulation evidence: runs of a notebook's models, kept apart from proof.

A Lean cell declares a simulation of one of the notebook's Asgard models::

    -- @evidence simulate dissipative (h := 1/100) (steps := 400)

Glitnir's core finds the directive and hands its verb, target and option
text to `read_directive`.

The Forseti evidence provider (`glitnir_forseti.evidence_provider`) runs it in
two steps. Lean, in Glitnir's sandbox, elaborates the notebook up to the
directive and prints the model's compiled right-hand sides — the compiler's
own expression trees (`rates.expressions`, which
`ContinuousModel.field_correct` ties to the compiled field's run), one per
state, over variables, exact rational constants, sums, products and
negations — with the state order, initial values and start. They are expanded
here into exact polynomials with `Fraction` coefficients under the same bounds
as a circuit's expansion. The expression form is linear in the size of the
equations where the compiled circuit's wiring grows quadratically with the
state count, so a 12-mode Galerkin field that the wire decoder's node and
depth bounds refuse fits in a few kilobytes (task 184). No typed Asgard
circuit is built, and Python Asgard is not needed: the rational pairs are read
here under Asgard's own interchange rules. The circuit grammar writes
constants as finite decimals, so a model with damping ``1/3`` could not be
decoded as a circuit at all. The field is then integrated with the declared
solver and step, in floating point.

A discrete system is iterated instead, from a circuit whose ports are the
state, this tick's inputs and fixed parameters (the order
`Gimle.Forseti.Discrete.step` reads)::

    -- @evidence iterate update (state := 1, 1) (inputs := 1, 1; -1, -1)
    --   (parameter := 1/4) (steps := 200) (names := x, q, r, d, θ)

Lean prints the circuit in the exact V2 wire form, Python Asgard's wire
decoder validates the tree, it is expanded the same way, and the state is
iterated in floating point with the input vectors taken in turn, one per tick.

Evidence is never a proof (semantic invariant 5): Glitnir keeps it outside the
check records, and nothing that derives a claim's status reads it.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

# Points kept per state in a snapshot, as Glitnir accepts them; runs are
# downsampled to this.
from glitnir.notebook.evidence_cards import MAX_SAMPLES

_OPTION = re.compile(r"\(\s*(\w+)\s*:=\s*([^)]+?)\s*\)")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")
_STATE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
#: What the emission templates print before their JSON.
MARK = "forsetiEvidence:"
SOLVERS = ("euler", "rk4")
#: The evidence directives: a continuous model, or a discrete update circuit.
VERBS = ("simulate", "iterate")
MAX_STEPS = 100_000
#: Most field-term evaluations one run may do (steps × terms × solver stages),
#: so a large model cannot hold the server for minutes.
MAX_WORK = 20_000_000
#: Most terms one expanded output polynomial may have, and across all outputs;
#: highest total degree of a term; most term products one expansion may form;
#: and the longest numerator or denominator, in bits, so a hostile field cannot
#: expand without bound (repeated squaring doubles degree and digits, and
#: squaring a constant doubles digits at degree zero). Float64 cannot hold a
#: value much past 1024 bits, so nothing longer could be integrated anyway.
MAX_FIELD_TERMS = 4_096
MAX_FIELD_TOTAL_TERMS = 16_384
MAX_FIELD_DEGREE = 64
MAX_EXPANSION_WORK = 1_000_000
MAX_COEFFICIENT_BITS = 1_100
#: Most nodes across all of a field's emitted expression trees, the deepest
#: nesting of any one, and most states. The compiled right-hand sides of a
#: 12-mode Galerkin field are a few hundred nodes, deep in the tens; a hostile
#: field is refused before expansion. The state cap is the wire form's
#: `MAX_WIRES`, so the two emission forms admit the same models.
MAX_EXPRESSION_NODES = 65_536
MAX_EXPRESSION_DEPTH = 256
MAX_FIELD_STATES = 64
#: Most digits in each part of a number written in a directive option (its
#: integer part, and its decimal part or denominator). Every number token is
#: checked against `_NUMBER`, a plain decimal or fraction with no exponent,
#: before it is converted: Python's `Fraction` and `int` also take exponents and
#: other forms, and ``1e-999999999`` would hold the preview, which reads every
#: directive on each refresh. Forty digits are more than float64 resolves and
#: keep a step's exact value far inside the expansion's coefficient bound.
MAX_NUMBER_DIGITS = 40
_NUMBER = re.compile(
    rf"-?[0-9]{{1,{MAX_NUMBER_DIGITS}}}"
    rf"(\.[0-9]{{1,{MAX_NUMBER_DIGITS}}}|/[0-9]{{1,{MAX_NUMBER_DIGITS}}})?"
)
NOT_PLAIN = "numbers in evidence options must be plain decimals or fractions"
#: Most decimal digits in a numerator or denominator of an emitted initial
#: value or start, Asgard's own interchange bound (`MAX_DIGITS`).
MAX_RATIONAL_DIGITS = 512
_EMIT = """def _root_.forsetiExprJson {n : Nat} :
    Gimle.Asgard.Polynomial.Expr n → Lean.Json
  | .var i => Lean.toJson ["var", toString i.val]
  | .constant q => Lean.toJson ["const", toString q.num, toString q.den]
  | .add a b =>
    Lean.Json.arr #[Lean.toJson "add", forsetiExprJson a, forsetiExprJson b]
  | .mul a b =>
    Lean.Json.arr #[Lean.toJson "mul", forsetiExprJson a, forsetiExprJson b]
  | .neg a => Lean.Json.arr #[Lean.toJson "neg", forsetiExprJson a]

def _root_.forsetiEvidenceJson {b : Gimle.Asgard.Model.Body}
    {e : Gimle.Asgard.Model.Evolution}
    (p : Gimle.Asgard.Model.ContinuousModel b e) : Lean.Json :=
  let rat (q : ℚ) : Lean.Json := Lean.toJson [toString q.num, toString q.den]
  Lean.Json.mkObj [
    ("states", Lean.toJson (e.states.map (·.stateId))),
    ("initial", Lean.Json.arr
      ((List.finRange e.states.length).map fun i => rat (p.initials i)).toArray),
    ("start", rat e.start),
    ("axis", Lean.toJson e.axis.name),
    ("field", Lean.Json.arr
      ((List.finRange e.states.length).map fun i =>
        _root_.forsetiExprJson (p.rates.expressions i)).toArray)]

#eval IO.println ("{mark}" ++ (_root_.forsetiEvidenceJson {name}).compress)"""
_EMIT_CIRCUIT = """def _root_.forsetiEvidenceJson {n m : Nat}
    (c : Gimle.Asgard.Circuit n m) : Lean.Json :=
  Lean.Json.mkObj [
    ("inputs", Lean.toJson n),
    ("outputs", Lean.toJson m),
    ("circuit", Gimle.Asgard.Interchange.RationalWire.Tree.toJson
      (Gimle.Asgard.Interchange.RationalWire.encodeCircuit c))]

#eval IO.println ("{mark}" ++ (_root_.forsetiEvidenceJson {name}).compress)"""


def emission_command(verb: str, name: str) -> str:
    """Return the Lean that prints `MARK` and the field or circuit of ``name``.

    ``simulate`` prints a continuous model's field, ``iterate`` an update
    circuit. Only a Lean name is ever written into the template.

    Raises:
        EvidenceError: If ``verb`` is not an evidence verb or ``name`` is not a
            Lean name.
    """
    if verb not in VERBS:
        raise EvidenceError(f"unknown evidence {verb!r}; use simulate or iterate")
    if not _NAME.fullmatch(name):
        raise EvidenceError(f"{name!r} is not a Lean name")
    template = _EMIT_CIRCUIT if verb == "iterate" else _EMIT
    return template.replace("{mark}", MARK).replace("{name}", name)


class EvidenceError(ValueError):
    """Raised when a simulation cannot be run as declared."""


_Powers = tuple[int, ...]
_Polynomial = tuple[tuple[_Powers, Fraction], ...]


@dataclass(frozen=True)
class ExactPolynomials:
    """An emitted circuit expanded into exact polynomials, one per output.

    Each output is a tuple of ``(powers, coefficient)`` terms sorted by powers,
    with nonzero `Fraction` coefficients and one power per input port. It is a
    derived execution form for evidence, never a judgment binding.
    """

    input_degree: int
    output_degree: int
    outputs: tuple[_Polynomial, ...]


def exact_polynomials(node: Any) -> ExactPolynomials:
    """Expand a decoded ``ExactCircuit`` into exact polynomials.

    Raises:
        EvidenceError: If an output grows past `MAX_FIELD_TERMS` terms or
            `MAX_FIELD_DEGREE`, or the expansion past `MAX_EXPANSION_WORK`
            term products.
    """
    n = node.inputs
    budget = [MAX_EXPANSION_WORK]
    ports = tuple({tuple(int(i == j) for j in range(n)): Fraction(1)} for i in range(n))
    outputs = _expand(node, ports, n, budget)
    if sum(len(output) for output in outputs) > MAX_FIELD_TOTAL_TERMS:
        raise EvidenceError("the field is too large to expand")
    return ExactPolynomials(
        n,
        node.outputs,
        tuple(tuple(sorted(output.items())) for output in outputs),
    )


def expression_polynomials(trees: Any, n: int) -> ExactPolynomials:
    """Expand emitted expression trees, one per output, over ``n`` variables.

    A tree is ``["var", i]``, ``["const", num, den]``, ``["add", a, b]``,
    ``["mul", a, b]`` or ``["neg", a]``, the compiler's `Expr`. The numbers are
    decimal strings, as the wire form writes them.

    Raises:
        EvidenceError: If there are no states or more than `MAX_FIELD_STATES`,
            a tree is malformed, the trees together have more than
            `MAX_EXPRESSION_NODES` nodes or one nests deeper than
            `MAX_EXPRESSION_DEPTH`, or the expansion exceeds a bound.
    """
    if type(trees) is not list:
        raise EvidenceError("the field is a list of expression trees")
    if not 0 < n <= MAX_FIELD_STATES or len(trees) > MAX_FIELD_STATES:
        raise EvidenceError(f"a field has between 1 and {MAX_FIELD_STATES} states")
    nodes = [MAX_EXPRESSION_NODES]
    budget = [MAX_EXPANSION_WORK]
    outputs = tuple(_expand_expression(tree, n, 0, nodes, budget) for tree in trees)
    if sum(len(output) for output in outputs) > MAX_FIELD_TOTAL_TERMS:
        raise EvidenceError("the field is too large to expand")
    return ExactPolynomials(
        n, len(outputs), tuple(tuple(sorted(output.items())) for output in outputs)
    )


def _expand_expression(
    tree: Any, n: int, depth: int, nodes: list[int], budget: list[int]
) -> dict[_Powers, Fraction]:
    nodes[0] -= 1
    if nodes[0] < 0 or depth > MAX_EXPRESSION_DEPTH:
        raise EvidenceError("expression node/depth limit")
    if type(tree) is not list or not tree or type(tree[0]) is not str:
        raise EvidenceError("invalid expression node")
    tag = tree[0]
    arity = _ARITY.get(tag)
    if arity is None:
        raise EvidenceError(f"unknown expression tag {tag!r}")
    if len(tree) != arity + 1:
        raise EvidenceError(f"expression {tag!r} takes {arity} arguments")
    if tag == "var":
        index = _natural(tree[1])
        if index >= n:
            raise EvidenceError("expression variable out of range")
        return {tuple(int(i == index) for i in range(n)): Fraction(1)}
    if tag == "const":
        denominator = _natural(tree[2])
        if denominator == 0:
            raise EvidenceError("expression constant with zero denominator")
        coefficient = _sized(Fraction(_integer(tree[1]), denominator))
        return {(0,) * n: coefficient} if coefficient else {}
    if tag == "neg":
        inner = _expand_expression(tree[1], n, depth + 1, nodes, budget)
        return {p: -c for p, c in inner.items()}
    left = _expand_expression(tree[1], n, depth + 1, nodes, budget)
    right = _expand_expression(tree[2], n, depth + 1, nodes, budget)
    if tag == "mul":
        return _times(left, right, budget)
    total = dict(left)
    for powers, coefficient in right.items():
        total[powers] = _sized(total.get(powers, Fraction(0)) + coefficient)
    return _bounded({p: c for p, c in total.items() if c})


#: The expression tags and how many arguments each takes.
_ARITY = {"var": 1, "const": 2, "add": 2, "mul": 2, "neg": 1}


#: Exactly Lean's `toString` of an `Int`: no sign on zero, no leading zeros.
_DECIMAL = re.compile(r"^(0|-?[1-9][0-9]*)$")


def _integer(text: Any) -> int:
    """Read a decimal integer written as Lean writes it."""
    if type(text) is not str or len(text) > 400 or not _DECIMAL.fullmatch(text):
        raise EvidenceError("expression number is a decimal string")
    return int(text)


def _natural(text: Any) -> int:
    value = _integer(text)
    if value < 0:
        raise EvidenceError("expression number is a natural")
    return value


def _expand(
    node: Any,
    inputs: tuple[dict[_Powers, Fraction], ...],
    n: int,
    budget: list[int],
) -> tuple[dict[_Powers, Fraction], ...]:
    """Push input polynomials through one node of the exact tree."""
    tag = node.tag
    if tag == "compose":
        return _expand(node.right, _expand(node.left, inputs, n, budget), n, budget)
    if tag == "parallel":
        size = node.left.inputs
        return _expand(node.left, inputs[:size], n, budget) + _expand(
            node.right, inputs[size:], n, budget
        )
    if tag == "id":
        return inputs
    if tag == "terminal":
        return ()
    if tag == "split":
        return (inputs[0], inputs[0])
    if tag == "swap":
        return (inputs[1], inputs[0])
    if tag == "const":
        coefficient = _sized(node.coefficient)
        return ({(0,) * n: coefficient} if coefficient else {},)
    if tag == "scalar":
        return (_times(inputs[0], {(0,) * n: _sized(node.coefficient)}, budget),)
    if tag == "add":
        total = dict(inputs[0])
        for powers, coefficient in inputs[1].items():
            total[powers] = _sized(total.get(powers, Fraction(0)) + coefficient)
        return (_bounded({p: c for p, c in total.items() if c}),)
    if tag == "multiplication":
        return (_times(inputs[0], inputs[1], budget),)
    raise EvidenceError(f"unknown exact circuit tag {tag!r}")


def _times(
    left: dict[_Powers, Fraction], right: dict[_Powers, Fraction], budget: list[int]
) -> dict[_Powers, Fraction]:
    budget[0] -= len(left) * len(right)
    if budget[0] < 0:
        raise EvidenceError("the field is too large to expand")
    product: dict[_Powers, Fraction] = {}
    for a, x in left.items():
        for b, y in right.items():
            powers = tuple(i + j for i, j in zip(a, b))
            if sum(powers) > MAX_FIELD_DEGREE:
                raise EvidenceError("the field's degree is too high to expand")
            product[powers] = _sized(product.get(powers, Fraction(0)) + x * y)
    return _bounded({p: c for p, c in product.items() if c})


def _sized(value: Fraction) -> Fraction:
    """Refuse a coefficient too long to be worth expanding further."""
    if (
        value.numerator.bit_length() > MAX_COEFFICIENT_BITS
        or value.denominator.bit_length() > MAX_COEFFICIENT_BITS
    ):
        raise EvidenceError("a coefficient of the field is too large to expand")
    return value


def _bounded(polynomial: dict[_Powers, Fraction]) -> dict[_Powers, Fraction]:
    if len(polynomial) > MAX_FIELD_TERMS:
        raise EvidenceError("the field is too large to expand")
    return polynomial


def _float(value: Fraction) -> float:
    """Round an exact value to float64 as Asgard's `numerical_value` does.

    The numerator and denominator are each rounded, then divided; a value
    whose parts or quotient float64 cannot hold, or that underflows to zero,
    is refused rather than run.
    """
    try:
        result = float(value.numerator) / float(value.denominator)
    except OverflowError:
        result = math.inf
    if not math.isfinite(result) or (value and result == 0):
        raise EvidenceError(f"{value} cannot be run in float64")
    return result


def rounded(*groups: Any) -> list[str]:
    """List, once each and in order, the exact values float64 cannot hold."""
    found: dict[str, None] = {}
    for group in groups:
        for value in group:
            if Fraction(_float(value)) != value:
                found[str(value)] = None
    return list(found)


def coefficients(polynomials: ExactPolynomials) -> list[Fraction]:
    """Return every coefficient of the polynomials, output by output."""
    return [c for output in polynomials.outputs for _, c in output]


@dataclass(frozen=True)
class Simulation:
    """One evidence directive.

    Attributes:
        model: The Asgard ``ContinuousModel`` to simulate, or the update
            ``Circuit`` to iterate.
        solver: ``euler`` or ``rk4``.
        step: The step size, exact as written.
        steps: How many steps to take.
        error: Why it cannot be run as written, if it cannot.
        verb: ``simulate`` or ``iterate``.
        state: For ``iterate``, the initial state.
        inputs: For ``iterate``, the input vectors taken in turn, one per tick.
        parameter: For ``iterate``, the fixed parameters.
        names: For ``iterate``, a name for every port, state first.
        plane: For ``simulate``, two state names drawn against each other
            beside the time series.
    """

    model: str
    solver: str = "euler"
    step: Fraction = Fraction(1, 100)
    steps: int = 100
    error: str = ""
    verb: str = "simulate"
    state: tuple[Fraction, ...] = ()
    inputs: tuple[tuple[Fraction, ...], ...] = ()
    parameter: tuple[Fraction, ...] = ()
    names: tuple[str, ...] = ()
    plane: tuple[str, ...] = ()


def read_directive(verb: str, model: str, rest: str) -> Simulation:
    """Read one directive's verb, target and option text.

    The result's `error` says why it cannot be run as written, if it cannot.
    """
    pairs = _OPTION.findall(rest)
    options = dict(pairs)
    base = Simulation(model)
    if _OPTION.sub("", rest).strip():
        return _with_error(
            base, "options are written (h := 1/100) (steps := 400) (solver := rk4)"
        )
    for index, (key, _) in enumerate(pairs):
        if any(key == earlier for earlier, _ in pairs[:index]):
            return _with_error(base, f"option {key!r} is given twice")
    if verb not in VERBS:
        return _with_error(base, f"unknown evidence {verb!r}; use simulate or iterate")
    if not _NAME.fullmatch(model):
        return _with_error(base, f"{model!r} is not a Lean name")
    if verb == "iterate":
        return _iteration(base, options)
    unknown = sorted(set(options) - {"h", "steps", "solver", "plane"})
    if unknown:
        return _with_error(base, f"unknown option {unknown[0]!r}")
    plane: tuple[str, ...] = ()
    if "plane" in options:
        plane = tuple(name.strip() for name in options["plane"].split(","))
        if (
            len(plane) != 2
            or plane[0] == plane[1]
            or not all(_STATE_NAME.match(name) for name in plane)
        ):
            return _with_error(base, "plane names two states, as (plane := x, z)")
    try:
        step = _number(options.get("h", "1/100"))
        steps = _count(options.get("steps", "100"))
    except _NotPlain:
        return _with_error(base, NOT_PLAIN)
    except (ValueError, ZeroDivisionError):
        return _with_error(
            base, "the step h must be a number such as 1/100, and steps a count"
        )
    solver = options.get("solver", "euler")
    if solver not in SOLVERS:
        return _with_error(base, f"unknown solver {solver!r}; use euler or rk4")
    if step <= 0 or not 0 < steps <= MAX_STEPS:
        return _with_error(
            base, f"the step h must be positive, and steps 1–{MAX_STEPS}"
        )
    return Simulation(model, solver, step, steps, plane=plane)


def _iteration(base: Simulation, options: dict[str, str]) -> Simulation:
    """Read an ``iterate`` directive's options."""
    base = Simulation(base.model, verb="iterate")
    unknown = sorted(set(options) - {"state", "inputs", "parameter", "steps", "names"})
    if unknown:
        return _with_error(base, f"unknown option {unknown[0]!r}")
    if "state" not in options:
        return _with_error(base, "iterate needs an initial (state := …)")
    try:
        state = _vector(options["state"])
        rows = options.get("inputs", "").split(";")
        inputs = tuple(_vector(row) for row in rows) if options.get("inputs") else ((),)
        parameter = _vector(options.get("parameter", ""))
        steps = _count(options.get("steps", "100"))
    except _NotPlain:
        return _with_error(base, NOT_PLAIN)
    except (ValueError, ZeroDivisionError):
        return _with_error(
            base,
            "write numbers such as (state := 1, 1) (inputs := 1, 1; -1, -1) "
            "(parameter := 1/4) and steps as a count",
        )
    if not state or len({len(row) for row in inputs}) != 1:
        return _with_error(
            base, "the state is empty or the input vectors differ in size"
        )
    if not 0 < steps <= MAX_STEPS:
        return _with_error(base, f"steps must be 1–{MAX_STEPS}")
    names = tuple(n.strip() for n in options.get("names", "").split(",") if n.strip())
    ports = len(state) + len(inputs[0]) + len(parameter)
    if names and len(names) != ports:
        return _with_error(base, f"names must name all {ports} ports, state first")
    return Simulation(
        base.model,
        steps=steps,
        verb="iterate",
        state=state,
        inputs=inputs,
        parameter=parameter,
        names=names,
    )


class _NotPlain(ValueError):
    """Raised for a number token that is not a plain decimal or fraction."""


def _number(text: str) -> Fraction:
    """Read one exact number, checking its form before converting it.

    Raises:
        _NotPlain: If it is not a plain decimal or fraction within
            `MAX_NUMBER_DIGITS` digits per part.
        ZeroDivisionError: If its denominator is zero.
    """
    token = text.replace(" ", "")
    if not _NUMBER.fullmatch(token):
        raise _NotPlain(NOT_PLAIN)
    return Fraction(token)


def _count(text: str) -> int:
    """Read a whole number such as a step count."""
    value = _number(text)
    if value.denominator != 1:
        raise ValueError("a count is a whole number")
    return int(value)


def _vector(text: str) -> tuple[Fraction, ...]:
    """Read comma-separated exact numbers; an empty text is the empty vector."""
    return tuple(_number(part) for part in text.split(",") if part.strip())


def _with_error(simulation: Simulation, error: str) -> Simulation:
    return Simulation(
        simulation.model,
        error=error,
        verb=simulation.verb,
    )


#: A canonical positive denominator, as Lean's `toString` of a `Nat` writes it.
_POSITIVE = re.compile(r"^[1-9][0-9]*$")


def _rational(value: Any) -> Fraction:
    """Read an emitted ``[numerator, denominator]`` pair as Asgard reads it.

    Both are canonical decimal strings of at most `MAX_RATIONAL_DIGITS` digits,
    the denominator positive and the pair reduced.
    """
    if type(value) is not list or len(value) != 2:
        raise EvidenceError("a value is a [numerator, denominator] pair")
    numerator, denominator = value
    if (
        type(numerator) is not str
        or type(denominator) is not str
        or len(numerator.removeprefix("-")) > MAX_RATIONAL_DIGITS
        or len(denominator) > MAX_RATIONAL_DIGITS
        or not _DECIMAL.fullmatch(numerator)
        or not _POSITIVE.fullmatch(denominator)
    ):
        raise EvidenceError("a value is a canonical decimal pair")
    n, d = int(numerator), int(denominator)
    if math.gcd(n, d) != 1:
        raise EvidenceError("a rational value must be reduced")
    return Fraction(n, d)


def field_polynomials(
    emitted: dict[str, Any],
) -> tuple[ExactPolynomials, list[Fraction], Fraction]:
    """Read the emitted field's values and expand it exactly.

    Returns:
        The field's exact polynomials, the initial values and the start, all
        exact.

    Raises:
        EvidenceError: If the field cannot be read or expanded, or does not
            map the states to their derivatives.
    """
    missing = [key for key in ("initial", "start", "field") if key not in emitted]
    if missing:
        raise EvidenceError(f"the emitted field has no {missing[0]!r}")
    values = emitted["initial"]
    if type(values) is not list or not 0 < len(values) <= MAX_FIELD_STATES:
        raise EvidenceError(f"a field has between 1 and {MAX_FIELD_STATES} states")
    initial = [_rational(value) for value in values]
    start = _rational(emitted["start"])
    polynomials = expression_polynomials(emitted["field"], len(initial))
    states = len(initial)
    if polynomials.input_degree != states or polynomials.output_degree != states:
        raise EvidenceError("the field does not map the states to their derivatives")
    return polynomials, initial, start


def integrate(
    denotation: Any,
    initial: list[Fraction],
    start: Fraction,
    simulation: Simulation,
) -> dict[str, Any]:
    """Integrate the field with the declared solver, in floating point.

    Returns:
        ``times`` and ``values`` (one list per state), downsampled to at most
        `MAX_SAMPLES` points, and ``finite`` saying whether every value stayed
        a finite number.
    """
    terms = [
        [(powers, _float(coefficient)) for powers, coefficient in output]
        for output in denotation.outputs
    ]

    def field(x: list[float]) -> list[float]:
        derivative = []
        for output in terms:
            total = 0.0
            for powers, coefficient in output:
                product = coefficient
                for value, power in zip(x, powers):
                    product *= value**power
                total += product
            derivative.append(total)
        return derivative

    h = _float(simulation.step)
    x = [_float(v) for v in initial]
    t = _float(start)
    stride = max(1, -(-simulation.steps // (MAX_SAMPLES - 1)))
    times, values = [t], [[v] for v in x]
    finite = True
    for step in range(1, simulation.steps + 1):
        try:
            x = _step(field, x, h, simulation.solver)
        except OverflowError:
            finite = False
            break
        t = _float(start) + step * h
        if any(v != v or v in (float("inf"), float("-inf")) for v in x):
            finite = False
            break
        if step % stride == 0 or step == simulation.steps:
            times.append(t)
            for series, value in zip(values, x):
                series.append(value)
    return {"times": times, "values": values, "finite": finite}


def _step(
    field: Callable[[list[float]], list[float]],
    x: list[float],
    h: float,
    solver: str,
) -> list[float]:
    if solver == "euler":
        return [v + h * d for v, d in zip(x, field(x))]
    k1 = field(x)
    k2 = field([v + h / 2 * d for v, d in zip(x, k1)])
    k3 = field([v + h / 2 * d for v, d in zip(x, k2)])
    k4 = field([v + h * d for v, d in zip(x, k3)])
    return [
        v + h / 6 * (a + 2 * b + 2 * c + d) for v, a, b, c, d in zip(x, k1, k2, k3, k4)
    ]


def update_polynomials(
    emitted: dict[str, Any], simulation: Simulation
) -> ExactPolynomials:
    """Decode an emitted update circuit and check its ports match the directive.

    Raises:
        EvidenceError: If the circuit cannot be decoded, or its ports are not
            the state, the inputs and the parameters.
    """
    from gimle.asgard.rational_interchange import ExactCircuit, RationalError

    try:
        denotation = exact_polynomials(ExactCircuit.decode(emitted["circuit"]))
    except EvidenceError:
        raise
    except (KeyError, TypeError, ValueError, RationalError) as error:
        raise EvidenceError(f"Python Asgard could not read the circuit: {error}")
    states = len(simulation.state)
    ports = states + len(simulation.inputs[0]) + len(simulation.parameter)
    if denotation.input_degree != ports or denotation.output_degree != states:
        raise EvidenceError(
            f"the circuit maps {denotation.input_degree} ports to "
            f"{denotation.output_degree}, not {ports} (state, inputs, parameters) "
            f"to {states}"
        )
    return denotation


def iterate(denotation: Any, simulation: Simulation) -> dict[str, Any]:
    """Iterate the update in floating point, taking the inputs in turn.

    Returns:
        ``times`` (the ticks) and ``values`` (one list per state), downsampled
        to at most `MAX_SAMPLES` points, and ``finite``.
    """
    terms = [
        [(powers, _float(coefficient)) for powers, coefficient in output]
        for output in denotation.outputs
    ]
    parameter = [_float(v) for v in simulation.parameter]
    inputs = [[_float(v) for v in row] for row in simulation.inputs]
    x = [_float(v) for v in simulation.state]
    stride = max(1, -(-simulation.steps // (MAX_SAMPLES - 1)))
    times: list[float] = [0]
    values = [[v] for v in x]
    finite = True
    for tick in range(1, simulation.steps + 1):
        ports = x + inputs[(tick - 1) % len(inputs)] + parameter
        try:
            x = [_evaluate(output, ports) for output in terms]
        except OverflowError:
            finite = False
            break
        if any(v != v or v in (float("inf"), float("-inf")) for v in x):
            finite = False
            break
        if tick % stride == 0 or tick == simulation.steps:
            times.append(tick)
            for series, value in zip(values, x):
                series.append(value)
    return {"times": times, "values": values, "finite": finite}


def _evaluate(output: list[tuple[Any, float]], ports: list[float]) -> float:
    total = 0.0
    for powers, coefficient in output:
        product = coefficient
        for value, power in zip(ports, powers):
            product *= value**power
        total += product
    return total
