r"""Live Lean tests for stream and field root goals (112, lanes 2–3).

They run against the pinned standalone release, and fail instead of skipping
under the gate::

    FORSETI_REQUIRE_LEAN_PROJECT=1 \
    FORSETI_LEAN_PROJECT=/path/to/forseti-lean-v1.26.0 \
        uv run python -m pytest tests/test_streams_lean.py
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment.evidence import Certificate, Witness, decode_evidence
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence
from glitnir_forseti.entailment.syntax import Atom, Relation, Var
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.streams import render
from glitnir_forseti.streams.examples import (
    EULER_START,
    EXAMPLES,
    EXP,
    FRONT,
    OBSERVED,
    band,
    band_goal,
    burgers_goal,
    field_claim,
    front_band,
    front_truncation,
    heat_goal,
    trig_band,
    trig_truncation,
    truncation,
    vorticity_goal,
)
from glitnir_forseti.streams.goal import (
    CoefficientClaim,
    ExpSum,
    FieldClaim,
    Modes,
    encode_goal,
)
from glitnir_forseti.streams.models import HEAT
from glitnir_forseti.streams.replay import build_artifact, replay_stream
from glitnir_forseti.streams.service import Status, decide_stream
from glitnir_forseti.streams.tail import (
    FrontEvidence,
    TailEvidence,
    WindowEvidence,
    choose_band,
)
from glitnir_forseti.streams.trig import BandEvidence, RadiusEvidence
from glitnir_forseti.streams.trig import choose_band as choose_trig_band

LIMITS = EntailmentLimits()
SESSION = SessionLimits(timeout_ms=600_000, max_output_bytes=1_048_576)
BY_NAME = {example.name: example for example in EXAMPLES}


@pytest.fixture(scope="module")
def decided(lean_test_project):
    """Decide every example once, from its goal alone."""
    return {
        example.name: decide_stream(example.goal, project=lean_test_project)
        for example in EXAMPLES
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_the_examples_end_as_required(decided, example) -> None:
    result = decided[example.name]
    got = result.status.value + (f":{result.reason.value}" if result.reason else "")
    assert got == example.expected, result.stages
    if result.status is not Status.UNKNOWN:
        assert set(result.axioms) <= {"propext", "Classical.choice", "Quot.sound"}


def test_the_refutations_keep_their_witnesses(decided) -> None:
    field = json.loads(decided["field-below-two"].artifact)["evidence"]
    assert field["values"] == [["t", [1, 1]], ["x", [1, 1]], ["u", [3, 1]]]
    assert "the heat solution" in field["input"]
    zero = json.loads(decided["zero-constant"].artifact)["evidence"]
    assert zero["values"] == [["b", [1, 1]], ["u", [1, 1]]]
    assert "FormalHeat.heat" in zero["input"]
    band = decided["field-band"]
    assert "Gimle.Forseti.FieldBound.sq_le_sq_of_box" in band.dependencies


def test_root_artifacts_replay_offline_in_a_fresh_process(
    decided, lean_test_project, tmp_path
) -> None:
    """Replay runs with Z3 and every producer module made unimportable."""
    blocked = [
        "z3",
        "glitnir_forseti.entailment.search",
        "glitnir_forseti.entailment.solver",
        "glitnir_forseti.entailment.worker",
        "glitnir_forseti.streams.service",
    ]
    for name in (
        "full-stream",
        "zero-constant",
        "field-band",
        "exp-tail",
        "front-tail",
    ):
        example = BY_NAME[name]
        artifact = tmp_path / f"{name}.json"
        goal = tmp_path / f"{name}.goal.json"
        artifact.write_bytes(decided[name].artifact)
        goal.write_text(json.dumps(encode_goal(example.goal)))
        code = (
            "import sys\n"
            f"for name in {blocked!r}:\n"
            "    sys.modules[name] = None\n"
            "from glitnir_forseti.streams.__main__ import main\n"
            f"sys.exit(main(['replay', {str(artifact)!r}, '--goal', {str(goal)!r}, "
            f"'--lean-project', {str(lean_test_project)!r}]))\n"
        )
        run = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ},
            timeout=600,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        report = json.loads(run.stdout)
        assert report["outcome"] == "validated"
        assert report["status"] == example.expected


def _lean(unit, project) -> IndependentStatus:
    return check_independently(
        unit.prelude, unit.statement, unit.script, SESSION, project
    ).status


def _certificate(artifact: bytes, context) -> Certificate:
    evidence = json.loads(artifact)["evidence"]["certificate"]
    certificate = decode_evidence(evidence, context, LIMITS)
    assert isinstance(certificate, Certificate)
    return certificate


def test_old_field_evidence_is_refused_by_lean(decided, lean_test_project) -> None:
    """Lean refuses old field evidence rendered against another claim.

    The band's certificate against a tighter bound, and a witness whose ``u``
    is not the solution's field, both fail in Lean, bypassing Python's checks.
    """
    from glitnir_forseti.streams.goal import FIELD_CONTEXT

    certificate = _certificate(decided["field-band"].artifact, FIELD_CONTEXT)
    tighter = heat_goal(field_claim(band(2)))
    claim = tighter.claim
    assert isinstance(claim, FieldClaim)
    unit = render.prove_field(tighter, HEAT, claim, certificate)
    assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED
    wrong = Witness((("t", Fraction(1)), ("x", Fraction(1)), ("u", Fraction(5, 2))))
    unit = render.refute_field(tighter, HEAT, claim, wrong)
    assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED


def test_a_leaf_counterexample_needs_the_admitted_input(lean_test_project) -> None:
    """Lean refuses a leaf counterexample no admitted input has.

    ``u[t¹] ≥ 0`` fails at halo values ``(0, −1)``; rendered as a root
    refutation, Lean refuses it, and the root is in fact proved.
    """
    weak = heat_goal(CoefficientClaim(OBSERVED, Atom(Relation.GE, Var(0))))
    claim = weak.claim
    assert isinstance(claim, CoefficientClaim)
    leaf = Witness((("b", Fraction(0)), ("u", Fraction(-1))))
    unit = render.refute_coefficients(HEAT, claim, leaf)
    assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED
    result = decide_stream(weak, project=lean_test_project)
    assert result.status is Status.PROVEN


def test_the_full_stream_identity_is_the_contract(decided) -> None:
    result = decided["full-stream"]
    assert HEAT.full_stream.lemma in result.dependencies
    assert "Gimle.Asgard.Examples.PolynomialHeat.original_heat" in result.dependencies
    assert json.loads(result.artifact)["evidence"] == {"kind": "contract"}


def test_the_full_stream_polynomials_are_bound_by_lean(lean_test_project) -> None:
    """A full-stream root naming u = x² + 3t, rendered directly, fails in Lean."""
    from glitnir_forseti.entailment.syntax import Add, Const, Mul

    wrong = (
        ("u_xx", Const(Fraction(2))),
        ("u", Add(Mul(Var(1), Var(1)), Mul(Const(Fraction(3)), Var(0)))),
    )
    unit = render.full_stream(HEAT, wrong)
    assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED


def _tail_goal(epsilon: Fraction, profile=EXP):
    return heat_goal(
        truncation(epsilon), interpretation="analytic-tail", profile=profile
    )


@pytest.mark.parametrize(
    ("goal", "rho"),
    [
        # e^x does not fit ρ = 2: |a|·ρ = 2 > 1.
        (_tail_goal(Fraction(1, 1000)), Fraction(2)),
        # ρ = 1/2 puts the box |x| ≤ 1/2 on the radius.
        (_tail_goal(Fraction(1, 1000)), Fraction(1, 2)),
        # ε below the certified bound 1/12288.
        (_tail_goal(Fraction(1, 20000)), Fraction(1)),
        # e^(2x) claimed at ρ = 1: the profile's bound does not hold there.
        (
            _tail_goal(Fraction(1, 1000), ExpSum(((Fraction(1), Fraction(2)),))),
            Fraction(1),
        ),
    ],
    ids=[
        "rate-too-large",
        "box-on-radius",
        "epsilon-below-bound",
        "wrong-profile-bound",
    ],
)
def test_lean_refuses_a_truncation_proof_that_does_not_hold(
    goal, rho, lean_test_project
) -> None:
    """Each artifact is regenerated consistently; only Lean can refuse it."""
    artifact = build_artifact(goal, TailEvidence(rho), LIMITS, {"stage": "test"})
    outcome = replay_stream(artifact, goal, LIMITS, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome


@pytest.mark.parametrize(
    ("goal", "evidence"),
    [
        # ε below the certified bound 1/16384 at the example's radii.
        (
            burgers_goal(front_truncation(Fraction(1, 100000))),
            FrontEvidence(Fraction(1), (Fraction(2, 3), Fraction(1, 2))),
        ),
        # r_t = 1 is below the heat radius 2 but the inverse is not majorized
        # there: (1/2)·((1 − 1/2)⁻¹·(1 − 1/2)⁻¹ − 1) = 3/2 > 1.
        (
            burgers_goal(front_truncation(Fraction(1, 10000))),
            FrontEvidence(Fraction(1), (Fraction(1), Fraction(1, 2))),
        ),
        # The box |x| ≤ 1/8 is not inside r_x/2 = 1/8.
        (
            burgers_goal(front_truncation(Fraction(1, 10000))),
            FrontEvidence(Fraction(1), (Fraction(2, 3), Fraction(1, 4))),
        ),
        # r_t = 2 is the heat radius ρ²/ν itself: not strictly inside.
        (
            burgers_goal(front_truncation(Fraction(1, 10000))),
            FrontEvidence(Fraction(1), (Fraction(2), Fraction(1, 2))),
        ),
        # e^(−2x) does not fit ρ = 1.
        (
            burgers_goal(
                front_truncation(Fraction(1, 10000)),
                profile=ExpSum(
                    ((Fraction(1), Fraction(0)), (Fraction(1), Fraction(-2)))
                ),
            ),
            FrontEvidence(Fraction(1), (Fraction(2, 3), Fraction(1, 2))),
        ),
    ],
    ids=[
        "epsilon-below-bound",
        "inverse-not-majorized",
        "box-on-radius",
        "radii-on-heat-radius",
        "rate-too-large",
    ],
)
def test_lean_refuses_a_front_proof_that_does_not_hold(
    goal, evidence, lean_test_project
) -> None:
    """Each front artifact is regenerated consistently; only Lean can refuse it."""
    artifact = build_artifact(goal, evidence, LIMITS, {"stage": "test"})
    outcome = replay_stream(artifact, goal, LIMITS, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome


def test_the_front_is_proved_with_the_example_radii(decided) -> None:
    result = decided["front-tail"]
    assert result.status is Status.PROVEN
    assert "ColeHopf.front_truncation" in " ".join(result.dependencies)
    evidence = json.loads(result.artifact)["evidence"]
    assert evidence == {
        "kind": "front-radii",
        "rho": [1, 1],
        "r": {"t": [2, 3], "x": [1, 2]},
    }
    assert "1/16384" in result.stages[-1].detail


def _band_evidence(goal) -> WindowEvidence:
    found = choose_band(Fraction(1, 2), FRONT, goal.claim, LIMITS)
    assert isinstance(found, WindowEvidence), found
    return found


_BAND = band_goal(front_band(Fraction(2, 5), Fraction(3, 5)))
_ABOVE = band_goal(front_band(Fraction(2, 5), Fraction(27, 50)))
_BELOW = band_goal(front_band(Fraction(11, 20), None))


@pytest.mark.parametrize(
    ("goal", "evidence"),
    [
        # One wrong table entry: the window identity fails in the kernel.
        (
            _BAND,
            lambda: (
                lambda e: replace(
                    e,
                    table=tuple(
                        tuple(
                            v + (1 if (i, j) == (3, 3) else 0)
                            for j, v in enumerate(row)
                        )
                        for i, row in enumerate(e.table)
                    ),
                )
            )(_band_evidence(_BAND)),
        ),
        # The checked window and error are right, but hi = 27/50 is below the
        # window's bound: the `high` side condition fails.
        (_ABOVE, lambda: replace(_band_evidence(_ABOVE), point=None, side="")),
        # A refutation above at the centre, where the window is 1/2 < 27/50.
        (
            _ABOVE,
            lambda: replace(_band_evidence(_ABOVE), point=(Fraction(0), Fraction(0))),
        ),
        # A refutation below 2/5 at a corner where the window is 0.49.
        (
            band_goal(front_band(Fraction(2, 5), None)),
            lambda: replace(
                _band_evidence(_BELOW), point=(Fraction(1, 6), Fraction(1, 8))
            ),
        ),
        # A smaller ε than the radii certify at this window.
        (_BAND, lambda: replace(_band_evidence(_BAND), epsilon=Fraction(1, 100000))),
    ],
    ids=[
        "wrong-table",
        "band-too-narrow",
        "not-above-at-centre",
        "not-below",
        "epsilon-below-bound",
    ],
)
def test_lean_refuses_a_band_proof_that_does_not_hold(
    goal, evidence, lean_test_project
) -> None:
    """Each band artifact is regenerated consistently; only Lean can refuse it."""
    artifact = build_artifact(goal, evidence(), LIMITS, {"stage": "test"})
    outcome = replay_stream(artifact, goal, LIMITS, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome


def test_the_band_is_proved_from_the_checked_window(decided) -> None:
    result = decided["front-band"]
    assert result.status is Status.PROVEN
    joined = " ".join(result.dependencies)
    assert "ColeHopf.front_band_of_window" in joined and "coeff_inv_window" in joined
    evidence = json.loads(result.artifact)["evidence"]
    assert evidence["kind"] == "window" and evidence["window"] == [16, 16]
    assert evidence["epsilon"] == [1, 16384] and "point" not in evidence
    assert "≈ 0.4477" in result.stages[-1].detail
    assert decided["front-band-upper"].status is Status.PROVEN
    above = json.loads(decided["front-band-above"].artifact)["evidence"]
    assert (above["point"], above["side"]) == ({"t": [1, 6], "x": [-1, 8]}, "above")
    assert "front_above_of_window" in " ".join(decided["front-band-above"].dependencies)
    below = json.loads(decided["front-band-below"].artifact)["evidence"]
    assert (below["point"], below["side"]) == ({"t": [1, 6], "x": [1, 8]}, "below")
    assert "front_below_of_window" in " ".join(decided["front-band-below"].dependencies)
    narrow = decided["front-band-narrow"]
    assert narrow.status is Status.UNKNOWN and "no proof and no refutation" in (
        narrow.stages[-1].detail
    )


# --- the Euler radius lane (217) ------------------------------------------------

_EULER_TAIL = vorticity_goal(trig_truncation(Fraction(1, 100)))
_EULER_BAND = vorticity_goal(trig_band(Fraction(-31, 10), Fraction(31, 10)))


def test_zero_weight_euler_truncation_replays(lean_test_project) -> None:
    """The zero field has zero truncation error, checked by the same root."""
    goal = vorticity_goal(
        trig_truncation(Fraction(1)), start=Modes(((1, 0, Fraction(0)),))
    )
    limits = replace(LIMITS, wall_time_ms=SESSION.timeout_ms)
    result = decide_stream(goal, limits, project=lean_test_project)
    assert result.status is Status.PROVEN, result.stages
    assert result.artifact is not None
    assert (
        replay_stream(result.artifact, goal, limits, lean_test_project).status
        == "PROVEN"
    )


def _euler_band_evidence(goal) -> BandEvidence:
    found = choose_trig_band(EULER_START, goal.claim, LIMITS)
    assert isinstance(found, BandEvidence), found
    return found


@pytest.mark.parametrize(
    ("goal", "evidence"),
    [
        # ε below the certified bound: `tailBound ≤ ε` fails in the kernel.
        (
            vorticity_goal(trig_truncation(Fraction(1, 1000))),
            lambda: RadiusEvidence(3, Fraction(3)),
        ),
        # A window norm smaller than the kernel's absSum: the equation fails.
        (
            _EULER_BAND,
            lambda: replace(
                _euler_band_evidence(_EULER_BAND),
                norms=(Fraction(3), Fraction(8, 5), Fraction(1, 2)),
            ),
        ),
        # The right norms, but the band asked is inside their bound: linarith fails.
        (
            vorticity_goal(trig_band(None, Fraction(3))),
            lambda: _euler_band_evidence(_EULER_BAND),
        ),
    ],
    ids=["epsilon-below-bound", "wrong-norm", "band-too-narrow"],
)
def test_lean_refuses_an_euler_proof_that_does_not_hold(
    goal, evidence, lean_test_project
) -> None:
    """Each Euler artifact is regenerated consistently; only Lean can refuse it."""
    artifact = build_artifact(goal, evidence(), LIMITS, {"stage": "test"})
    outcome = replay_stream(artifact, goal, LIMITS, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome


def test_the_euler_claims_are_proved_from_the_radius_theorems(decided) -> None:
    tail = decided["euler-tail"]
    assert tail.status is Status.PROVEN
    joined = " ".join(tail.dependencies)
    assert "NS.euler_l1_geometric" in joined and "truncationBound_rat" in joined
    assert "Gimle.Forseti.Fourier.solution_contract" in tail.dependencies
    assert "Gimle.Forseti.Fourier.Contract.consequence" in tail.dependencies
    assert json.loads(tail.artifact)["evidence"] == {
        "kind": "euler-radius",
        "size": 3,
        "weight": [3, 1],
    }
    band = decided["euler-band"]
    assert band.status is Status.PROVEN
    evidence = json.loads(band.artifact)["evidence"]
    assert evidence["window"] == 3 and evidence["norms"] == [[3, 1], [8, 5], [223, 260]]
    assert "NS.stream_eq_table" in " ".join(band.dependencies)
    assert json.loads(decided["euler-band-upper"].artifact)["evidence"]["window"] == 1
    assert "≈ 3.0012" in decided["euler-band-narrow"].stages[-1].detail
