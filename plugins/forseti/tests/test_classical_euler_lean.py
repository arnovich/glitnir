"""Replay complete Euler circuit contracts and refuse hostile proof applications."""

import json
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.notebook.readable import readable
from glitnir_forseti.streams import render, replay, service
from glitnir_forseti.streams.examples import trig_band, trig_truncation, vorticity_goal
from glitnir_forseti.streams.goal import Modes, StreamGoal, encode_goal
from glitnir_forseti.streams.models import VORTICITY
from glitnir_forseti.streams.trig import RadiusEvidence

LIMITS = EntailmentLimits(wall_time_ms=600_000)
SESSION = SessionLimits(timeout_ms=600_000, max_output_bytes=1_048_576)


def _goal(band: bool, zero: bool) -> StreamGoal:
    """Use different initial data from the original three-mode example."""
    start = (
        Modes(((0, 0, Fraction(0)),))
        if zero
        else Modes(
            (
                (1, 0, Fraction(1, 2)),
                (-1, 0, Fraction(1, 2)),
                (0, 2, Fraction(1, 4)),
                (0, -2, Fraction(1, 4)),
            )
        )
    )
    goal = vorticity_goal(
        (
            trig_band(Fraction(-2), Fraction(2))
            if band
            else trig_truncation(Fraction(0) if zero else Fraction(1, 100))
        ),
        start=start,
    )
    return replace(
        goal,
        interpretation="euler-band" if band else "euler-truncation",
        claim=replace(
            goal.claim, radius=Fraction(1000000) if zero else Fraction(1, 2160)
        ),
    )


@pytest.mark.parametrize("band", [False, True])
@pytest.mark.parametrize("zero", [False, True])
def test_classical_contract_replays_and_readable_statement_is_equivalent(
    band, zero, lean_test_project
) -> None:
    """Check the whole root and both directions of the displayed notebook claim."""
    goal = _goal(band, zero)
    result = service.decide_stream(goal, LIMITS, project=lean_test_project)
    assert result.status is service.Status.PROVEN, result.stages
    assert result.artifact is not None
    regenerated = replay.regenerate_application(result.artifact, goal, LIMITS)
    assert isinstance(regenerated, tuple), regenerated
    app = regenerated[2]
    assert "Gimle.Forseti.Euler.certified" in result.dependencies
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
    "bad_start", [Modes(((0, 0, Fraction(1)),)), Modes(((1, 0, Fraction(1)),))]
)
def test_lean_itself_rejects_nonphysical_data(bad_start, lean_test_project) -> None:
    """Bypass the Python support check: kernel-decided premises must still fail."""
    goal = replace(_goal(False, False), profile=bad_start)
    app = render.prove_trig_truncation(goal, RadiusEvidence(1, Fraction(1)))
    checked = check_independently(
        app.prelude, app.statement, app.script, SESSION, lean_test_project
    )
    assert checked.status is IndependentStatus.REJECTED, checked


def test_a_convergence_certificate_cannot_be_relabelled_classical() -> None:
    """Even rebinding environment metadata cannot upgrade the retained theorem."""
    goal = _goal(False, False)
    old = replace(goal, interpretation="trig-truncation")
    artifact = replay.build_artifact(
        old, RadiusEvidence(2, Fraction(3, 2)), LIMITS, {"stage": "test"}
    )
    assert isinstance(
        replay.regenerate_application(artifact, goal, LIMITS), InvalidEvidence
    )
    document = json.loads(artifact)
    document["goal"] = encode_goal(goal)
    document["environment"] = replay.environment_identity(
        replay.imports_for(goal, VORTICITY)
    )
    refused = replay.regenerate_application(json.dumps(document).encode(), goal, LIMITS)
    assert isinstance(refused, InvalidEvidence) and "regeneration" in refused.reason


@pytest.mark.parametrize("change", ["radius", "start", "viscosity", "source"])
def test_classical_artifact_binds_data_radius_viscosity_and_full_postcondition(
    change,
) -> None:
    """Hostile changes are rejected before invoking the independent checker."""
    goal = _goal(False, False)
    artifact = replay.build_artifact(
        goal, RadiusEvidence(2, Fraction(3, 2)), LIMITS, {"stage": "test"}
    )
    if change == "source":
        document = json.loads(artifact)
        document["source"] = document["source"].replace(
            "Gimle.Forseti.Euler.Certified", "True /- removed PDE -/ --"
        )
        artifact = json.dumps(document).encode()
    elif change == "radius":
        goal = replace(goal, claim=replace(goal.claim, radius=Fraction(1, 2161)))
    elif change == "start":
        goal = replace(goal, profile=Modes(((0, 0, Fraction(0)),)))
    else:
        goal = replace(goal, viscosity=Fraction(1, 10))
    assert not isinstance(replay.regenerate_application(artifact, goal, LIMITS), tuple)
