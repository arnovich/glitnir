"""Tests for the textual goal syntax.

Every corpus goal must survive render-then-parse unchanged, since the notebook
and the agent write goals as text and the deciders regenerate Lean from the
parsed value.
"""

import dataclasses
import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from glitnir_forseti.claims.text import (
    MAX_EXPONENT,
    MAX_TEXT,
    RESERVED,
    GoalTextError,
    parse_goal,
    render_goal,
    render_short_goal,
)
from glitnir_forseti.entailment import syntax as entailment
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import (
    Add,
    And,
    Atom,
    Const,
    Context,
    Fls,
    Goal,
    GoalError,
    Mul,
    Neg,
    Not,
    Or,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.hedging.goal import HedgingGoal
from glitnir_forseti.stepped.goal import SteppedGoal
from glitnir_forseti.streams import goal as stream
from glitnir_forseti.streams.goal import FieldClaim, StreamGoal, TrigBandClaim, band_of
from glitnir_forseti.trajectory import goal as trajectory
from glitnir_forseti.trajectory.examples import BAND, energy_goal, forced_goal
from glitnir_forseti.trajectory.goal import (
    INTERPRETATIONS,
    OBLIGATIONS,
    QUANTIFIERS,
    TrajectoryGoal,
)
from glitnir_forseti.trajectory.models import MODELS

BENCHMARKS = Path(__file__).parents[1] / "benchmarks"
LIMITS = EntailmentLimits()
DECODERS = {
    "entailment": entailment.decode_goal,
    "trajectory": trajectory.decode_goal,
    "streams": stream.decode_goal,
    "stepped": SteppedGoal.from_json,
    "hedging": HedgingGoal.from_json,
}
CORPUS = sorted(
    (kind, path)
    for kind in DECODERS
    for path in (BENCHMARKS / kind / "goals").glob("*.json")
)
X = Context(("x",))
XY = Context(("x", "y"))


def test_the_corpus_is_not_empty() -> None:
    assert {kind for kind, _ in CORPUS} == set(DECODERS)


@pytest.mark.parametrize(("kind", "path"), CORPUS, ids=lambda v: getattr(v, "stem", v))
def test_corpus_goals_round_trip_through_text(kind: str, path: Path) -> None:
    goal = DECODERS[kind](json.loads(path.read_text()), LIMITS)
    text = render_goal(goal)
    assert parse_goal(text) == goal
    assert render_goal(parse_goal(text)) == text


def test_an_entailment_reads_as_written() -> None:
    goal = parse_goal("entailment x : x*x = 2 |- x < 0")
    assert goal == Goal(
        X,
        Atom(Relation.EQ, Add(Mul(Var(0), Var(0)), Neg(Const(Fraction(2))))),
        Atom(Relation.LT, Var(0)),
    )


def test_unicode_and_ascii_spellings_agree() -> None:
    ascii_text = "entailment x, y : x <= 1 and not y >= 0 or false |- true"
    unicode_text = "entailment x, y : x ≤ 1 ∧ ¬ y ≥ 0 ∨ false ⊢ true"
    assert parse_goal(ascii_text) == parse_goal(unicode_text)


@pytest.mark.parametrize(
    ("text", "formula"),
    [
        # not > and > or
        (
            "not x = 0 and y = 0 or x > 0",
            Or(
                And(Not(Atom(Relation.EQ, Var(0))), Atom(Relation.EQ, Var(1))),
                Atom(Relation.GT, Var(0)),
            ),
        ),
        (
            "x = 0 and (y = 0 or x > 0)",
            And(
                Atom(Relation.EQ, Var(0)),
                Or(Atom(Relation.EQ, Var(1)), Atom(Relation.GT, Var(0))),
            ),
        ),
        ("true and false", And(Tru(), Fls())),
    ],
)
def test_connective_precedence(text: str, formula) -> None:
    assert parse_goal(f"entailment x, y : {text} |- true").antecedent == formula


@pytest.mark.parametrize(
    ("text", "poly"),
    [
        ("-x*y", Mul(Neg(Var(0)), Var(1))),
        ("-(x*y)", Neg(Mul(Var(0), Var(1)))),
        ("x - y - 1", Add(Add(Var(0), Neg(Var(1))), Neg(Const(Fraction(1))))),
        ("x - (y - 1)", Add(Var(0), Neg(Add(Var(1), Neg(Const(Fraction(1))))))),
        ("x^3", Mul(Mul(Var(0), Var(0)), Var(0))),
        ("-x^2", Neg(Mul(Var(0), Var(0)))),
        ("(-3/2)*x", Mul(Const(Fraction(-3, 2)), Var(0))),
        ("-3/2*x", Mul(Neg(Const(Fraction(3, 2))), Var(0))),
        ("0.25 + ( - 1 )", Add(Const(Fraction(1, 4)), Const(Fraction(-1)))),
    ],
)
def test_polynomial_structure(text: str, poly) -> None:
    goal = parse_goal(f"entailment x, y : {text} = 0 |- true")
    assert goal.antecedent == Atom(Relation.EQ, poly)


def test_a_right_hand_side_other_than_zero_is_subtracted() -> None:
    goal = parse_goal("entailment x : x <= 0 and x <= 0/1 and x <= (- 0) |- x >= 1")
    zero_atom = Atom(Relation.LE, Var(0))
    assert goal.antecedent == And(And(zero_atom, zero_atom), zero_atom)
    assert goal.consequent == Atom(Relation.GE, Add(Var(0), Neg(Const(Fraction(1)))))
    literal = parse_goal("entailment x : x - 0 <= 0 |- true").antecedent
    assert literal == Atom(Relation.LE, Add(Var(0), Neg(Const(Fraction(0)))))


def test_a_negative_constant_allows_any_spacing() -> None:
    goal = parse_goal("entailment x : x = (-\t3 / 2 ) |- true".replace("3 / 2", "3/2"))
    assert goal.antecedent == Atom(
        Relation.EQ, Add(Var(0), Neg(Const(Fraction(-3, 2))))
    )


def test_initial_ports_may_be_given_in_any_order() -> None:
    goal = parse_goal(
        "trajectory three-state interpretation continuous-real obligation contract"
        " start time = 2 initial initial-z = -1, initial-x = 1, initial-y = 2"
        " observe obs-v as V always : V - 0 >= 0 and V <= 6"
    )
    assert goal == energy_goal(BAND)


@pytest.mark.parametrize("word", sorted(RESERVED))
def test_keyword_names_are_refused_both_ways(word: str) -> None:
    with pytest.raises(GoalTextError):
        parse_goal(f"entailment x, {word} : {word} = 0 |- true")
    goal = Goal(Context(("x", word)), Atom(Relation.EQ, Var(1)), Tru())
    with pytest.raises(GoalTextError, match="keyword"):
        render_goal(goal)


def test_a_parse_error_says_what_was_expected() -> None:
    with pytest.raises(GoalTextError, match="expected 'from', 'interpretation'"):
        parse_goal("trajectory three-state observe obs-v as V always : true")
    with pytest.raises(GoalTextError, match="a comparison"):
        parse_goal("entailment x : x != 1 |- true")


def test_keywords_do_not_swallow_names_that_start_with_them() -> None:
    goal = parse_goal("entailment notable, android, order : notable = android |- true")
    assert goal.context.names == ("notable", "android", "order")


def test_a_trajectory_goal_reads_as_written() -> None:
    text = (
        "trajectory three-state interpretation continuous-real obligation contract"
        " start time = 2 initial initial-x = 1, initial-y = 2, initial-z = -1"
        " observe obs-v as V always : V - 0 >= 0 and V <= 6"
    )
    goal = parse_goal(text)
    assert goal == energy_goal(BAND)


def test_a_field_goal_reads_as_written() -> None:
    goal = parse_goal(
        "stream formal-heat interpretation evaluated-field basis ogf profile x^2"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0 and u <= 3"
    )
    assert isinstance(goal, StreamGoal)
    assert goal.claim == FieldClaim(
        "u",
        (Fraction(0), Fraction(1)),
        (Fraction(-1), Fraction(1)),
        And(
            Atom(Relation.GE, Var(2)),
            Atom(Relation.LE, Add(Var(2), Neg(Const(Fraction(3))))),
        ),
    )


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("entailment x : x < 0", "line 1, column"),
        ("entailment x : y < 0 |- true", "unknown coordinate 'y'"),
        ("entailment x, x : x < 0 |- true", "distinct"),
        ("entailment x : x/2 < 0 |- true", "line 1"),
        ("entailment x : x^0 = 1 |- true", "exponent"),
        (f"entailment x : x^{MAX_EXPONENT + 1} = 1 |- true", "exponent"),
        ("entailment x : x = 1/0 |- true", "exact rational"),
        ("theorem x : x = 1 |- true", "line 1, column 1"),
        (
            "stream formal-heat interpretation evaluated-field basis ogf profile x"
            " field u over x in [0, 1], t in [0, 1] : true",
            "over t, then x",
        ),
        (
            "stream formal-heat interpretation evaluated-field basis ogf profile x"
            " field u over t in [1, 0], x in [0, 1] : true",
            "ordered",
        ),
        (
            "trajectory m interpretation continuous-real obligation contract"
            " start time = 0 initial a = 1, a = 2 observe o as V always : true",
            "distinct",
        ),
        (
            "trajectory m interpretation sideways obligation contract"
            " start time = 0 initial a = 1 observe o as V always : true",
            "interpretation",
        ),
    ],
)
def test_malformed_text_is_an_error_not_a_guess(text: str, match: str) -> None:
    with pytest.raises(GoalTextError, match=match):
        parse_goal(text)


