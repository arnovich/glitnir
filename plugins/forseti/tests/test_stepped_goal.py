"""Tests for the stepped goal: its grammar, its text form and its JSON."""

from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.claims.text import (
    GoalTextError,
    parse_goal,
    render_goal,
    render_short_goal,
)
from glitnir_forseti.entailment.syntax import GoalError
from glitnir_forseti.stepped.goal import SteppedGoal

HORIZON = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " within 256 observe s breach 1 probability <= 1/4"
)
FIXED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks subgaussian 64"
    " at every mark within 256 observe s breach 1 probability <= 1/100"
)
EXPECTED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " observe V expected <= 33/256 at 256"
)


@pytest.mark.parametrize("text", [HORIZON, FIXED, EXPECTED])
def test_round_trip(text: str) -> None:
    goal = parse_goal(text)
    assert isinstance(goal, SteppedGoal)
    assert render_goal(goal) == text
    assert SteppedGoal.from_json(goal.to_json()) == goal


def test_fields() -> None:
    g = parse_goal(FIXED)
    assert (g.law, g.claim, g.horizon, g.barrier, g.bound) == (
        "sub-gaussian",
        "fixed-date",
        256,
        Fraction(1),
        Fraction(1, 100),
    )
    e = parse_goal(EXPECTED)
    assert (e.claim, e.observation, e.horizon, e.barrier, e.bound) == (
        "expected",
        "V",
        256,
        None,
        Fraction(33, 256),
    )


def test_horizon_goal_reads_every_field() -> None:
    goal = parse_goal(HORIZON)
    assert goal == SteppedGoal(
        model="hedged-spread",
        initial=(Fraction(0), Fraction(0)),
        start=Fraction(0),
        step=Fraction(1, 256),
        law="two-moment",
        variance=Fraction(64),
        claim="horizon",
        horizon=256,
        observation="s",
        barrier=Fraction(1),
        bound=Fraction(1, 4),
    )
    assert SteppedGoal.SCHEMA == "gimle.forseti.stepped-goal/v1"
    assert goal.to_json()["schema"] == SteppedGoal.SCHEMA


def test_trajectory_lane_unchanged() -> None:
    goal = parse_goal(
        "trajectory hedged-spread from (0, 0) at 0 within 1 observe s"
        " always : s >= -1 and s <= 1"
    )
    assert not isinstance(goal, SteppedGoal)


def test_stepped_goal_has_only_the_long_form() -> None:
    assert render_short_goal(parse_goal(HORIZON)) is None


def test_fractional_horizon_is_refused() -> None:
    with pytest.raises(GoalTextError, match="whole number of steps"):
        parse_goal(HORIZON.replace("within 256", "within 5/2"))


def test_json_has_a_barrier_only_for_a_barrier_claim() -> None:
    assert "barrier" in parse_goal(HORIZON).to_json()
    document = parse_goal(EXPECTED).to_json()
    assert "barrier" not in document
    assert SteppedGoal.from_json(document) == parse_goal(EXPECTED)
    with pytest.raises(GoalError, match="no barrier"):
        SteppedGoal.from_json({**document, "barrier": [1, 1]})
    with pytest.raises(GoalError, match="exactly the v1 fields"):
        SteppedGoal.from_json({**parse_goal(HORIZON).to_json(), "extra": 1})


def test_negative_bound_is_refused() -> None:
    text = HORIZON.replace("<= 1/4", "<= -1/4")
    with pytest.raises(GoalTextError, match="bound"):
        parse_goal(text)
    with pytest.raises(GoalError, match="bound"):
        replace(parse_goal(HORIZON), bound=Fraction(-1, 4))


def test_negative_variance_is_refused() -> None:
    text = HORIZON.replace("variance 64", "variance -64")
    with pytest.raises(GoalTextError, match="variance"):
        parse_goal(text)
    with pytest.raises(GoalError, match="variance"):
        replace(parse_goal(HORIZON), variance=Fraction(-64))
