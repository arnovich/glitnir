"""Tests for the deciders' front door.

The services run for real on the paths that need neither Lean nor Z3; where a
decision is needed, the service is replaced by one returning a genuine artifact
built from the same goal, so the binding and regeneration run unmodified.
"""

import dataclasses
import json
import os
import shlex
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from glitnir_forseti.claims import decide as front
from glitnir_forseti.claims.decide import (
    Decision,
    catalog,
    check_names,
    decide,
    read_goal,
    save,
)
from glitnir_forseti.claims.text import GoalTextError, render_goal
from glitnir_forseti.entailment import corpus
from glitnir_forseti.entailment.evidence import Certificate, Row, SquareSum
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import (
    Atom,
    Const,
    Context,
    Goal,
    Relation,
    Tru,
    Var,
)
from glitnir_forseti.entailment.syntax import encode_goal as encode_entailment
from glitnir_forseti.streams.goal import StreamGoal
from glitnir_forseti.streams.goal import encode_goal as encode_stream
from glitnir_forseti.streams.service import decide_stream
from glitnir_forseti.trajectory import replay, service
from glitnir_forseti.trajectory.examples import EXAMPLES
from glitnir_forseti.trajectory.goal import TrajectoryGoal, encode_goal
from glitnir_forseti.trajectory.service import Status, TrajectoryResult

LIMITS = EntailmentLimits()
GOALS = {example.name: example.goal for example in EXAMPLES}
ONE = Const(Fraction(1))
STEPPED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " within 256 observe s breach 1 probability <= 1/4"
)
BAND_CERTIFICATE = Certificate(
    (
        Row(SquareSum(()), (SquareSum(((Fraction(1), ONE),)), SquareSum(()))),
        Row(SquareSum(()), (SquareSum(()), SquareSum(((Fraction(1), ONE),)))),
    )
)


def _decided(goal: TrajectoryGoal, status: str, evidence) -> TrajectoryResult:
    artifact = replay.build_artifact(goal, evidence, LIMITS, {"stage": "test"})
    return TrajectoryResult(
        Status(status),
        artifact=artifact,
        stages=(service.Stage("certificate", "found"),),
        axioms=("propext",),
        dependencies=("energy_contract",),
    )


def _replace_decider(monkeypatch, goal_type, fake) -> None:
    kind = front._KINDS[goal_type]
    monkeypatch.setitem(front._KINDS, goal_type, dataclasses.replace(kind, decide=fake))


def test_text_and_json_read_to_the_same_goal() -> None:
    goal = GOALS["energy-band"]
    assert read_goal(render_goal(goal)) == goal
    assert read_goal(json.dumps(encode_goal(goal))) == goal
    field = read_goal(
        "stream formal-heat interpretation evaluated-field basis ogf profile x*x"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0"
    )
    assert isinstance(field, StreamGoal)
    assert read_goal(json.dumps(encode_stream(field))) == field


@pytest.mark.parametrize(
    ("source", "match"),
    [
        ('{"schema": "other"}', "unknown goal schema"),
        ("[1, 2]", "line 1"),
        ("{not json", "not valid JSON"),
        ('{"schema": "gimle.forseti.trajectory-goal/v1"}', "malformed trajectory"),
        ("entailment x : y = 0 |- true", "unknown coordinate"),
    ],
)
def test_unreadable_goals_are_errors(source: str, match: str) -> None:
    with pytest.raises(GoalTextError, match=match):
        read_goal(source)


def test_a_goal_that_is_not_text_is_refused() -> None:
    with pytest.raises(GoalTextError):
        read_goal(b"entailment x : x = 0 |- true")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        decide("entailment x : x = 0 |- true")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["stochastic", "energy-positive"])
def test_unsupported_trajectories_are_unknown_never_refuted(name: str) -> None:
    decision = decide(GOALS[name])
    assert decision.status == "UNKNOWN"
    assert decision.reason == "unsupported"
    assert decision.artifact is None and decision.lean is None
    outcomes = [stage["outcome"] for stage in decision.as_dict()["stages"]]
    assert any(outcome.startswith("unsupported") for outcome in outcomes)