def test_errors_are_goal_errors() -> None:
    assert issubclass(GoalTextError, GoalError)


def test_hostile_sizes_are_refused() -> None:
    with pytest.raises(GoalTextError, match="exceeds"):
        parse_goal("entailment x : " + "x + " * MAX_TEXT + "x = 0 |- true")
    deep = "-" * 20000 + "x"
    with pytest.raises(GoalError):
        parse_goal(f"entailment x : {deep} = 0 |- true")
    with pytest.raises(GoalTextError, match="string"):
        parse_goal(b"entailment x : x = 0 |- true")  # type: ignore[arg-type]


def test_rendering_a_non_goal_is_a_type_error() -> None:
    with pytest.raises(TypeError):
        render_goal("entailment x : x = 0 |- true")  # type: ignore[arg-type]


_NAMES = ("x", "y", "notable", "android", "field", "over", "as")
_POLYS = st.recursive(
    st.one_of(
        st.integers(0, len(_NAMES) - 1).map(Var),
        st.fractions(max_denominator=7).map(Const),
    ),
    lambda inner: st.one_of(
        st.builds(Add, inner, inner),
        st.builds(Mul, inner, inner),
        st.builds(Neg, inner),
    ),
    max_leaves=8,
)
_FORMULAS = st.recursive(
    st.one_of(
        st.just(Tru()),
        st.just(Fls()),
        st.builds(Atom, st.sampled_from(list(Relation)), _POLYS),
    ),
    lambda inner: st.one_of(
        st.builds(And, inner, inner),
        st.builds(Or, inner, inner),
        st.builds(Not, inner),
    ),
    max_leaves=6,
)


