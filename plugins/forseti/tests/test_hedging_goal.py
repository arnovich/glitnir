"""Tests for the hedging goal: its grammar, its text form and its JSON."""

from dataclasses import replace
from fractions import Fraction
from typing import Any

import pytest

from glitnir_forseti.claims.text import (
    GoalTextError,
    parse_goal,
    render_goal,
    render_short_goal,
)
from glitnir_forseti.entailment.syntax import GoalError
from glitnir_forseti.hedging.goal import CLAIMS, HedgingGoal
from glitnir_forseti.hedging.payoffs import PAYOFFS
from glitnir_forseti.stepped.goal import SteppedGoal
from glitnir_forseti.trajectory.goal import TrajectoryGoal

COVERS = (
    "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
    " premium 3555/1000 covers"
)
#: The same goal as `render_goal` writes it: every number in lowest terms.
COVERS_WRITTEN = COVERS.replace("3555/1000", "711/200")
SHORTFALL = (
    "hedging call strike 100 spot 100 band down 1/100 up 1/100 steps 20"
    " against down 1/50 up 1/50 shortfall <= 4"
)
STEPPED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " within 256 observe s breach 1 probability <= 1/4"
)
TRAJECTORY = (
    "trajectory hedged-spread from (0, 0) at 0 within 1 observe s"
    " always : s >= -1 and s <= 1"
)
LONG_TRAJECTORY = (
    "trajectory MODEL interpretation continuous-real obligation contract"
    " start time = 0 initial a = 0, b = 1 observe p as V always : V >= 0"
)


def _covers() -> HedgingGoal:
    goal = parse_goal(COVERS)
    assert isinstance(goal, HedgingGoal)
    return goal


def _shortfall() -> HedgingGoal:
    goal = parse_goal(SHORTFALL)
    assert isinstance(goal, HedgingGoal)
    return goal


@pytest.mark.parametrize("text", [COVERS_WRITTEN, SHORTFALL])
def test_round_trip(text: str) -> None:
    goal = parse_goal(text)
    assert isinstance(goal, HedgingGoal)
    assert render_goal(goal) == text
    assert parse_goal(render_goal(goal)) == goal
    assert HedgingGoal.from_json(goal.to_json()) == goal


def test_a_covers_goal_reads_every_field() -> None:
    goal = _covers()
    assert goal == HedgingGoal(
        payoff="call",
        strike=Fraction(100),
        spot=Fraction(100),
        up=Fraction(1, 50),
        down=Fraction(1, 50),
        steps=20,
        claim="covers",
        premium=Fraction(711, 200),
        against_up=None,
        against_down=None,
        bound=None,
    )
    assert goal.claim in CLAIMS
    assert HedgingGoal.SCHEMA == "gimle.forseti.hedging-goal/v1"
    assert goal.to_json()["schema"] == HedgingGoal.SCHEMA


def test_a_premium_is_read_exactly() -> None:
    goal = parse_goal(
        "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
        " premium 7/2 covers"
    )
    assert isinstance(goal, HedgingGoal)
    assert goal.premium == Fraction(7, 2)
    assert render_goal(_covers()) == COVERS_WRITTEN


def test_a_shortfall_goal_reads_every_field() -> None:
    goal = _shortfall()
    assert goal == HedgingGoal(
        payoff="call",
        strike=Fraction(100),
        spot=Fraction(100),
        up=Fraction(1, 100),
        down=Fraction(1, 100),
        steps=20,
        claim="shortfall",
        premium=None,
        against_up=Fraction(1, 50),
        against_down=Fraction(1, 50),
        bound=Fraction(4),
    )


def test_the_band_and_the_against_band_keep_their_own_ends() -> None:
    goal = parse_goal(
        "hedging put strike 95 spot 100 band down 1/20 up 1/30 steps 3"
        " against down 1/10 up 1/15 shortfall <= -1/2"
    )
    assert isinstance(goal, HedgingGoal)
    assert (goal.down, goal.up) == (Fraction(1, 20), Fraction(1, 30))
    assert (goal.against_down, goal.against_up) == (Fraction(1, 10), Fraction(1, 15))
    assert goal.bound == Fraction(-1, 2)
    assert parse_goal(render_goal(goal)) == goal


def test_decimals_read_exactly() -> None:
    goal = parse_goal(
        "hedging straddle strike 100.5 spot 100 band down 0.02 up 0.02 steps 4"
        " premium 0.25 covers"
    )
    assert isinstance(goal, HedgingGoal)
    assert (goal.strike, goal.down, goal.premium) == (
        Fraction(201, 2),
        Fraction(1, 50),
        Fraction(1, 4),
    )