def test_an_unsupported_stream_is_unknown() -> None:
    goal = read_goal(
        "stream formal-heat interpretation stochastic-ito basis ogf profile x*x"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0"
    )
    decision = decide(goal)
    assert (decision.kind, decision.status, decision.reason) == (
        "stream",
        "UNKNOWN",
        "unsupported",
    )
    assert decide_stream(goal).status.value == decision.status


def test_an_over_limit_entailment_is_unknown() -> None:
    tight = EntailmentLimits(max_nodes=2)
    decision = decide(corpus.DISK, tight)
    assert (decision.kind, decision.status, decision.reason) == (
        "entailment",
        "UNKNOWN",
        "resource-limit",
    )


def test_a_proof_carries_its_regenerated_lean(monkeypatch) -> None:
    goal = GOALS["energy-band"]
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(g, "PROVEN", BAND_CERTIFICATE),
    )
    decision = decide(goal)
    assert decision.status == "PROVEN" and decision.reason is None
    assert decision.lean is not None
    unit = replay.regenerate_application(decision.artifact, goal, LIMITS)
    assert isinstance(unit, tuple)
    assert decision.lean.statement == unit[2].statement
    assert decision.lean.script == tuple(unit[2].script)
    record = decision.as_dict()
    assert record["lean"]["statement"] == unit[2].statement
    assert record["goal"] == render_goal(goal)
    assert record["dependencies"] == ["energy_contract"]
    assert "artifact" not in record


def test_a_refutation_carries_the_negation(monkeypatch) -> None:
    goal = GOALS["energy-below-five"]
    witness = replay.InitialWitness(goal.start, goal.initial, Fraction(6))
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(g, "REFUTED", witness),
    )
    decision = decide(goal)
    assert decision.status == "REFUTED"
    assert decision.lean is not None and "¬" in decision.lean.statement


def test_an_artifact_that_does_not_bind_is_a_defect(monkeypatch) -> None:
    other = GOALS["energy-below-seven"]
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(other, "PROVEN", BAND_CERTIFICATE),
    )
    with pytest.raises(RuntimeError, match="does not bind"):
        decide(GOALS["energy-band"])


def test_a_status_that_disagrees_with_the_artifact_is_a_defect(monkeypatch) -> None:
    def lying(goal, limits, **_):
        decided = _decided(goal, "PROVEN", BAND_CERTIFICATE)
        return dataclasses.replace(decided, status=Status.REFUTED)

    _replace_decider(monkeypatch, TrajectoryGoal, lying)
    with pytest.raises(RuntimeError, match="does not bind"):
        decide(GOALS["energy-band"])