@settings(max_examples=400, deadline=None)
@given(_FORMULAS, _FORMULAS)
def test_any_entailment_round_trips(antecedent, consequent) -> None:
    goal = Goal(Context(_NAMES), antecedent, consequent)
    assert parse_goal(render_goal(goal)) == goal


def test_a_truncation_claim_reads_as_written() -> None:
    from glitnir_forseti.streams.goal import ExpSum, TruncationClaim

    goal = parse_goal(
        "stream formal-heat interpretation analytic-tail basis egf profile"
        " exponentials [1/2, 1], [1/2, -1] truncation u window 8, 16"
        " radius 1/4, 0.5 epsilon 1/10000"
    )
    assert goal == StreamGoal(
        "formal-heat",
        "analytic-tail",
        "egf",
        ExpSum(((Fraction(1, 2), Fraction(1)), (Fraction(1, 2), Fraction(-1)))),
        TruncationClaim(
            "u", (8, 16), (Fraction(1, 4), Fraction(1, 2)), Fraction(1, 10000)
        ),
    )


def test_a_viscous_goal_reads_and_renders_as_written() -> None:
    from glitnir_forseti.streams.goal import ExpSum, TruncationClaim

    text = (
        "stream burgers viscosity 1/2 interpretation analytic-tail basis ogf potential"
        " exponentials [1, 0], [1, -1] truncation u window 16, 16"
        " radius 1/6, 1/8 epsilon 1/10000"
    )
    goal = parse_goal(text)
    assert goal == StreamGoal(
        "burgers",
        "analytic-tail",
        "ogf",
        ExpSum(((Fraction(1), Fraction(0)), (Fraction(1), Fraction(-1)))),
        TruncationClaim(
            "u", (16, 16), (Fraction(1, 6), Fraction(1, 8)), Fraction(1, 10000)
        ),
        Fraction(1, 2),
    )
    assert render_goal(goal) == text
    heat = text.replace("viscosity 1/2 ", "").replace("potential", "profile")
    assert parse_goal(heat).viscosity is None
    for wrong in (
        text.replace("potential", "profile"),
        heat.replace("profile", "potential"),
    ):
        with pytest.raises(GoalTextError, match="potential"):
            parse_goal(wrong)
    with pytest.raises(GoalTextError, match="number"):
        parse_goal(text.replace("viscosity 1/2", "viscosity"))