@pytest.mark.parametrize("payoff", sorted(PAYOFFS))
def test_every_registered_payoff_has_a_word(payoff: str) -> None:
    goal = parse_goal(COVERS.replace("call", payoff))
    assert isinstance(goal, HedgingGoal)
    assert goal.payoff == payoff
    assert parse_goal(render_goal(goal)) == goal


def test_the_grammar_names_no_payoff_the_registry_lacks() -> None:
    with pytest.raises(GoalTextError, match="expected"):
        parse_goal(COVERS.replace("call", "bond"))
    with pytest.raises(GoalTextError, match="expected"):
        parse_goal(COVERS.replace("call", "cal"))


def test_a_payoff_without_a_word_has_no_text_form() -> None:
    goal = replace(_covers(), payoff="bond")
    with pytest.raises(GoalTextError, match="no text form"):
        render_goal(goal)


def test_hedging_goal_has_only_the_long_form() -> None:
    assert render_short_goal(_covers()) is None


@pytest.mark.parametrize(
    "text",
    [
        COVERS.replace("hedging", "hedge"),
        COVERS.replace(" strike 100", ""),
        COVERS.replace("band down 1/50 up 1/50", "band up 1/50 down 1/50"),
        COVERS.replace(" covers", ""),
        COVERS.replace("premium 3555/1000 covers", "covers"),
        SHORTFALL.replace(" <= 4", " 4"),
        SHORTFALL.replace("shortfall", "covers"),
        SHORTFALL.replace(" against down 1/50 up 1/50", ""),
        SHORTFALL + " premium 1 covers",
    ],
)
def test_malformed_text_is_refused(text: str) -> None:
    with pytest.raises(GoalTextError):
        parse_goal(text)


def test_fractional_steps_are_refused() -> None:
    with pytest.raises(GoalTextError, match="whole number of steps"):
        parse_goal(COVERS.replace("steps 20", "steps 5/2"))


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("steps 20", "steps 0", "steps"),
        ("steps 20", "steps -3", "steps"),
        ("spot 100", "spot 0", "spot"),
        ("spot 100", "spot -100", "spot"),
        ("strike 100", "strike -1", "strike"),
        ("up 1/50", "up 0", "up"),
        ("down 1/50", "down 0", "down"),
        ("down 1/50", "down -1/50", "down"),
        ("3555/1000", "-1", "premium"),
    ],
)
def test_text_that_breaks_a_shape_check_is_refused(
    old: str, new: str, match: str
) -> None:
    with pytest.raises(GoalTextError, match=match):
        parse_goal(COVERS.replace(old, new))


def test_a_zero_strike_and_a_zero_premium_are_goals() -> None:
    text = COVERS.replace("strike 100", "strike 0").replace("3555/1000", "0")
    goal = parse_goal(text)
    assert isinstance(goal, HedgingGoal)
    assert (goal.strike, goal.premium) == (Fraction(0), Fraction(0))


def test_a_band_of_a_whole_step_is_a_goal_for_the_decider_to_refuse() -> None:
    goal = parse_goal(COVERS.replace("down 1/50", "down 1"))
    assert isinstance(goal, HedgingGoal)
    assert goal.down == 1


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("against_up", Fraction(0), "against"),
        ("against_down", Fraction(-1, 50), "against"),
        ("premium", Fraction(1), "premium"),
        ("bound", None, "bound"),
    ],
)
def test_a_shortfall_goal_is_checked_field_by_field(
    field: str, value: Any, match: str
) -> None:
    with pytest.raises(GoalError, match=match):
        replace(_shortfall(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("premium", None),
        ("premium", Fraction(-1)),
        ("bound", Fraction(1)),
        ("against_up", Fraction(1, 50)),
        ("against_down", Fraction(1, 50)),
    ],
)
def test_a_covers_goal_is_checked_field_by_field(field: str, value: Any) -> None:
    with pytest.raises(GoalError):
        replace(_covers(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payoff", "Call Option"),
        ("payoff", ""),
        ("payoff", 1),
        ("claim", "within"),
        ("strike", 100),
        ("strike", 100.0),
        ("spot", Fraction(0)),
        ("up", Fraction(0)),
        ("down", Fraction(0)),
        ("steps", 0),
        ("steps", 20.0),
        ("steps", True),
    ],
)
def test_a_goal_is_checked_field_by_field(field: str, value: Any) -> None:
    with pytest.raises(GoalError):
        replace(_covers(), **{field: value})


