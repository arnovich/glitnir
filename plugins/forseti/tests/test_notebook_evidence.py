"""Tests for reading, expanding and running simulation evidence.

The directive reader, the exact expansion and the floating-point runs are
tested here on fields Lean emitted for the model fixtures
(``dissipative-field.json``). The provider that runs them for Glitnir is
tested in ``test_evidence_provider.py``.
"""

import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from glitnir_forseti.notebook import evidence

FIXTURES = Path(__file__).parent / "fixtures" / "notebooks"
FIELD = (FIXTURES / "dissipative-field.json").read_text(encoding="utf-8").strip()
#: The same field as the compiled circuit, in the wire form `iterate` uses.
CIRCUIT = (FIXTURES / "dissipative-circuit.json").read_text(encoding="utf-8").strip()
#: How Glitnir's core splits a directive line (`glitnir.notebook.evidence_cards`).
_DIRECTIVE = re.compile(r"--\s*@evidence\s+(\w+)\s+(\S+)(?:\s+(.*?))?\s*")
MODEL = "-- @evidence simulate dissipative (h := 1/100) (steps := 400)"


def _read(line: str) -> evidence.Simulation:
    """Read a directive line as the core hands it to the evidence provider."""
    match = _DIRECTIVE.fullmatch(line)
    assert match is not None, line
    verb, target, options = match.groups()
    return evidence.read_directive(verb, target, options or "")


# ----- directives ----------------------------------------------------------------


def test_the_directive_is_read_with_its_options() -> None:
    simulation = _read(MODEL)
    assert (simulation.model, simulation.solver) == ("dissipative", "euler")
    assert (simulation.step, simulation.steps) == (Fraction(1, 100), 400)
    assert simulation.error == ""


@pytest.mark.parametrize(
    ("directive", "error"),
    [
        ("-- @evidence plot dissipative", "unknown evidence"),
        ("-- @evidence simulate 1bad", "not a Lean name"),
        ("-- @evidence simulate m (dt := 1)", "unknown option"),
        ("-- @evidence simulate m (h := fast)", "must be plain decimals"),
        ("-- @evidence simulate m (h := 1/0)", "h must be a number"),
        ("-- @evidence simulate m (solver := ode45)", "unknown solver"),
        ("-- @evidence simulate m (h := -1)", "h must be positive"),
        ("-- @evidence simulate m (steps := 0)", "steps 1"),
        ("-- @evidence simulate m (steps := 100001)", "steps 1–100000"),
        ("-- @evidence simulate m (h := 1) (h := 2)", "option 'h' is given twice"),
        ("-- @evidence simulate m (plane := x)", "plane names two states"),
        ("-- @evidence simulate m (plane := x, x)", "plane names two states"),
        ("-- @evidence simulate m (plane := x, 2z)", "plane names two states"),
    ],
)
def test_malformed_directives_carry_an_error(directive: str, error: str) -> None:
    assert error in _read(directive).error


def test_a_plane_names_two_states() -> None:
    simulation = _read(MODEL + " (plane := x, q)")
    assert simulation.plane == ("x", "q")


@pytest.mark.parametrize("rest", ["h := 1/10", "(h: 1/10)", "(h := 1/10) extra"])
def test_stray_option_text_is_an_error(rest: str) -> None:
    assert "options are written" in _read(f"-- @evidence simulate m {rest}").error


# ----- reading and integrating the field ---------------------------------------