@pytest.mark.parametrize("viscosity", ["-1", "0", "1", "3/2", "-1/2"])
def test_any_viscosity_round_trips(viscosity: str) -> None:
    text = (
        f"stream burgers viscosity {viscosity} interpretation analytic-tail basis ogf"
        " potential exponentials [1, 0], [1, -1] truncation u window 16, 16"
        " radius 1/6, 1/8 epsilon 1/10000"
    )
    goal = parse_goal(text)
    assert isinstance(goal, StreamGoal) and goal.viscosity == Fraction(viscosity)
    assert render_goal(goal) == text
    assert parse_goal(render_goal(goal)) == goal


# ----- the short trajectory form ------------------------------------

LONG = (
    "trajectory three-state interpretation continuous-real obligation contract"
    " start time = 2 initial initial-x = 1, initial-y = 2, initial-z = -1"
    " observe obs-v as V always : V - 0 >= 0 and V <= 6"
)
SHORT = (
    "trajectory three-state from (1, 2, -1) at 2 observe V always"
    " : V - 0 >= 0 and V <= 6"
)


def test_the_short_form_reads_as_the_long_form() -> None:
    assert parse_goal(SHORT) == parse_goal(LONG) == energy_goal(BAND)


def test_the_short_form_takes_values_in_the_models_state_order() -> None:
    goal = parse_goal(
        "trajectory three-state from (-1, 3/2, 0.5) at -1/2 observe V"
        " eventually : V >= 1"
    )
    assert isinstance(goal, TrajectoryGoal)
    assert goal.initial == (
        ("initial-x", Fraction(-1)),
        ("initial-y", Fraction(3, 2)),
        ("initial-z", Fraction(1, 2)),
    )
    assert (goal.start, goal.quantifier) == (Fraction(-1, 2), "eventually")
    assert (goal.interpretation, goal.obligation) == ("continuous-real", "contract")
    assert (goal.axis, goal.observation, goal.name) == ("time", "obs-v", "V")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "trajectory roessler from (1, 2, 3) at 0 observe V always : true",
            "no trajectory model 'roessler'.*three-state",
        ),
        (
            "trajectory three-state from (1, 2) at 2 observe V always : true",
            "3 initial values.*initial-x, initial-y, initial-z.*got 2",
        ),
        (
            "trajectory three-state from (1, 2, 3, 4) at 2 observe V always : true",
            "3 initial values",
        ),
        (
            "trajectory three-state from (1, 2, -1) at 2 observe E always : E >= 0",
            "no observation named 'E'.*V",
        ),
        (
            "trajectory three-state from (1, 2, -1) at 2 observe V always : W >= 0",
            "unknown coordinate 'W'",
        ),
        (
            "trajectory three-state from (1, 2, -1) observe V always : true",
            "expected 'at'",
        ),
        (
            "trajectory three-state from 1, 2, -1 at 2 observe V always : true",
            "line 1",
        ),
    ],
)
def test_the_short_form_refuses_what_the_registry_does_not_have(
    text: str, match: str
) -> None:
    with pytest.raises(GoalTextError, match=match):
        parse_goal(text)


def test_an_observation_name_two_bands_share_is_refused(monkeypatch) -> None:
    entry = MODELS["three-state"]
    band = entry.observations["obs-v"]
    twin = dataclasses.replace(band, port="obs-w", index=2)
    doubled = dataclasses.replace(entry, observations={"obs-v": band, "obs-w": twin})
    monkeypatch.setitem(MODELS, "three-state", doubled)
    with pytest.raises(GoalTextError, match="ambiguous"):
        parse_goal(SHORT)
    assert render_short_goal(energy_goal(BAND)) is None
    # The long form names the port, so it still reads.
    assert parse_goal(LONG) == energy_goal(BAND)


def test_a_keyword_observation_has_no_short_form(monkeypatch) -> None:
    entry = MODELS["three-state"]
    band = dataclasses.replace(entry.observations["obs-v"], name="not")
    monkeypatch.setitem(
        MODELS, "three-state", dataclasses.replace(entry, observations={"obs-v": band})
    )
    goal = energy_goal(Tru(), name="not")
    with pytest.raises(GoalTextError, match="keyword"):
        render_short_goal(goal)
    with pytest.raises(GoalTextError, match="keyword"):
        parse_goal(
            "trajectory three-state from (1, 2, -1) at 2 observe not always : true"
        )