def test_json_has_the_claims_fields_and_no_others() -> None:
    covers = _covers().to_json()
    assert set(covers) == {
        "schema",
        "payoff",
        "strike",
        "spot",
        "up",
        "down",
        "steps",
        "claim",
        "premium",
    }
    assert covers["premium"] == [711, 200]
    assert covers["steps"] == 20
    shortfall = _shortfall().to_json()
    assert set(shortfall) == {
        "schema",
        "payoff",
        "strike",
        "spot",
        "up",
        "down",
        "steps",
        "claim",
        "against_up",
        "against_down",
        "bound",
    }
    assert shortfall["against_up"] == [1, 50]
    assert shortfall["bound"] == [4, 1]


def test_json_round_trips_both_claims() -> None:
    for goal in (_covers(), _shortfall()):
        assert HedgingGoal.from_json(goal.to_json()) == goal


def test_json_refuses_a_covers_goal_with_a_bound() -> None:
    document = _covers().to_json()
    with pytest.raises(GoalError, match="exactly the v1 fields"):
        HedgingGoal.from_json({**document, "bound": [4, 1]})


def test_json_refuses_a_shortfall_goal_without_against_up() -> None:
    document = _shortfall().to_json()
    del document["against_up"]
    with pytest.raises(GoalError, match="exactly the v1 fields"):
        HedgingGoal.from_json(document)


def test_json_refuses_a_covers_goal_without_a_premium() -> None:
    document = _covers().to_json()
    del document["premium"]
    with pytest.raises(GoalError, match="exactly the v1 fields"):
        HedgingGoal.from_json(document)


def test_json_refuses_a_shortfall_goal_with_a_premium() -> None:
    document = _shortfall().to_json()
    with pytest.raises(GoalError, match="exactly the v1 fields"):
        HedgingGoal.from_json({**document, "premium": [1, 1]})


@pytest.mark.parametrize(
    "tamper",
    [
        lambda d: {**d, "extra": 1},
        lambda d: {k: v for k, v in d.items() if k != "steps"},
        lambda d: {**d, "schema": "gimle.forseti.hedging-goal/v2"},
        lambda d: {**d, "claim": "within"},
        lambda d: {**d, "claim": ["covers"]},
        lambda d: {**d, "payoff": 1},
        lambda d: {**d, "steps": 2.5},
        lambda d: {**d, "steps": True},
        lambda d: {**d, "steps": 0},
        lambda d: {**d, "strike": 100},
        lambda d: {**d, "strike": [200, 2]},
        lambda d: {**d, "spot": [0, 1]},
        lambda d: {**d, "premium": [-1, 1]},
    ],
)
def test_json_refuses_what_is_not_exactly_v1(tamper: Any) -> None:
    with pytest.raises(GoalError):
        HedgingGoal.from_json(tamper(_covers().to_json()))


def test_json_refuses_what_is_not_a_document() -> None:
    for data in (None, [], "hedging", 3):
        with pytest.raises(GoalError):
            HedgingGoal.from_json(data)


def test_the_stepped_lane_is_unchanged() -> None:
    goal = parse_goal(STEPPED)
    assert isinstance(goal, SteppedGoal)
    assert render_goal(goal) == STEPPED


def test_the_trajectory_lane_is_unchanged() -> None:
    goal = parse_goal(TRAJECTORY)
    assert isinstance(goal, TrajectoryGoal)
    assert (goal.model, goal.name) == ("hedged-spread", "s")
    assert parse_goal(render_goal(goal)) == goal


@pytest.mark.parametrize(
    "word",
    [
        "hedging",
        "strike",
        "spot",
        "band",
        "down",
        "up",
        "steps",
        "premium",
        "covers",
        "against",
        "shortfall",
        "call",
        "put",
        "straddle",
    ],
)
def test_the_hedging_words_still_name_things_in_the_other_lanes(word: str) -> None:
    entailment = parse_goal(f"entailment {word}, y : {word} <= y |- true")
    assert render_goal(entailment).startswith(f"entailment {word}, y")
    stepped = parse_goal(STEPPED.replace("hedged-spread", word))
    assert isinstance(stepped, SteppedGoal)
    assert stepped.model == word
    trajectory = parse_goal(LONG_TRAJECTORY.replace("MODEL", word))
    assert isinstance(trajectory, TrajectoryGoal)
    assert trajectory.model == word