def test_the_emitted_field_is_read_exactly_without_asgard(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "gimle.asgard", None)
    denotation, initial, start = evidence.field_polynomials(json.loads(FIELD))
    assert denotation.outputs == ((((1,), Fraction(-1, 2)),),)
    assert initial == [Fraction(1)] and start == Fraction(0)
    with pytest.raises(evidence.EvidenceError, match="has no 'field'"):
        evidence.field_polynomials({"initial": [["1", "1"]], "start": ["0", "1"]})
    with pytest.raises(evidence.EvidenceError, match="numerator, denominator"):
        evidence.field_polynomials(
            {"field": [["var", "0"]], "initial": [["1", "1"]], "start": 0}
        )
    with pytest.raises(evidence.EvidenceError, match="between 1 and"):
        evidence.field_polynomials({"field": [], "initial": [], "start": ["0", "1"]})
    with pytest.raises(evidence.EvidenceError, match="invalid expression"):
        evidence.field_polynomials(
            {"field": ["nonsense"], "initial": [["1", "1"]], "start": ["0", "1"]}
        )
    mismatched = {**json.loads(FIELD), "initial": [["1", "1"], ["2", "1"]]}
    with pytest.raises(evidence.EvidenceError, match="does not map"):
        evidence.field_polynomials(mismatched)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (["2", "4"], "must be reduced"),
        (["1", "0"], "canonical"),
        (["01", "1"], "canonical"),
        (["-0", "1"], "canonical"),
        (["1", "-1"], "canonical"),
        ([1, "1"], "canonical"),
        (["1" * 513, "1"], "canonical"),
        (["1", "1", "1"], "pair"),
        ("1/2", "pair"),
    ],
)
def test_rational_values_are_read_under_asgards_interchange_rules(value, error) -> None:
    field = {"field": [["var", "0"]], "initial": [value], "start": ["0", "1"]}
    with pytest.raises(evidence.EvidenceError, match=error):
        evidence.field_polynomials(field)
    longest = ["-" + "1" * 512, "1"]
    assert evidence.field_polynomials({**field, "initial": [longest]})[1] == [
        -int("1" * 512)
    ]


def test_float64_rounding_follows_asgards_numerical_policy() -> None:
    """Numerator and denominator are rounded, then divided, as Asgard does."""
    assert evidence.rounded([Fraction(1, 64), Fraction(3)]) == []
    assert evidence.rounded([Fraction(1, 3), Fraction(1, 100)]) == ["1/3", "1/100"]
    for value in (Fraction(10**320), Fraction(1, 10**320), Fraction(10**400, 3)):
        with pytest.raises(evidence.EvidenceError, match="float64"):
            evidence.rounded([value])


def test_rk4_is_more_accurate_and_blow_ups_are_flagged() -> None:
    denotation, initial, start = evidence.field_polynomials(json.loads(FIELD))
    rk4 = evidence.Simulation("m", "rk4", Fraction(1, 10), 40)
    result = evidence.integrate(denotation, initial, start, rk4)
    assert abs(result["values"][0][-1] - math.exp(-2)) < 1e-6
    unstable = evidence.Simulation("m", "euler", Fraction(1000), 20000)
    blown = evidence.integrate(denotation, initial, start, unstable)
    assert blown["finite"] is False


# ----- discrete iteration ----------------------------------------------


WINDUP = next(
    line
    for line in (FIXTURES / "PIWindup.lean").read_text(encoding="utf-8").splitlines()
    if line.startswith("-- @evidence iterate")
)
UPDATE = (FIXTURES / "pi-windup-update.json").read_text(encoding="utf-8").strip()


def test_an_iterate_directive_is_read_with_its_ports() -> None:
    simulation = _read(WINDUP)
    assert simulation.verb == "iterate" and simulation.model == "update"
    assert simulation.state == (1, 1) and simulation.parameter == (Fraction(1, 4),)
    assert len(simulation.inputs) == 8 and simulation.inputs[0] == (1, -1)
    assert simulation.names == ("x", "q", "r", "d", "θ")


@pytest.mark.parametrize(
    ("directive", "error"),
    [
        ("-- @evidence iterate u (steps := 3)", "needs an initial (state"),
        ("-- @evidence iterate u (state := 1) (inputs := 1; 1, 2)", "differ in size"),
        ("-- @evidence iterate u (state := x)", "must be plain decimals"),
        ("-- @evidence iterate u (state := 1) (steps := 1/2)", "write numbers"),
        ("-- @evidence iterate u (state := 1) (steps := 0)", "steps must be"),
        ("-- @evidence iterate u (state := 1) (h := 1/2)", "unknown option 'h'"),
        ("-- @evidence iterate u (state := 1) (inputs := 2) (names := a)", "all 2"),
        ("-- @evidence fly u (state := 1)", "use simulate, iterate or sample"),
    ],
)
def test_malformed_iterate_directives_carry_an_error(directive, error) -> None:
    simulation = _read(directive)
    assert error in simulation.error
    assert simulation.verb == ("simulate" if " fly " in directive else "iterate")