def test_the_long_form_needs_no_registry() -> None:
    goal = parse_goal(
        "trajectory roessler interpretation continuous-real obligation contract"
        " start t = 0 initial a = 1 observe o as V always : true"
    )
    assert goal.model == "roessler"
    assert render_short_goal(goal) is None


def test_rendering_writes_the_long_form_and_the_short_form_on_request() -> None:
    goal = energy_goal(BAND)
    assert render_goal(goal).startswith("trajectory three-state interpretation")
    short = render_short_goal(goal)
    assert short == (
        "trajectory three-state from (1, 2, -1) at 2 observe V always :"
        " V - 0 >= 0 and V <= 6"
    )
    assert parse_goal(short) == goal
    assert render_goal(parse_goal(short)) == render_goal(goal)


@pytest.mark.parametrize(
    "change",
    [
        {"interpretation": "stochastic-ito"},
        {"obligation": "holds"},
        {"initial": (("initial-x", Fraction(1)),)},
        {"observation": "obs-x"},
        {"name": "E"},
        {"model": "roessler"},
    ],
)
def test_a_goal_the_short_form_cannot_say_has_none(change) -> None:
    goal = dataclasses.replace(energy_goal(BAND), **change)
    assert render_short_goal(goal) is None
    assert parse_goal(render_goal(goal)) == goal


def test_only_trajectory_goals_have_a_short_form() -> None:
    goal = parse_goal("entailment x : x = 0 |- true")
    assert render_short_goal(goal) is None
    with pytest.raises(TypeError):
        render_short_goal("trajectory")  # type: ignore[arg-type]


_V_POLYS = st.recursive(
    st.one_of(st.just(Var(0)), st.fractions(max_denominator=7).map(Const)),
    lambda inner: st.one_of(
        st.builds(Add, inner, inner),
        st.builds(Mul, inner, inner),
        st.builds(Neg, inner),
    ),
    max_leaves=6,
)
_V_FORMULAS = st.recursive(
    st.one_of(
        st.just(Tru()),
        st.builds(Atom, st.sampled_from(list(Relation)), _V_POLYS),
    ),
    lambda inner: st.one_of(
        st.builds(And, inner, inner),
        st.builds(Or, inner, inner),
        st.builds(Not, inner),
    ),
    max_leaves=5,
)
_RATIONALS = st.fractions(max_denominator=9, min_value=-50, max_value=50)


@settings(max_examples=300, deadline=None)
@given(
    _RATIONALS,
    st.tuples(_RATIONALS, _RATIONALS, _RATIONALS),
    st.sampled_from(QUANTIFIERS),
    st.sampled_from(INTERPRETATIONS),
    st.sampled_from(OBLIGATIONS),
    _V_FORMULAS,
)
def test_any_trajectory_goal_round_trips_in_both_forms(
    start, values, quantifier, interpretation, obligation, post
) -> None:
    entry = MODELS["three-state"]
    goal = energy_goal(
        post,
        start=start,
        initial=tuple(zip(entry.initial_ids, values)),
        quantifier=quantifier,
        interpretation=interpretation,
        obligation=obligation,
    )
    assert parse_goal(render_goal(goal)) == goal
    short = render_short_goal(goal)
    defaults = (interpretation, obligation) == ("continuous-real", "contract")
    assert (short is not None) == defaults
    if short is not None:
        assert parse_goal(short) == goal


# ----- driver preconditions ---------------------------------------

DRIVEN_LONG = (
    "trajectory forced-decay interpretation continuous-real obligation contract"
    " start time = 0 initial initial-x = 0 drivers driver-u within 1/2"
    " observe obs-x as x always : x <= 2"
)
DRIVEN_SHORT = (
    "trajectory forced-decay from (0) at 0 within 1/2 observe x always : x <= 2"
)


def test_a_driver_bound_reads_in_both_forms() -> None:
    expected = forced_goal(
        parse_goal(DRIVEN_LONG).post,  # type: ignore[union-attr]
        drivers=(("driver-u", Fraction(1, 2)),),
    )
    assert parse_goal(DRIVEN_LONG) == parse_goal(DRIVEN_SHORT) == expected
    assert render_goal(expected) == DRIVEN_LONG
    assert render_short_goal(expected) == DRIVEN_SHORT


