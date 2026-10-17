r"""Live Lean tests for trajectory root goals (task 112, ODE lane).

They run against the pinned standalone release, and fail instead of skipping
under the gate::

    FORSETI_REQUIRE_LEAN_PROJECT=1 \
    FORSETI_LEAN_PROJECT=/path/to/forseti-lean-v1.26.0 \
        uv run python -m pytest tests/test_trajectory_lean.py
"""

import json
import os
import subprocess
import sys
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment.evidence import (
    Certificate,
    Row,
    SquareSum,
    decode_evidence,
)
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import Const
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.trajectory import render, replay
from glitnir_forseti.trajectory.examples import BAND, EXAMPLES, energy_goal
from glitnir_forseti.trajectory.goal import TrajectoryGoal, encode_goal
from glitnir_forseti.trajectory.models import FORCED_DECAY, HEDGED_SPREAD, THREE_STATE
from glitnir_forseti.trajectory.service import Status, decide_trajectory

LIMITS = EntailmentLimits()
SESSION = SessionLimits(timeout_ms=600_000, max_output_bytes=1_048_576)
ENERGY = THREE_STATE.observations["obs-v"]
DECAY = FORCED_DECAY.observations["obs-x"]
EXPOSURE = HEDGED_SPREAD.observations["obs-s"]
HEDGED_ENERGY = HEDGED_SPREAD.observations["obs-v"]
ONE = Const(Fraction(1))

#: The two driven models: the proven band example and its band, the refuted
#: example and its band, the driver ports as the root lists them, and a goal
#: with a bound above the contract's.
DRIVEN = [
    pytest.param(
        FORCED_DECAY,
        "forced-band",
        DECAY,
        "forced-negative",
        DECAY,
        '"driver-u", "driver-du"',
        (("driver-u", Fraction(3)),),
        id="forced-decay",
    ),
    pytest.param(
        HEDGED_SPREAD,
        "hedged-band",
        EXPOSURE,
        "hedged-energy-at-least-quarter",
        HEDGED_ENERGY,
        '"driver-d"',
        (("driver-d", Fraction(3)),),
        id="hedged-spread",
    ),
]


@pytest.fixture(scope="module")
def decided(lean_test_project):
    """Decide every example once, from its goal alone."""
    return {
        example.name: decide_trajectory(example.goal, project=lean_test_project)
        for example in EXAMPLES
    }


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_the_examples_end_as_required(decided, example) -> None:
    result = decided[example.name]
    got = result.status.value + (f":{result.reason.value}" if result.reason else "")
    assert got == example.expected, result.stages
    if result.status is not Status.UNKNOWN:
        assert set(result.axioms) <= {"propext", "Classical.choice", "Quot.sound"}
        assert result.artifact is not None


def test_the_band_cites_the_named_contract(decided) -> None:
    result = decided["energy-band"]
    assert ENERGY.contract in result.dependencies
    refuted = decided["energy-below-five"]
    assert ENERGY.initial_value_lemma in refuted.dependencies
    witness = json.loads(refuted.artifact)["evidence"]
    assert witness == {
        "kind": "initial-witness",
        "time": [2, 1],
        "initial": [
            ["initial-x", [1, 1]],
            ["initial-y", [2, 1]],
            ["initial-z", [-1, 1]],
        ],
        "value": [6, 1],
    }


def test_a_leaf_counterexample_does_not_refute_the_root(decided) -> None:
    """V ≥ 1 fails at V = 0, inside the band, but no trajectory is shown there."""
    stages = {s.stage: s.outcome for s in decided["energy-at-least-one"].stages}
    assert stages == {
        "binding": "supported",
        "certificate": "none",
        "initial-witness": "none",
    }