def test_the_update_is_iterated_with_inputs_taken_in_turn() -> None:
    simulation = _read(
        "-- @evidence iterate u (state := 0) (inputs := 1; -1) (steps := 3)"
    )
    average = type(
        "Denotation",
        (),
        {"outputs": ((((1, 0), Fraction(1, 2)), ((0, 1), Fraction(1, 2))),)},
    )
    run = evidence.iterate(average, simulation)
    assert run["times"] == [0, 1, 2, 3]
    assert run["values"] == [[0.0, 0.5, -0.25, 0.375]]
    assert run["finite"] is True


# ----- coefficients with no finite decimal form -------------------------


THIRD = {
    "axis": "t",
    "field": [["mul", ["const", "-1", "3"], ["var", "0"]]],
    "initial": [["1", "1"]],
    "start": ["0", "1"],
    "states": ["state-x"],
}


def test_a_non_decimal_field_is_expanded_exactly() -> None:
    denotation, initial, start = evidence.field_polynomials(THIRD)
    assert denotation.outputs == ((((1,), Fraction(-1, 3)),),)
    simulation = evidence.Simulation("m", "euler", Fraction(1, 100), 400)
    result = evidence.integrate(denotation, initial, start, simulation)
    assert result["values"][0][-1] == pytest.approx((1 - 1 / 300) ** 400)


def _decimal(value: Fraction) -> str:
    """Write a coefficient with a finite decimal form as the circuit grammar does."""
    places = 0
    while (value * 10**places).denominator != 1:
        places += 1
        assert places <= 64, f"{value} has no finite decimal form"
    scaled = int(value * 10**places)
    sign, digits = ("-", str(-scaled)) if scaled < 0 else ("", str(scaled))
    if places == 0:
        return f"{sign}{digits}"
    digits = digits.rjust(places + 1, "0")
    return f"{sign}{digits[:-places]}.{digits[-places:]}"


def _asgard_circuit(node):
    """Build the typed Asgard circuit of a decoded exact wire tree, exactly."""
    from gimle.asgard.circuit import Circuit

    if node.tag == "compose":
        return _asgard_circuit(node.left).compose(_asgard_circuit(node.right))
    if node.tag == "parallel":
        return _asgard_circuit(node.left).parallel(_asgard_circuit(node.right))
    if node.tag in {"const", "scalar"}:
        return Circuit.from_string(f"{node.tag}({_decimal(node.coefficient)})")
    return Circuit.from_string(node.tag)


@pytest.mark.simulation
@pytest.mark.parametrize("fixture", [CIRCUIT, UPDATE])
def test_the_expansion_agrees_with_asgards_denotation(fixture: str) -> None:
    from gimle.asgard.circuit import multivariate_polynomial_denotation
    from gimle.asgard.rational_interchange import ExactCircuit

    tree = json.loads(fixture)["circuit"]
    asgard = multivariate_polynomial_denotation(
        _asgard_circuit(ExactCircuit.decode(tree))
    )
    ours = evidence.exact_polynomials(ExactCircuit.decode(tree))
    assert (ours.input_degree, ours.output_degree, ours.outputs) == (
        asgard.input_degree,
        asgard.output_degree,
        asgard.outputs,
    )


_ATOMICS = {
    (1, 1): ["id"],
    (2, 1): ["add", "multiplication"],
    (1, 2): ["split"],
    (2, 2): ["swap"],
    (1, 0): ["terminal"],
}
#: Most wires between two nodes of a random field, small enough that neither
#: expansion reaches its size limits on almost every draw.
_WIRES = 3
_DECIMALS = st.builds(
    lambda numerator, twos, fives: Fraction(numerator, 2**twos * 5**fives),
    st.integers(-(10**6), 10**6),
    st.integers(0, 6),
    st.integers(0, 6),
)