def test_save_writes_what_the_replay_command_reads(monkeypatch, tmp_path) -> None:
    goal = GOALS["energy-band"]
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(g, "PROVEN", BAND_CERTIFICATE),
    )
    decision = decide(goal)
    paths = save(decision, tmp_path / "artifacts")
    assert (tmp_path / "artifacts").is_dir()
    assert open(paths["artifact"], "rb").read() == decision.artifact
    assert read_goal(open(paths["goal"]).read()) == goal
    assert paths["replay"].startswith("python -m glitnir_forseti.trajectory replay ")
    # The command parses: without a Lean project it stops at the check, not at
    # reading the files.
    command = shlex.split(paths["replay"])
    completed = subprocess.run(
        [sys.executable, *command[1:], "--wall-time-ms", "1"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "invalid" not in completed.stdout


def test_save_refuses_an_unknown_decision(tmp_path) -> None:
    unknown = Decision("trajectory", GOALS["stochastic"], "UNKNOWN", "unsupported")
    with pytest.raises(ValueError, match="only a decided goal"):
        save(unknown, tmp_path)


def test_entailment_goals_dispatch_to_the_entailment_decider(monkeypatch) -> None:
    seen = []

    def fake(goal, limits, **_):
        seen.append(goal)
        from glitnir_forseti.entailment.service import (
            EntailmentResult,
            Status,
            UnknownReason,
        )

        return EntailmentResult(Status.UNKNOWN, UnknownReason.Z3_UNAVAILABLE)

    _replace_decider(monkeypatch, Goal, fake)
    decision = decide(read_goal("entailment x : x*x = 2 |- x < 0"))
    assert seen and decision.kind == "entailment"
    assert decision.reason == "z3-unavailable"


def test_an_over_limit_goal_is_unknown_in_either_encoding() -> None:
    text = "entailment a, b, c, d, e, f, g, h, i : a <= 1 |- true"
    as_text = read_goal(text)
    as_json = read_goal(json.dumps(encode_entailment(as_text)))
    assert as_text == as_json
    assert decide(as_json).reason == "resource-limit"


def test_a_goal_document_is_size_capped() -> None:
    with pytest.raises(GoalTextError, match="exceeds"):
        read_goal("{" + " " * 70000 + "}")


def test_an_exhausted_search_is_unknown_never_refuted(monkeypatch) -> None:
    def exhausted(goal, limits, **_):
        return TrajectoryResult(
            Status.UNKNOWN,
            service.UnknownReason.EXHAUSTED,
            stages=(service.Stage("certificate", "exhausted"),),
        )

    _replace_decider(monkeypatch, TrajectoryGoal, exhausted)
    decision = decide(GOALS["energy-at-least-one"])
    assert (decision.status, decision.reason) == ("UNKNOWN", "exhausted")
    assert decision.artifact is None and decision.lean is None


def test_a_goal_without_text_is_reported_as_json() -> None:
    goal = Goal(Context(("x", "true")), Atom(Relation.EQ, Var(1)), Tru())
    record = Decision("entailment", goal, "UNKNOWN", "timeout").as_dict()
    assert record["goal"] == encode_entailment(goal)


def test_the_catalog_names_every_registered_model() -> None:
    from glitnir_forseti.stepped.models import MODELS as steppeds
    from glitnir_forseti.streams.models import MODELS as streams
    from glitnir_forseti.trajectory.models import MODELS as trajectories

    text = catalog()
    for key in (*streams, *trajectories, *steppeds):
        assert f"  {key}:" in text
    assert (
        "Stepped models (shocks `variance v` or `subgaussian v`; claims"
        " `within N observe X breach B probability <= q`,"
        " `at every mark within N observe X breach B probability <= q`,"
        " `observe X expected <= c at N`):"
    ) in text
    assert (
        "  hedged-spread: from (0, 0) at 0 step 1/256, at most 4096 steps"
        " (1024 for fixed-date and expected claims);"
        " observe s (breach claims), V (expected claims)"
    ) in text
    assert "initial initial-x = 1, initial-y = 2, initial-z = -1" in text
    assert (
        "short: trajectory three-state from (1, 2, -1) at 2 observe V always"
        " : <formula over V>" in text
    )


def test_every_registered_model_has_an_example_that_reads() -> None:
    from glitnir_forseti.claims.decide import examples
    from glitnir_forseti.stepped.models import MODELS as steppeds
    from glitnir_forseti.streams.models import MODELS as streams
    from glitnir_forseti.trajectory.models import MODELS as trajectories

    listed = examples()
    assert {(e["kind"], e["model"]) for e in listed} == {
        ("entailment", ""),
        *(("trajectory", key) for key in trajectories),
        *(("stream", key) for key in streams),
        *(("stepped", key) for key in steppeds),
    }
    for example in listed:
        goal = read_goal(example["goal"])
        check_names(goal)
        assert example["goal"].startswith(example["kind"])
    burgers = next(e["goal"] for e in listed if e["model"] == "burgers")
    assert burgers.startswith("stream burgers viscosity 1/2 ")
    assert "potential exponentials" in burgers
    assert "viscosity" in catalog()
    band = next(e for e in listed if e["model"] == "three-state")
    assert band["goal"] == (
        "trajectory three-state from (1, 2, -1) at 2 observe V always"
        " : -V <= 0 and V <= 6"
    )
    # A stepped example sits at the exact horizon bound N·Δ²·v/B² = 1/4.
    [stepped] = [e for e in listed if e["kind"] == "stepped"]
    assert stepped["goal"] == (
        "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
        " within 256 observe s breach 1 probability <= 1/4"
    )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"model": "roessler"}, "no trajectory model 'roessler'"),
        ({"observation": "obs-x"}, "no observation 'obs-x'"),
        ({"name": "E"}, "named 'E'"),
        ({"initial": (("initial-x", Fraction(1)),)}, "initial ports are"),
    ],
)
def test_names_that_do_not_exist_are_errors_with_the_catalog(change, match) -> None:
    goal = dataclasses.replace(GOALS["energy-band"], **change)
    with pytest.raises(GoalTextError, match=match) as raised:
        check_names(goal)
    assert "three-state:" in str(raised.value)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"model": "roessler"}, "no stepped model 'roessler'"),
        ({"observation": "W"}, "hedged-spread has no observation 'W'"),
    ],
)
def test_stepped_names_that_do_not_exist_are_errors_with_the_catalog(
    change, match
) -> None:
    goal = dataclasses.replace(read_goal(STEPPED), **change)
    with pytest.raises(GoalTextError, match=match) as raised:
        check_names(goal)
    assert "Stepped models" in str(raised.value)
    assert "  hedged-spread: from (0, 0)" in str(raised.value)