def test_root_artifacts_replay_offline_in_a_fresh_process(
    decided, lean_test_project, tmp_path
) -> None:
    """Replay runs with Z3 and every producer module made unimportable."""
    blocked = [
        "z3",
        "glitnir_forseti.entailment.search",
        "glitnir_forseti.entailment.solver",
        "glitnir_forseti.entailment.worker",
        "glitnir_forseti.trajectory.service",
    ]
    for name in ("energy-band", "energy-below-five"):
        example = next(e for e in EXAMPLES if e.name == name)
        artifact = tmp_path / f"{name}.json"
        goal = tmp_path / f"{name}.goal.json"
        artifact.write_bytes(decided[name].artifact)
        goal.write_text(json.dumps(encode_goal(example.goal)))
        code = (
            "import sys\n"
            f"for name in {blocked!r}:\n"
            "    sys.modules[name] = None\n"
            "from glitnir_forseti.trajectory.__main__ import main\n"
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
        assert report["initial"] == {
            "initial-x": "1",
            "initial-y": "2",
            "initial-z": "-1",
        }


def _lean(unit, project) -> IndependentStatus:
    return check_independently(
        unit.prelude, unit.statement, unit.script, SESSION, project
    ).status


@pytest.mark.parametrize(
    "other",
    [
        energy_goal(BAND, start=Fraction(3)),
        energy_goal(
            BAND,
            initial=(
                ("initial-x", Fraction(1)),
                ("initial-y", Fraction(2)),
                ("initial-z", Fraction(1)),
            ),
        ),
    ],
    ids=["later-start", "other-initial"],
)
def test_goal_binding_lives_in_lean_too(decided, lean_test_project, other) -> None:
    """Lean refuses old evidence rendered against a changed time or state.

    The band's certificate and the start-time witness are rendered against the
    changed goal directly, bypassing the Python goal comparison; the cited
    theorems are about the declared time and state, so both fail in Lean.
    """
    document = json.loads(decided["energy-band"].artifact)["evidence"]
    certificate = decode_evidence(document["certificate"], other.context, LIMITS)
    assert (
        _lean(render.prove(other, THREE_STATE, ENERGY, certificate), lean_test_project)
        is IndependentStatus.REJECTED
    )
    five = energy_goal(
        BAND.right,  # type: ignore[attr-defined]
        start=other.start,
        initial=other.initial,
    )
    assert (
        _lean(render.refute(five, THREE_STATE, ENERGY, Fraction(6)), lean_test_project)
        is IndependentStatus.REJECTED
    )


def test_a_wrong_initial_value_is_refused_by_lean(lean_test_project) -> None:
    """A witness claiming V = 7 at the start does not match the lemma."""
    goal = energy_goal(BAND.right)  # type: ignore[attr-defined]
    unit = render.refute(goal, THREE_STATE, ENERGY, Fraction(7))
    assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED


def test_an_old_certificate_does_not_replay_for_a_changed_root(
    decided, lean_test_project
) -> None:
    artifact = decided["energy-band"].artifact
    for other in (
        energy_goal(BAND, start=Fraction(3)),
        energy_goal(BAND.right),  # type: ignore[attr-defined]
    ):
        outcome = replay.replay_root(artifact, other, LIMITS, lean_test_project)
        assert not isinstance(outcome, replay.ValidatedRoot)


def test_a_changed_initial_state_ends_unknown_not_refuted(lean_test_project) -> None:
    goal = energy_goal(
        BAND,
        initial=(
            ("initial-x", Fraction(1)),
            ("initial-y", Fraction(2)),
            ("initial-z", Fraction(1)),
        ),
    )
    result = decide_trajectory(goal, project=lean_test_project)
    assert result.status is Status.UNKNOWN
    assert result.reason is not None and result.reason.value == "unsupported"


def test_a_misstated_model_entry_proves_and_refutes_nothing(lean_test_project) -> None:
    """Lean refuses both templates for a mis-stated entry or axis.

    The identities are conjuncts Lean must prove, so swapped initial IDs or
    another axis cannot yield a proof or a refutation.
    """
    swapped = replace(THREE_STATE, initial_ids=("initial-y", "initial-x", "initial-z"))
    goal = energy_goal(BAND)
    five = energy_goal(BAND.right)  # type: ignore[attr-defined]
    certificate = Certificate(
        (
            Row(SquareSum(()), (SquareSum(((Fraction(1), ONE),)), SquareSum(()))),
            Row(SquareSum(()), (SquareSum(()), SquareSum(((Fraction(1), ONE),)))),
        )
    )
    for unit in (
        render.prove(goal, swapped, ENERGY, certificate),
        render.refute(five, swapped, ENERGY, Fraction(6)),
    ):
        assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED
    other_axis = TrajectoryGoal(
        **{**{f: getattr(five, f) for f in five.__slots__}, "axis": "t"}
    )
    assert (
        _lean(
            render.refute(other_axis, THREE_STATE, ENERGY, Fraction(6)),
            lean_test_project,
        )
        is IndependentStatus.REJECTED
    )


@pytest.mark.parametrize(
    "start",
    [Fraction(0), Fraction(1), Fraction(2), Fraction(1, 2), Fraction(-3, 2)],
    ids=["0", "1", "2", "1/2", "-3/2"],
)
def test_the_time_binding_closes_for_any_start(lean_test_project, start) -> None:
    """The rendered time step closes for every start (task 165).

    Some starts `simp` alone closes and some it does not; the script is the
    same for both. The evolution is a concrete literal, as every registered
    model's is, so `simp` can unfold it.
    """
    entry = replace(THREE_STATE, evolution="forsetiEvolution")
    line = render._bind(entry, THREE_STATE.observations["obs-v"])[0]
    tactic = line.split(":= by ", 1)[1]
    literal = f"(mkRat ({start.numerator}) {start.denominator})"
    prelude = (
        "import Gimle.Forseti.Examples.ThreeStateContract\n\n"
        "def forsetiEvolution : Gimle.Asgard.Model.Evolution := {\n"
        '  states := [⟨"state-x", "dx", "initial-x"⟩]\n'
        '  initialPorts := [⟨"initial-x", "x0", .initial⟩]\n'
        '  initialValues := [⟨"initial-x", 1⟩]\n'
        '  axis := ⟨"time", "t"⟩\n'
        '  evolveAlong := "time"\n'
        f"  start := {literal}\n"
        "}\n\n"
        "def forsetiTime : Gimle.Asgard.Dynamics.TimeDomain :=\n"
        f'  ⟨"time", ((({literal}) : ℚ) : ℝ)⟩\n'
    )
    check = check_independently(
        prelude,
        ": forsetiTime = forsetiEvolution.time",
        (tactic,),
        SessionLimits(timeout_ms=600_000),
        lean_test_project,
    )
    assert check.status is IndependentStatus.CONFIRMED, check.reason


@pytest.mark.parametrize(
    ("entry", "proven_name", "band", "refuted_name", "refuted_band", "ports", "wide"),
    DRIVEN,
)
def test_a_driven_root_binds_its_driver_precondition(
    decided, entry, proven_name, band, refuted_name, refuted_band, ports, wide
) -> None:
    """A driven model's root states its drivers' precondition, never `True`."""
    proven = decided[proven_name]
    assert band.contract in proven.dependencies
    source = json.loads(proven.artifact)["source"]
    assert "(fun _ => True)" not in source
    assert "Initialized forsetiTime forsetiDrivers" in source
    assert f"{entry.evolution}.Admitted {entry.drivers.bindings} d" in source
    assert "|d t forsetiDriver0| ≤" in source
    assert ports in source
    refuted = decided[refuted_name]
    assert entry.drivers is not None
    assert entry.drivers.zero_admitted in refuted.dependencies
    assert refuted_band.initial_value_lemma in refuted.dependencies


def _without_precondition(
    unit: render.RootApplication, entry
) -> render.RootApplication:
    """Replace the root's driver predicate by `True`, keeping everything else."""
    start = unit.prelude.index("def forsetiDrivers")
    end = unit.prelude.index("def forsetiIdentities")
    prelude = (
        unit.prelude[:start]
        + "def forsetiDrivers : Gimle.Asgard.Dynamics.Signal\n"
        + "    (Gimle.Asgard.Model.DriverBinding.width "
        + f"{entry.drivers.bindings}) → Prop :=\n"
        + "  fun _ => True\n\n"
        + unit.prelude[end:]
    )
    return replace(unit, prelude=prelude)


@pytest.mark.parametrize(
    ("entry", "proven_name", "band", "refuted_name", "refuted_band", "ports", "wide"),
    DRIVEN,
)
def test_a_driven_root_without_its_precondition_is_refused_by_lean(
    decided,
    lean_test_project,
    entry,
    proven_name,
    band,
    refuted_name,
    refuted_band,
    ports,
    wide,
) -> None:
    """Lean itself will not prove the root once the precondition is dropped.

    The service refuses such a goal before Lean runs (`forced-no-precondition`,
    `hedged-no-precondition`). Here the band's own certificate is rendered with
    the driver predicate replaced by `True`, and with a driver bound above the
    contract's, bypassing the Python binding: both fail in Lean, so the
    precondition is carried by the checked statement, not only by the registry.
    """
    goal = next(e.goal for e in EXAMPLES if e.name == proven_name)
    document = json.loads(decided[proven_name].artifact)["evidence"]
    certificate = decode_evidence(document["certificate"], goal.context, LIMITS)
    unit = render.prove(goal, entry, band, certificate)
    assert _lean(unit, lean_test_project) is IndependentStatus.CONFIRMED
    assert (
        _lean(_without_precondition(unit, entry), lean_test_project)
        is IndependentStatus.REJECTED
    )
    assert (
        _lean(
            render.prove(replace(goal, drivers=wide), entry, band, certificate),
            lean_test_project,
        )
        is IndependentStatus.REJECTED
    )


@pytest.mark.parametrize(
    ("entry", "proven_name", "band", "refuted_name", "refuted_band", "ports", "wide"),
    DRIVEN,
)
def test_a_misstated_driver_entry_proves_and_refutes_nothing(
    decided,
    lean_test_project,
    entry,
    proven_name,
    band,
    refuted_name,
    refuted_band,
    ports,
    wide,
) -> None:
    """Wrong driver ports or a wrong index fail the identities in Lean."""
    drivers = entry.drivers
    assert drivers is not None
    goal = next(e.goal for e in EXAMPLES if e.name == proven_name)
    document = json.loads(decided[proven_name].artifact)["evidence"]
    certificate = decode_evidence(document["certificate"], goal.context, LIMITS)
    negative = next(e.goal for e in EXAMPLES if e.name == refuted_name)
    # Reverse the ports and point the index at the other one when there are
    # two. With one port no other index states a root at all (the checker then
    # reports unknown, not rejected), so misname it, and name one too many.
    if len(drivers.ports) > 1:
        misstatements = (
            replace(drivers, ports=tuple(reversed(drivers.ports))),
            replace(drivers, index=1),
        )
    else:
        misstatements = (
            replace(drivers, ports=("driver-x",)),
            replace(drivers, ports=(*drivers.ports, "driver-x")),
        )
    for misstated in (replace(entry, drivers=d) for d in misstatements):
        for unit in (
            render.prove(goal, misstated, band, certificate),
            render.refute(negative, misstated, refuted_band, Fraction(0)),
        ):
            assert _lean(unit, lean_test_project) is IndependentStatus.REJECTED