def _number(value: Fraction) -> list[str]:
    return [str(value.numerator), str(value.denominator)]


def _row(nodes: list[list]) -> list:
    """Put one or more nodes side by side."""
    tree = nodes[0]
    for node in nodes[1:]:
        tree = ["parallel", tree, node]
    return tree


@st.composite
def _wire_trees(draw: st.DrawFn, inputs: int, outputs: int, depth: int) -> list:
    """Draw a well-typed exact wire tree from ``inputs`` to ``outputs`` wires.

    Uses the node set an earlier fuzzing review used, with decimal coefficients. An
    atomic node of the right type is preferred; at depth zero with none, the
    wires are passed through, dropped or started from constants.
    """
    kinds = 2 * _ATOMICS.get((inputs, outputs), [])
    if (inputs, outputs) == (0, 1):
        kinds += ["const", "const"]
    if (inputs, outputs) == (1, 1):
        kinds += ["scalar", "scalar"]
    if depth > 0:
        kinds += ["compose", "compose", "parallel"]
    kind = draw(st.sampled_from(kinds or ["wiring"]))
    if kind in {"const", "scalar"}:
        return [kind, *_number(draw(_DECIMALS))]
    if kind == "compose":
        middle = draw(st.integers(0, _WIRES))
        return [
            "compose",
            draw(_wire_trees(inputs, middle, depth - 1)),
            draw(_wire_trees(middle, outputs, depth - 1)),
        ]
    if kind == "parallel":
        left_in = draw(st.integers(0, inputs))
        left_out = draw(st.integers(0, outputs))
        return [
            "parallel",
            draw(_wire_trees(left_in, left_out, depth - 1)),
            draw(_wire_trees(inputs - left_in, outputs - left_out, depth - 1)),
        ]
    if kind != "wiring":
        return [kind]
    made = [["const", *_number(draw(_DECIMALS))] for _ in range(outputs - inputs)]
    nodes = [["id"]] * min(inputs, outputs) + [["terminal"]] * (inputs - outputs)
    if not nodes + made:  # nothing in, nothing out
        return ["compose", ["const", "1", "1"], ["terminal"]]
    return _row(nodes + made)


_ARITIES = st.tuples(st.integers(0, _WIRES), st.integers(0, _WIRES))


@pytest.mark.simulation
@settings(max_examples=200, deadline=None)
@given(st.data())
def test_the_expansion_agrees_with_asgards_denotation_on_random_fields(
    data: st.DataObject,
) -> None:
    """Evidence's expansion and Asgard's cannot drift apart.

    Terms, coefficients and their order must be equal wherever both expand;
    each side keeps its own size limits, which these small fields rarely reach.
    """
    from gimle.asgard.circuit import multivariate_polynomial_denotation
    from gimle.asgard.circuit.multivariate import PolynomialDenotationError
    from gimle.asgard.rational_interchange import ExactCircuit

    inputs, outputs = data.draw(_ARITIES)
    node = ExactCircuit.decode(data.draw(_wire_trees(inputs, outputs, depth=4)))
    try:
        asgard = multivariate_polynomial_denotation(_asgard_circuit(node))
        ours = evidence.exact_polynomials(node)
    except (evidence.EvidenceError, PolynomialDenotationError):
        assume(False)
    assert (ours.input_degree, ours.output_degree) == (inputs, outputs)
    assert (ours.input_degree, ours.output_degree, ours.outputs) == (
        asgard.input_degree,
        asgard.output_degree,
        asgard.outputs,
    )


def _squared(tree: list, times: int) -> list:
    for _ in range(times):
        tree = ["mul", tree, tree]
    return tree


def test_an_expansion_over_its_limit_is_refused(monkeypatch) -> None:
    binomial = ["add", ["var", "0"], ["const", "1", "1"]]
    tree = _squared(binomial, 3)  # (x + 1)^8, nine terms
    monkeypatch.setattr(evidence, "MAX_FIELD_TERMS", 4)
    with pytest.raises(evidence.EvidenceError, match="too large"):
        evidence.field_polynomials({**THIRD, "field": [tree]})
    monkeypatch.setattr(evidence, "MAX_FIELD_TERMS", 10)
    monkeypatch.setattr(evidence, "MAX_EXPANSION_WORK", 2)
    with pytest.raises(evidence.EvidenceError, match="too large"):
        evidence.field_polynomials({**THIRD, "field": [tree]})