def test_known_stepped_names_pass_even_when_the_claim_is_unsupported() -> None:
    goal = read_goal(STEPPED)
    check_names(goal)
    check_names(dataclasses.replace(goal, law="sub-gaussian"))
    check_names(dataclasses.replace(goal, step=Fraction(1, 100)))
    check_names(dataclasses.replace(goal, initial=(Fraction(1), Fraction(0))))


def test_known_names_pass_even_when_the_claim_is_unsupported() -> None:
    check_names(GOALS["stochastic"])
    check_names(GOALS["energy-positive"])
    check_names(corpus.DISK)
    field = read_goal(
        "stream formal-heat interpretation stochastic-ito basis ogf profile x*x"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0"
    )
    check_names(field)
    with pytest.raises(GoalTextError, match="no stream model"):
        check_names(dataclasses.replace(field, model="wave"))


def test_save_never_follows_or_replaces_a_file(monkeypatch, tmp_path) -> None:
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(g, "PROVEN", BAND_CERTIFICATE),
    )
    decision = decide(GOALS["energy-band"])
    paths = save(decision, tmp_path)
    assert save(decision, tmp_path) == paths  # the same bytes again: fine
    assert os.stat(paths["artifact"]).st_mode & 0o077 == 0
    Path(paths["goal"]).write_text("{}")
    with pytest.raises(OSError, match="different content"):
        save(decision, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    target = tmp_path / "target.json"
    target.write_bytes(decision.artifact)
    elsewhere.mkdir()
    name = Path(paths["artifact"]).name
    (elsewhere / name).symlink_to(target)
    with pytest.raises(OSError):
        save(decision, elsewhere)


def test_the_replay_command_quotes_its_paths(monkeypatch, tmp_path) -> None:
    _replace_decider(
        monkeypatch,
        TrajectoryGoal,
        lambda g, limits, **_: _decided(g, "PROVEN", BAND_CERTIFICATE),
    )
    paths = save(decide(GOALS["energy-band"]), tmp_path / "a dir; echo x")
    words = shlex.split(paths["replay"])
    assert words[4] == paths["artifact"] and words[6] == paths["goal"]


def test_every_short_form_the_catalog_advertises_parses() -> None:
    for line in catalog().splitlines():
        if line.strip().startswith("short: "):
            text = line.strip().removeprefix("short: ").rsplit(" : ", 1)[0]
            assert read_goal(text + " : true")
