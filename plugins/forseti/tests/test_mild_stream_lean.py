"""Independent replay of physical mild circuit contracts and hostile candidates."""

import json
from dataclasses import replace
from fractions import Fraction as F

import pytest

from glitnir_forseti.claims.text import parse_goal
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.notebook.readable import readable
from glitnir_forseti.streams import mild_render, replay, service
from glitnir_forseti.streams.goal import Modes
from glitnir_forseti.streams.mild import MildEvidence

LIMITS = EntailmentLimits(wall_time_ms=600_000)
SESSION = SessionLimits(timeout_ms=600_000, max_output_bytes=1_048_576)


def _goal(band: bool = False):
    """Use a nontrivial finite start and the physical four-term interval."""
    claim = (
        "band u time 1/5760 : u >= -3519/1000 and u <= 3519/1000"
        if band
        else "truncation u window 4 time 1/5760 epsilon 21/1024"
    )
    return parse_goal(
        "stream vorticity viscosity 1/10 interpretation"
        f" mild-{'band' if band else 'truncation'} basis interaction"
        " start modes [1, 0, 1/2],"
        f" [-1, 0, 1/2], [1, 1, 1/2], [-1, -1, 1/2], [2, 1, 1/2], [-2, -1, 1/2] {claim}"
    )


@pytest.mark.parametrize(
    "case",
    [
        "scale",
        "contraction",
        "band",
        "zero",
        "zero_time",
        "euler",
        "one_term",
        "fallback",
    ],
)
def test_mild_contract_and_both_readable_bridges(case, lean_test_project) -> None:
    """Replay every full postcondition and both independent notebook bridges."""
    goal = _goal(case == "band")
    if case == "contraction":
        goal = replace(goal, viscosity=F(1), claim=replace(goal.claim, time=F(1, 576)))
    elif case == "zero":
        goal = replace(
            goal,
            viscosity=F(0),
            profile=Modes(((5, 4, F(0)),)),
            claim=replace(goal.claim, time=F(1000000), epsilon=F(0)),
        )
    elif case == "zero_time":
        goal = replace(goal, claim=replace(goal.claim, time=F(0), epsilon=F(0)))
    elif case == "euler":
        goal = replace(goal, viscosity=F(0))
    elif case == "fallback":
        goal = replace(
            goal,
            viscosity=F(1),
            claim=replace(goal.claim, time=F(1, 1000), window=12, epsilon=F(10)),
        )
    elif case == "one_term":
        goal = replace(goal, claim=replace(goal.claim, window=1, epsilon=F(10)))
    result = service.decide_stream(goal, LIMITS, project=lean_test_project)
    assert result.status is service.Status.PROVEN, result.stages
    assert result.artifact is not None
    evidence = json.loads(result.artifact)["evidence"]
    assert evidence["kind"] == (
        "mild-contraction"
        if case in ("contraction", "band", "one_term")
        else "mild-scale"
    )
    regenerated = replay.regenerate_application(result.artifact, goal, LIMITS)
    assert isinstance(regenerated, tuple), regenerated
    app = regenerated[2]
    shown = readable(goal)
    assert shown is not None
    script = (
        *shown.setup,
        "have lane : forsetiRoot := by",
        *("  " + line for line in app.script),
        *shown.from_lane,
    )
    checked = check_independently(
        app.prelude, shown.statement, script, SESSION, lean_test_project
    )
    assert checked.status is IndependentStatus.CONFIRMED, checked
    back = (
        *shown.setup,
        "have claim " + shown.statement + " := by",
        *("  " + line for line in script),
        *shown.to_lane,
    )
    checked = check_independently(
        app.prelude, app.statement, back, SESSION, lean_test_project
    )
    assert checked.status is IndependentStatus.CONFIRMED, checked


@pytest.mark.parametrize(
    "case", ["theta_one", "q_one", "heat_time", "mean", "asymmetric", "negative_error"]
)
def test_kernel_rejects_hostile_mild_premises(case, lean_test_project) -> None:
    """Even bypassing Python admission cannot validate a false side condition."""
    goal = _goal()
    evidence = MildEvidence("mild-contraction", 3, F(3), 4, F(1, 24))
    if case == "theta_one":
        evidence = replace(evidence, radius=F(1, 12))
    elif case == "q_one":
        goal = replace(goal, claim=replace(goal.claim, time=F(1, 648)))
        evidence = MildEvidence("mild-scale", 3, F(3), 4)
    elif case == "heat_time":
        evidence = replace(evidence, radius=F(1, 100))
    elif case == "mean":
        goal = replace(goal, profile=Modes(((0, 0, F(1)),)))
        evidence = replace(evidence, size=1, weight=F(1))
    elif case == "asymmetric":
        goal = replace(goal, profile=Modes(((1, 0, F(1)),)))
        evidence = replace(evidence, size=1, weight=F(1))
    else:
        goal = replace(goal, claim=replace(goal.claim, epsilon=F(-1)))
    app = mild_render.prove(goal, evidence)
    checked = check_independently(
        app.prelude, app.statement, app.script, SESSION, lean_test_project
    )
    assert checked.status is IndependentStatus.REJECTED, checked


@pytest.mark.parametrize("field", ["time", "viscosity", "start", "source", "window"])
def test_mild_artifact_binds_every_physical_goal_parameter(field) -> None:
    """A retained proof cannot be reused for a modified root or initial slice."""
    goal = _goal()
    artifact = replay.build_artifact(
        goal, MildEvidence("mild-scale", 3, F(3), 4), LIMITS, {"stage": "test"}
    )
    if field == "time":
        goal = replace(goal, claim=replace(goal.claim, time=F(1, 5700)))
    elif field == "viscosity":
        goal = replace(goal, viscosity=F(0))
    elif field == "start":
        goal = replace(goal, profile=Modes(((0, 0, F(0)),)))
    elif field == "window":
        goal = replace(goal, claim=replace(goal.claim, window=3))
    else:
        document = json.loads(artifact)
        document["source"] = document["source"].replace(
            "Gimle.Forseti.MildVorticity.Certified", "True /- stripped guarantee -/ --"
        )
        artifact = json.dumps(document).encode()
    assert isinstance(
        replay.regenerate_application(artifact, goal, LIMITS), InvalidEvidence
    )