def test_repeated_squaring_is_stopped_by_the_degree_bound() -> None:
    tree = _squared(["var", "0"], 7)  # x^(2^7) = x^128, past MAX_FIELD_DEGREE
    with pytest.raises(evidence.EvidenceError, match="degree is too high"):
        evidence.field_polynomials({**THIRD, "field": [tree]})


@pytest.mark.parametrize(
    ("tree", "error"),
    [
        (["var", "1"], "out of range"),
        (["var", "-1"], "natural"),
        (["var", "-0"], "decimal string"),
        (["var", "01"], "decimal string"),
        (["const", "1", "0"], "zero denominator"),
        (["const", "1.5", "1"], "decimal string"),
        (["const", 1, "1"], "decimal string"),
        (["pow", ["var", "0"], "2"], "unknown expression tag"),
        (["add", ["var", "0"]], "'add' takes 2 arguments"),
        (["var", "0", "extra"], "'var' takes 1 arguments"),
        (["neg"], "'neg' takes 1 arguments"),
        (["const", "1", "-1"], "natural"),
        (["const", "1" * 401, "1"], "decimal string"),
        ([1], "invalid expression"),
        ([], "invalid expression"),
        ("x", "invalid expression"),
    ],
)
def test_malformed_expression_trees_are_refused(tree, error) -> None:
    with pytest.raises(evidence.EvidenceError, match=error):
        evidence.expression_polynomials([tree], 1)
    with pytest.raises(evidence.EvidenceError, match="list of expression trees"):
        evidence.expression_polynomials({"field": tree}, 1)


def test_the_total_term_and_coefficient_bounds_hold_across_trees(monkeypatch) -> None:
    binomial = ["add", ["var", "0"], ["const", "1", "1"]]
    trees = [_squared(binomial, 3)] * 4  # four outputs of nine terms each
    monkeypatch.setattr(evidence, "MAX_FIELD_TOTAL_TERMS", 30)
    with pytest.raises(evidence.EvidenceError, match="too large"):
        evidence.expression_polynomials(trees, 1)
    big = ["const", "9" * 340, "1"]
    with pytest.raises(evidence.EvidenceError, match="coefficient .* too large"):
        evidence.expression_polynomials([_squared(big, 2)], 1)


@pytest.mark.simulation
def test_the_fixture_field_is_the_fixture_circuit() -> None:
    """The expression fixture expands to exactly what the circuit fixture did."""
    from gimle.asgard.rational_interchange import ExactCircuit

    circuit = evidence.exact_polynomials(
        ExactCircuit.decode(json.loads(CIRCUIT)["circuit"])
    )
    field = evidence.expression_polynomials(json.loads(FIELD)["field"], 1)
    assert field.outputs == circuit.outputs


def test_the_emission_templates_name_their_forms() -> None:
    field = evidence.emission_command("simulate", "dissipative")
    assert "forsetiExprJson (p.rates.expressions i)" in field
    assert '("field", Lean.Json.arr' in field and "encodeCircuit" not in field
    assert field.endswith(
        '#eval IO.println ("forsetiEvidence:" ++ '
        "(_root_.forsetiEvidenceJson dissipative).compress)"
    )
    circuit = evidence.emission_command("iterate", "update")
    assert "(c : Gimle.Asgard.Circuit n m)" in circuit
    assert (
        "RationalWire.encodeCircuit c" in circuit and "forsetiExprJson" not in circuit
    )
    assert circuit.endswith("(_root_.forsetiEvidenceJson update).compress)")
    for verb, name in (("plot", "m"), ("simulate", "m) (x"), ("iterate", "")):
        with pytest.raises(evidence.EvidenceError):
            evidence.emission_command(verb, name)