def test_a_driven_goal_without_bounds_reads_without_drivers() -> None:
    goal = parse_goal("trajectory forced-decay from (0) at 0 observe x always : x <= 2")
    assert isinstance(goal, TrajectoryGoal) and goal.drivers == ()
    assert render_short_goal(goal) is not None


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "trajectory three-state from (1, 2, -1) at 2 within 1 observe V"
            " always : true",
            "three-state has no drivers",
        ),
        (
            DRIVEN_LONG.replace("within 1/2", "within 1/2, driver-u within 1"),
            "distinct",
        ),
        (DRIVEN_LONG.replace("within 1/2", "within -1"), "nonnegative"),
    ],
)
def test_malformed_driver_bounds_are_refused_as_text(text: str, match: str) -> None:
    with pytest.raises(GoalTextError, match=match):
        parse_goal(text)


def test_the_short_form_needs_the_models_own_bounded_driver() -> None:
    goal = parse_goal(DRIVEN_LONG)
    assert isinstance(goal, TrajectoryGoal)
    other = replace(goal, drivers=(("driver-du", Fraction(1)),))
    assert render_short_goal(other) is None
    undriven = replace(energy_goal(BAND), drivers=(("driver-u", Fraction(1)),))
    assert render_short_goal(undriven) is None
    assert parse_goal(render_goal(other)) == other


def test_a_field_band_about_the_front_reads_as_written() -> None:
    from glitnir_forseti.streams.examples import band_goal, front_band

    text = (
        "stream burgers viscosity 1/2 interpretation analytic-field basis ogf potential"
        " exponentials [1, 0], [1, -1] field u over t in [-1/6, 1/6], x in [-1/8, 1/8]"
        " : u >= 2/5 and u <= 3/5"
    )
    goal = parse_goal(text)
    assert goal == band_goal(front_band(Fraction(2, 5), Fraction(3, 5)))
    assert render_goal(goal) == text
    upper = parse_goal(text.rsplit(" : ", 1)[0] + " : u <= 3/5")
    assert upper == band_goal(front_band(None, Fraction(3, 5)))


def test_an_euler_goal_reads_as_written_and_renders_back() -> None:
    from glitnir_forseti.streams.examples import (
        trig_band,
        trig_truncation,
        vorticity_goal,
    )

    start = (
        "start modes [1, 0, 1/2], [-1, 0, 1/2], [1, 1, 1/2], [-1, -1, 1/2],"
        " [2, 1, 1/2],"
        " [-2, -1, 1/2]"
    )
    text = (
        f"stream vorticity viscosity 0 interpretation trig-truncation basis ogf {start}"
        " truncation u window 3 radius 1/6480 epsilon 1/100"
    )
    goal = parse_goal(text)
    assert goal == vorticity_goal(trig_truncation(Fraction(1, 100)))
    assert render_goal(goal) == text
    band = (
        f"stream vorticity viscosity 0 interpretation trig-band basis ogf {start}"
        " band u radius 1/6480 : u >= (-31/10) and u <= 31/10"
    )
    goal = parse_goal(band)
    assert goal == vorticity_goal(trig_band(Fraction(-31, 10), Fraction(31, 10)))
    assert render_goal(goal) == band
    # `-31/10` outside parentheses is the negation of 31/10: another value,
    # the same band.
    spelled = parse_goal(band.replace("(-31/10)", "-31/10"))
    assert isinstance(spelled, StreamGoal) and isinstance(spelled.claim, TrigBandClaim)
    assert band_of(spelled.claim.post) == (Fraction(-31, 10), Fraction(31, 10))
    upper = parse_goal(band.rsplit(" : ", 1)[0] + " : u <= 3")
    assert upper == vorticity_goal(trig_band(None, Fraction(3)))


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            "stream vorticity interpretation trig-truncation basis ogf start modes"
            " [1, 0, 1/2] truncation u window 3 radius 1/6480 epsilon 1/100",
            "names its viscosity",
        ),
        (
            "stream vorticity viscosity 0 interpretation trig-truncation basis ogf"
            " start modes [1/2, 0, 1/2] truncation u window 3 radius 1/6480"
            " epsilon 1/100",
            "integer mode index",
        ),
        (
            "stream vorticity viscosity 0 interpretation trig-truncation basis ogf"
            " profile x*x truncation u window 3 radius 1/6480 epsilon 1/100",
            "potential",
        ),
    ],
)
def test_malformed_euler_texts_are_refused(text, match) -> None:
    with pytest.raises(GoalTextError, match=match):
        parse_goal(text)