def test_a_field_has_a_bounded_number_of_states() -> None:
    with pytest.raises(evidence.EvidenceError, match="between 1 and 64"):
        evidence.expression_polynomials([], 0)
    too_many = [["var", str(i)] for i in range(65)]
    with pytest.raises(evidence.EvidenceError, match="between 1 and 64"):
        evidence.expression_polynomials(too_many, 65)
    with pytest.raises(evidence.EvidenceError, match="between 1 and 64"):
        evidence.expression_polynomials(too_many, 1)
    sixty_four = evidence.expression_polynomials(too_many[:64], 64)
    assert sixty_four.output_degree == 64


def test_expression_trees_are_bounded_in_nodes_and_depth(monkeypatch) -> None:
    deep: list = ["var", "0"]
    for _ in range(300):
        deep = ["neg", deep]
    with pytest.raises(evidence.EvidenceError, match="node/depth"):
        evidence.expression_polynomials([deep], 1)
    monkeypatch.setattr(evidence, "MAX_EXPRESSION_NODES", 8)
    with pytest.raises(evidence.EvidenceError, match="node/depth"):
        evidence.expression_polynomials([_squared(["var", "0"], 3)], 1)


def _evaluate(tree: list, point: list[Fraction]) -> Fraction:
    tag = tree[0]
    if tag == "var":
        return point[int(tree[1])]
    if tag == "const":
        return Fraction(int(tree[1]), int(tree[2]))
    if tag == "neg":
        return -_evaluate(tree[1], point)
    left, right = _evaluate(tree[1], point), _evaluate(tree[2], point)
    return left + right if tag == "add" else left * right


_RATIONALS = st.fractions(min_value=-9, max_value=9, max_denominator=6)


def _expressions(n: int):
    leaf = st.one_of(
        st.integers(0, n - 1).map(lambda i: ["var", str(i)]),
        _RATIONALS.map(lambda q: ["const", str(q.numerator), str(q.denominator)]),
    )
    return st.recursive(
        leaf,
        lambda inner: st.one_of(
            st.tuples(inner, inner).map(lambda p: ["add", *p]),
            st.tuples(inner, inner).map(lambda p: ["mul", *p]),
            inner.map(lambda a: ["neg", a]),
        ),
        max_leaves=12,
    )


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_the_expression_expansion_evaluates_like_the_tree(data: st.DataObject) -> None:
    """The expanded polynomial and the tree agree exactly at rational points."""
    n = data.draw(st.integers(1, 3))
    trees = data.draw(st.lists(_expressions(n), min_size=1, max_size=3))
    point = data.draw(st.lists(_RATIONALS, min_size=n, max_size=n))
    polynomials = evidence.expression_polynomials(trees, n)
    assert polynomials.input_degree == n and polynomials.output_degree == len(trees)
    for tree, output in zip(trees, polynomials.outputs):
        value = sum(
            (
                coefficient * math.prod(x**p for x, p in zip(point, powers))
                for powers, coefficient in output
            ),
            Fraction(0),
        )
        assert value == _evaluate(tree, point)


def _squarings(digits: int, times: int):
    from gimle.asgard.rational_interchange import ExactCircuit

    square = ["compose", ["split"], ["multiplication"]]
    tree = ["const", "9" * digits, "1"]
    for _ in range(times):
        tree = ["compose", tree, square]
    return ExactCircuit.decode(["compose", ["terminal"], tree])


@pytest.mark.simulation
def test_squaring_a_constant_cannot_grow_its_digits_without_bound() -> None:
    """Degree stays zero, so only the coefficient cap stops the growth."""
    import time

    started = time.monotonic()
    with pytest.raises(evidence.EvidenceError, match="coefficient .* too large"):
        evidence.exact_polynomials(_squarings(100, 24))
    assert time.monotonic() - started < 5
    small = evidence.exact_polynomials(_squarings(100, 1))
    assert small.outputs[0][0][1] == Fraction(10**100 - 1) ** 2
    with pytest.raises(evidence.EvidenceError, match="coefficient"):
        evidence.exact_polynomials(_squarings(512, 0))
