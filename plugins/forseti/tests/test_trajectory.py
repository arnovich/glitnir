"""Tests for trajectory root goals that need no Lean.

Goal encoding, support decisions, rendering, the replay's binding checks and
the service's reasons run here; Lean's checks of the root theorems are in
``test_trajectory_lean.py``.
"""

import json
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment.evidence import Certificate, Row, SquareSum, Witness
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import InvalidEvidence, ReplayUnavailable
from glitnir_forseti.entailment.search import LaneOutcome
from glitnir_forseti.entailment.syntax import (
    Atom,
    Const,
    GoalError,
    Relation,
    Var,
)
from glitnir_forseti.lean.closure import closure_identity, replay_pin, verify_closure
from glitnir_forseti.lean.independent import IndependentCheck, IndependentStatus
from glitnir_forseti.lean.session import SessionLimits
from glitnir_forseti.trajectory import render, replay, service
from glitnir_forseti.trajectory.examples import (
    BAND,
    EXAMPLES,
    energy_goal,
    forced_goal,
    hedged_goal,
)
from glitnir_forseti.trajectory.goal import (
    TrajectoryGoal,
    check_goal_limits,
    decode_goal,
    encode_goal,
)
from glitnir_forseti.trajectory.models import (
    FORCED_DECAY,
    HEDGED_SPREAD,
    MODELS,
    THREE_STATE,
    replay_imports,
)

LIMITS = EntailmentLimits()
BAND_GOAL = energy_goal(BAND)
ENERGY = THREE_STATE.observations["obs-v"]
ONE = Const(Fraction(1))
BAND_CERTIFICATE = Certificate(
    (
        Row(SquareSum(()), (SquareSum(((Fraction(1), ONE),)), SquareSum(()))),
        Row(SquareSum(()), (SquareSum(()), SquareSum(((Fraction(1), ONE),)))),
    )
)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_goals_round_trip(example) -> None:
    document = json.loads(json.dumps(encode_goal(example.goal)))
    assert decode_goal(document, LIMITS) == example.goal


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(extra=1), "v1 fields"),
        (lambda d: d.update(schema="other"), "schema"),
        (lambda d: d.update(quantifier="sometimes"), "quantifier"),
        (lambda d: d.update(obligation="total"), "obligation"),
        (lambda d: d["time"].update(axis='t"x'), "stable ID"),
        (lambda d: d["observation"].update(port="obs v"), "stable ID"),
        (lambda d: d.update(initial=list(reversed(d["initial"]))), "sorted"),
        (lambda d: d.update(initial=d["initial"] + d["initial"][:1]), "sorted"),
        (lambda d: d["time"].update(start=[4, 2]), "lowest terms"),
        (lambda d: d.update(post=["atom", "le", ["var", 1]]), "coordinate"),
    ],
)
def test_malformed_goals_are_refused(change, match) -> None:
    document = json.loads(json.dumps(encode_goal(BAND_GOAL)))
    change(document)
    with pytest.raises(GoalError, match=match):
        decode_goal(document, LIMITS)


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (energy_goal(BAND, interpretation="stochastic-ito"), "interpretation"),
        (energy_goal(BAND, interpretation="formal-stream"), "interpretation"),
        (energy_goal(BAND, obligation="holds"), "partial correctness"),
        (energy_goal(BAND, quantifier="eventually"), "quantifier"),
        (energy_goal(BAND, observation="obs-x"), "no contract"),
        (energy_goal(BAND, name="E"), "named 'V'"),
        (
            energy_goal(BAND, initial=(("initial-x", Fraction(1)),)),
            "initial ports",
        ),
        (energy_goal(BAND, start=Fraction(3)), "start at t = 2"),
        (
            energy_goal(
                BAND,
                initial=(
                    ("initial-x", Fraction(1)),
                    ("initial-y", Fraction(2)),
                    ("initial-z", Fraction(1)),
                ),
            ),
            "declared initial state",
        ),
    ],
)
def test_unsupported_goals_are_named(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found
    result = service.decide_trajectory(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED
    assert result.stages[0].detail == found


def test_the_root_restates_every_identity() -> None:
    unit = render.prove(BAND_GOAL, THREE_STATE, ENERGY, BAND_CERTIFICATE)
    assert unit.statement == ": forsetiRoot"
    assert '= ["initial-x", "initial-y", "initial-z"]' in unit.prelude
    assert '"obs-v"' in unit.prelude and 'axis.id = "time"' in unit.prelude
    assert "Gimle.Forseti.Trajectory.Contract" in unit.prelude
    assert "open " not in unit.prelude
    assert ENERGY.contract in unit.dependencies
    refuted = render.refute(BAND_GOAL, THREE_STATE, ENERGY, Fraction(6))
    assert refuted.statement == ": forsetiIdentities ∧ ¬ forsetiContract"
    assert ENERGY.initial_value_lemma in refuted.dependencies


def test_the_initial_state_is_rendered_in_state_order() -> None:
    goal = energy_goal(
        BAND,
        initial=(
            ("initial-x", Fraction(7)),
            ("initial-y", Fraction(-1, 3)),
            ("initial-z", Fraction(5)),
        ),
    )
    assert render.initial_point(goal, THREE_STATE) == (
        Fraction(7),
        Fraction(-1, 3),
        Fraction(5),
    )
    text = render.prelude(goal, THREE_STATE, ENERGY)
    assert "![(((mkRat (7) 1) : ℚ) : ℝ), (((mkRat (-1) 3) : ℚ) : ℝ)" in text


# --- replay binding, before Lean -------------------------------------------


@pytest.fixture
def no_lean(monkeypatch):
    """Fail the test if replay reaches Lean."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay reached Lean")

    monkeypatch.setattr(replay, "verify_closure", forbidden)
    monkeypatch.setattr(replay, "check_independently", forbidden)


def _artifact(goal=BAND_GOAL, evidence=BAND_CERTIFICATE) -> bytes:
    return replay.build_artifact(goal, evidence, LIMITS, {"stage": "test"})


def _witness(goal=BAND_GOAL, value=Fraction(6)) -> replay.InitialWitness:
    return replay.InitialWitness(goal.start, goal.initial, value)


def _edit(artifact: bytes, change) -> bytes:
    document = json.loads(artifact)
    change(document)
    return json.dumps(document).encode()


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
        energy_goal(Atom(Relation.LE, Var(0))),
        energy_goal(BAND, quantifier="at-start"),
        energy_goal(BAND, obligation="holds"),
        energy_goal(BAND, observation="obs-x"),
    ],
)
def test_an_old_certificate_is_bound_to_its_own_root(no_lean, other) -> None:
    outcome = replay.replay_root(_artifact(), other, LIMITS)
    assert isinstance(outcome, (InvalidEvidence, ReplayUnavailable))
    if isinstance(outcome, InvalidEvidence):
        assert "different root goal" in outcome.reason


@pytest.mark.parametrize(
    ("artifact", "reason"),
    [
        (lambda: _edit(_artifact(), lambda d: d.update(status="REFUTED")), "theorem"),
        (
            lambda: _edit(
                _artifact(),
                lambda d: d.update(
                    status="REFUTED",
                    theorem={"name": "forsetiSearch", "statement": "negation"},
                ),
            ),
            "kind of evidence",
        ),
        (
            lambda: _artifact(
                BAND_GOAL,
                replay.InitialWitness(Fraction(3), BAND_GOAL.initial, Fraction(6)),
            ),
            "start state",
        ),
        (
            lambda: _edit(
                _artifact(),
                lambda d: d["environment"].update(closure_sha256="0" * 64),
            ),
            "Lean environment",
        ),
        (
            lambda: _edit(
                _artifact(),
                lambda d: d.update(
                    source=d["source"].replace("decide +kernel", "sorry")
                ),
            ),
            "retained source",
        ),
        (
            lambda: _edit(
                _artifact(),
                lambda d: d["evidence"]["certificate"]["rows"].pop(),
            ),
            "malformed",
        ),
        (lambda: b"[" * 10_000, "not JSON"),
        (lambda: _edit(_artifact(), lambda d: d.update(status=[])), "status"),
    ],
)
def test_replay_rejects_before_lean(no_lean, artifact, reason) -> None:
    outcome = replay.replay_root(artifact(), BAND_GOAL, LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert reason in outcome.reason


def test_replay_maps_lean_outcomes(monkeypatch) -> None:
    confirmed = IndependentCheck(IndependentStatus.CONFIRMED)
    monkeypatch.setattr(replay, "verify_closure", lambda *a, **k: confirmed)
    for check, expected in (
        (IndependentCheck(IndependentStatus.CONFIRMED, ("propext",)), "validated"),
        (IndependentCheck(IndependentStatus.CONFIRMED, ("sorryAx",)), "invalid"),
        (IndependentCheck(IndependentStatus.REJECTED, reason="no"), "invalid"),
        (IndependentCheck(IndependentStatus.UNKNOWN, reason="late"), "unavailable"),
    ):
        monkeypatch.setattr(replay, "check_independently", lambda *a, c=check, **k: c)
        outcome = replay.replay_root(_artifact(), BAND_GOAL, LIMITS)
        kind = {
            replay.ValidatedRoot: "validated",
            InvalidEvidence: "invalid",
            ReplayUnavailable: "unavailable",
        }[type(outcome)]
        assert kind == expected
    refuted = replay.replay_root(_artifact(evidence=_witness()), BAND_GOAL, LIMITS)
    assert isinstance(refuted, ReplayUnavailable)


# --- the service -----------------------------------------------------------


def _stages(monkeypatch, certificate: LaneOutcome, witness: LaneOutcome) -> list:
    leaves = []

    def fake_certify(goal, *args, **kwargs):
        leaves.append(("certificate", goal))
        return certificate

    def fake_refute(goal, *args, **kwargs):
        leaves.append(("witness", goal))
        return witness

    monkeypatch.setattr(service, "certify", fake_certify)
    monkeypatch.setattr(service, "refute", fake_refute)
    return leaves


@pytest.mark.parametrize(
    ("certificate", "witness", "reason"),
    [
        ("none", "none", "exhausted"),
        ("unsupported-fragment", "none", "unsupported"),
        ("none", "unsupported-witness", "unsupported"),
        ("z3-unavailable", "z3-unavailable", "z3-unavailable"),
        ("timeout", "none", "timeout"),
        ("cancelled", "none", "cancelled"),
        ("resource-limit", "none", "resource-limit"),
    ],
)
def test_unknown_reasons(monkeypatch, certificate, witness, reason) -> None:
    _stages(monkeypatch, LaneOutcome(certificate), LaneOutcome(witness))
    result = service.decide_trajectory(BAND_GOAL)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is not None and result.reason.value == reason


def test_the_leaves_are_the_band_and_the_initial_value(monkeypatch) -> None:
    leaves = _stages(monkeypatch, LaneOutcome("none"), LaneOutcome("none"))
    service.decide_trajectory(BAND_GOAL)
    (_, first), (_, second) = leaves
    assert first.antecedent == ENERGY.invariant and first.consequent == BAND
    assert second.consequent == BAND
    assert "6" in str(second.antecedent)


def test_only_a_replayed_root_decides(monkeypatch) -> None:
    witness = Witness((("V", Fraction(6)),))
    _stages(monkeypatch, LaneOutcome("none"), LaneOutcome("found", evidence=witness))
    seen = []

    def fake_replay(artifact, goal, *args, **kwargs):
        seen.append(json.loads(artifact)["status"])
        return InvalidEvidence("Lean rejected the root proof")

    monkeypatch.setattr(service, "replay_root", fake_replay)
    result = service.decide_trajectory(BAND_GOAL)
    assert seen == ["REFUTED"]
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.EXHAUSTED


def test_lean_unavailable_after_a_candidate(monkeypatch) -> None:
    monkeypatch.delenv("FORSETI_LEAN_PROJECT", raising=False)
    _stages(
        monkeypatch,
        LaneOutcome("found", evidence=BAND_CERTIFICATE),
        LaneOutcome("none"),
    )
    result = service.decide_trajectory(BAND_GOAL)
    assert result.reason is service.UnknownReason.LEAN_UNAVAILABLE


# --- pins and commands -----------------------------------------------------


def test_every_model_import_set_is_pinned() -> None:
    pin = replay_pin()
    pinned = [tuple(entry["imports"]) for entry in pin["closures"]]
    for imports in replay_imports():
        assert imports in pinned
        assert len(closure_identity(imports)) == 64
    assert closure_identity(THREE_STATE.imports) != closure_identity(
        ("Gimle.Forseti.Syntax.Certificate",)
    )
    assert set(MODELS) == {
        "three-state",
        "damped-oscillator",
        "harmonic-oscillator",
        "forced-decay",
        "hedged-spread",
        "lorenz",
        "ns-t3",
        "ns-t3s",
        "ns-k5",
        "ns-b2",
    }
    # Each model checks in its own closure.
    assert len({closure_identity(entry.imports) for entry in MODELS.values()}) == 10


def test_an_unpinned_import_set_is_unavailable() -> None:
    check = verify_closure(("Gimle.Forseti.Stream",), SessionLimits())
    assert check.status is IndependentStatus.UNKNOWN
    assert "no closure" in check.reason
    with pytest.raises(KeyError):
        closure_identity(("Gimle.Forseti.Stream",))


def test_the_commands_report_errors_without_a_traceback(tmp_path, capsys) -> None:
    from glitnir_forseti.trajectory.__main__ import main

    goal = tmp_path / "goal.json"
    goal.write_text(json.dumps(encode_goal(BAND_GOAL)))
    other = tmp_path / "other.json"
    other.write_text(json.dumps(encode_goal(energy_goal(Atom(Relation.LE, Var(0))))))
    artifact = tmp_path / "artifact.json"
    artifact.write_bytes(_artifact())
    assert main(["replay", str(artifact), "--goal", str(other)]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid"
    goal.write_text("[" * 100_000)
    assert main(["replay", str(artifact), "--goal", str(goal)]) == 2
    assert main(["decide", str(tmp_path / "missing.json")]) == 2


def test_a_changed_axis_is_unsupported() -> None:
    goal = TrajectoryGoal(
        model="three-state",
        interpretation="continuous-real",
        obligation="contract",
        axis="t",
        start=Fraction(2),
        initial=BAND_GOAL.initial,
        observation="obs-v",
        name="V",
        quantifier="always",
        post=BAND,
    )
    found = replay.supported(goal)
    assert isinstance(found, str) and "evolves along 'time'" in found


def test_the_pin_keeps_other_platforms_binaries(monkeypatch) -> None:
    """A platform adds its binary digest; closures must match to share a pin."""
    from glitnir_forseti.lean import closure

    observed = closure.ClosureObservation(
        ("Gimle.Forseti.Syntax.Certificate",),
        1,
        (("forseti", 1, "a" * 64),),
        "Darwin-arm64",
        "b" * 64,
    )
    previous = closure.build_pin(
        [
            closure.ClosureObservation(
                observed.imports, 1, observed.packages, "Linux-x86_64", "c" * 64
            )
        ]
    )
    merged = closure.build_pin([observed], previous)
    assert merged["toolchain_binaries"] == {
        "Darwin-arm64": "b" * 64,
        "Linux-x86_64": "c" * 64,
    }
    other = closure.ClosureObservation(
        observed.imports, 1, (("forseti", 1, "d" * 64),), "Darwin-arm64", "b" * 64
    )
    with pytest.raises(ValueError, match="not byte-identical"):
        closure.build_pin([other], previous)


def _observation(platform: str, binaries: str, digest: str = "a" * 64):
    from glitnir_forseti.lean import closure

    return closure.ClosureObservation(
        ("Gimle.Forseti.Syntax.Certificate",),
        1,
        (("forseti", 1, digest),),
        platform,
        binaries,
    )


def test_the_pin_names_the_toolchain_its_binaries_belong_to() -> None:
    from glitnir_forseti.lean import closure

    pin = closure.build_pin([_observation("Linux-x86_64", "c" * 64)])
    assert pin["toolchain"] == closure.TOOLCHAIN
    assert closure.replay_pin()["toolchain"] == closure.TOOLCHAIN


def test_a_release_bump_keeps_other_platforms_binaries() -> None:
    """A release bump on one platform keeps the other platforms' digests.

    The binaries change with the toolchain, not the release, and replay
    re-observes every closure where it runs.
    """
    from glitnir_forseti.lean import closure

    previous = {
        **closure.build_pin([_observation("Linux-x86_64", "c" * 64)]),
        "release_sha256": "0" * 64,
    }
    # The new release compiles differently, so its closures differ.
    merged = closure.build_pin(
        [_observation("Darwin-arm64", "b" * 64, "d" * 64)], previous
    )
    assert merged["toolchain_binaries"] == {
        "Darwin-arm64": "b" * 64,
        "Linux-x86_64": "c" * 64,
    }
    assert merged["release_sha256"] == closure.RELEASE_SHA256


def test_a_toolchain_change_drops_other_platforms_binaries() -> None:
    from glitnir_forseti.lean import closure

    previous = {
        **closure.build_pin([_observation("Linux-x86_64", "c" * 64)]),
        "release_sha256": "0" * 64,
        "toolchain": "leanprover/lean4:v0.0.0",
    }
    merged = closure.build_pin([_observation("Darwin-arm64", "b" * 64)], previous)
    assert merged["toolchain_binaries"] == {"Darwin-arm64": "b" * 64}


def test_a_closure_program_change_keeps_other_platforms_binaries() -> None:
    from glitnir_forseti.lean import closure

    previous = {
        **closure.build_pin([_observation("Linux-x86_64", "c" * 64)]),
        "closure_program_sha256": "0" * 64,
    }
    merged = closure.build_pin(
        [_observation("Darwin-arm64", "b" * 64, "d" * 64)], previous
    )
    assert set(merged["toolchain_binaries"]) == {"Darwin-arm64", "Linux-x86_64"}


def test_a_pin_that_names_no_toolchain_keeps_others_only_for_its_release() -> None:
    """A pin from before the toolchain was named cannot say what it covers."""
    from glitnir_forseti.lean import closure

    legacy = closure.build_pin([_observation("Linux-x86_64", "c" * 64)])
    del legacy["toolchain"]
    same = closure.build_pin([_observation("Darwin-arm64", "b" * 64)], legacy)
    assert set(same["toolchain_binaries"]) == {"Darwin-arm64", "Linux-x86_64"}
    bumped = closure.build_pin(
        [_observation("Darwin-arm64", "b" * 64, "d" * 64)],
        {**legacy, "release_sha256": "0" * 64},
    )
    assert bumped["toolchain_binaries"] == {"Darwin-arm64": "b" * 64}


def test_a_platform_may_add_an_import_set_to_the_same_release() -> None:
    """A new closure is added beside the pinned ones; a changed one still is not."""
    from glitnir_forseti.lean import closure

    previous = closure.build_pin([_observation("Linux-x86_64", "c" * 64)])
    added = closure.ClosureObservation(
        ("Gimle.Forseti.Syntax.Positivstellensatz",),
        2,
        (("forseti", 2, "e" * 64),),
        "Darwin-arm64",
        "b" * 64,
    )
    merged = closure.build_pin(
        [_observation("Darwin-arm64", "b" * 64), added], previous
    )
    assert [entry["imports"] for entry in merged["closures"]] == [
        ["Gimle.Forseti.Syntax.Certificate"],
        ["Gimle.Forseti.Syntax.Positivstellensatz"],
    ]
    assert set(merged["toolchain_binaries"]) == {"Darwin-arm64", "Linux-x86_64"}
    with pytest.raises(ValueError, match="not byte-identical"):
        closure.build_pin(
            [_observation("Darwin-arm64", "b" * 64, "d" * 64), added], previous
        )


def test_a_platform_may_not_drop_an_import_set_of_the_same_release() -> None:
    from glitnir_forseti.lean import closure

    other = closure.ClosureObservation(
        ("Gimle.Forseti.Syntax.Positivstellensatz",),
        2,
        (("forseti", 2, "e" * 64),),
        "Linux-x86_64",
        "c" * 64,
    )
    previous = closure.build_pin([_observation("Linux-x86_64", "c" * 64), other])
    with pytest.raises(ValueError, match="does not observe"):
        closure.build_pin([_observation("Darwin-arm64", "b" * 64)], previous)


def test_a_pin_made_for_another_toolchain_confirms_nothing(monkeypatch) -> None:
    from glitnir_forseti.lean import closure

    pin = closure.replay_pin()
    monkeypatch.setattr(
        closure, "replay_pin", lambda: {**pin, "toolchain": "leanprover/lean4:v0.0.0"}
    )
    monkeypatch.setattr(
        closure,
        "observe_closure",
        lambda *a, **k: pytest.fail("a foreign pin needs no observation"),
    )
    check = closure.verify_closure(THREE_STATE.imports, SessionLimits())
    assert check.status is IndependentStatus.UNKNOWN
    assert "another Lean toolchain" in check.reason


def test_artifact_identity_leaves_out_the_binaries(monkeypatch) -> None:
    from glitnir_forseti.lean import closure

    pin = closure.replay_pin()
    before = closure.closure_identity(THREE_STATE.imports)
    monkeypatch.setattr(
        closure,
        "replay_pin",
        lambda: {**pin, "toolchain_binaries": {"Darwin-arm64": "e" * 64}},
    )
    assert closure.closure_identity(THREE_STATE.imports) == before


def test_every_model_binds_its_time_with_one_script() -> None:
    """No start is special-cased: the time binding is the same for each model."""
    from glitnir_forseti.trajectory import render

    scripts = {
        entry.key: render._bind(entry, next(iter(entry.observations.values())))[0]
        for entry in MODELS.values()
    }
    for key, script in scripts.items():
        evolution = MODELS[key].evolution
        assert script == (
            f"have time : forsetiTime = {evolution}.time := by simp [forsetiTime, "
            f"Gimle.Asgard.Model.Evolution.time, {evolution}] "
            "<;> norm_num [Rat.mkRat_eq_div]"
        )


def test_oscillator_goals_need_its_declared_start() -> None:
    from glitnir_forseti.trajectory.examples import OSCILLATOR_INITIAL, oscillator_goal

    goal = oscillator_goal(Atom(Relation.LE, Var(0)), start=Fraction(1))
    assert "t = 0" in replay.supported(goal)
    other = oscillator_goal(
        Atom(Relation.LE, Var(0)),
        initial=(("initial-v", Fraction(1)), ("initial-x", Fraction(1))),
    )
    assert "declared initial state" in replay.supported(other)
    assert OSCILLATOR_INITIAL == (
        ("initial-v", Fraction(0)),
        ("initial-x", Fraction(1)),
    )


# --- driven models -----------------------------------------------

DECAY = FORCED_DECAY.observations["obs-x"]
DECAY_GOAL = forced_goal(Atom(Relation.LE, Var(0)))


def test_an_undriven_goal_encodes_without_drivers() -> None:
    assert "drivers" not in encode_goal(BAND_GOAL)
    assert encode_goal(DECAY_GOAL)["drivers"] == [["driver-u", [1, 1]]]


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(drivers=[]), "non-empty"),
        (lambda d: d.update(drivers=[["driver-u", [-1, 1]]]), "nonnegative"),
        (lambda d: d.update(drivers=[["driver-u", [1, 1]]] * 2), "sorted"),
        (lambda d: d.update(drivers=[["driver u", [1, 1]]]), "stable ID"),
        (lambda d: d.update(drivers=[["driver-u"]]), "port ID, rational"),
        (lambda d: d.update(drivers={"driver-u": [1, 1]}), "non-empty"),
    ],
)
def test_malformed_driver_bounds_are_refused(change, match) -> None:
    document = json.loads(json.dumps(encode_goal(DECAY_GOAL)))
    change(document)
    with pytest.raises(GoalError, match=match):
        decode_goal(document, LIMITS)


@pytest.mark.parametrize(
    ("goal", "reason"),
    [
        (forced_goal(BAND, drivers=()), "must state its driver precondition"),
        (
            forced_goal(BAND, drivers=(("driver-du", Fraction(1)),)),
            "bounds exactly the driver 'driver-u'",
        ),
        (
            forced_goal(BAND, drivers=(("driver-u", Fraction(3, 2)),)),
            "assumes |driver-u| <= 1, not 3/2",
        ),
    ],
)
def test_a_driven_goal_must_state_the_contract_precondition(goal, reason) -> None:
    found = replay.supported(goal)
    assert isinstance(found, str) and reason in found
    result = service.decide_trajectory(goal)
    assert result.status is service.Status.UNKNOWN
    assert result.reason is service.UnknownReason.UNSUPPORTED


def test_an_undriven_goal_may_not_bound_drivers() -> None:
    goal = replace(BAND_GOAL, drivers=(("driver-u", Fraction(1)),))
    found = replay.supported(goal)
    assert isinstance(found, str) and "has no drivers" in found


def test_a_driven_root_states_its_precondition() -> None:
    unit = render.prove(DECAY_GOAL, FORCED_DECAY, DECAY, BAND_CERTIFICATE)
    assert "(fun _ => True)" not in unit.prelude
    assert "Initialized forsetiTime forsetiDrivers" in unit.prelude
    assert (
        "fun d => Gimle.Forseti.Examples.ForcedDecay.evolution.Admitted "
        "Gimle.Forseti.Examples.ForcedDecay.drivers d ∧" in unit.prelude
    )
    assert "|d t forsetiDriver0| ≤ (((mkRat (1) 1) : ℚ) : ℝ)" in unit.prelude
    assert '= ["driver-u", "driver-du"]' in unit.prelude
    assert '"driver-u" = some forsetiDriver0' in unit.prelude
    refuted = render.refute(DECAY_GOAL, FORCED_DECAY, DECAY, Fraction(0))
    assert refuted.prelude == render.prelude(DECAY_GOAL, FORCED_DECAY, DECAY)
    assert FORCED_DECAY.drivers is not None
    assert FORCED_DECAY.drivers.zero_admitted in refuted.dependencies


HEDGED_ENERGY = HEDGED_SPREAD.observations["obs-v"]


def test_a_driver_without_a_derivative_port_binds_its_one_coordinate() -> None:
    """A one-port driver's roots name it and refute through `zero_admitted`.

    The hedged spread's driver has no derivative port, so the refutation at
    the start cannot cite a derivative; it cites the entry's admitted zero
    driver instead.
    """
    goal = hedged_goal(Atom(Relation.LE, Var(0)), observe="obs-v")
    found = replay.supported(goal)
    assert found == (HEDGED_SPREAD, HEDGED_ENERGY)
    unit = render.prove(goal, HEDGED_SPREAD, HEDGED_ENERGY, BAND_CERTIFICATE)
    assert '= ["driver-d"]' in unit.prelude
    assert '"driver-d" = some forsetiDriver0' in unit.prelude
    assert "def forsetiObservation : Fin 2 := ⟨1, by decide⟩" in unit.prelude
    assert (
        "have obs : forsetiObservation = "
        "Gimle.Forseti.Examples.HedgedSpread.vIndex := rfl" in unit.script
    )
    refuted = render.refute(goal, HEDGED_SPREAD, HEDGED_ENERGY, Fraction(0))
    assert HEDGED_SPREAD.drivers is not None
    zero = HEDGED_SPREAD.drivers.zero_admitted
    assert any(line.endswith(f":= {zero}") for line in refuted.script)
    assert "hasDerivAt_const" not in "\n".join(refuted.script)
    assert zero in refuted.dependencies
    assert HEDGED_ENERGY.initial_value_lemma in refuted.dependencies
    # Its start values need no admission, so the lemma is applied without one;
    # the forced decay's does, and keeps it.
    hedged_start = next(line for line in refuted.script if "output_at_start_v" in line)
    assert "using zero) output related" not in hedged_start
    decay = render.refute(DECAY_GOAL, FORCED_DECAY, DECAY, Fraction(0))
    decay_start = next(line for line in decay.script if "output_at_start" in line)
    assert "using zero) output related" in decay_start


def test_an_undriven_root_keeps_true_as_its_driver_predicate() -> None:
    text = render.prelude(BAND_GOAL, THREE_STATE, ENERGY)
    assert "Initialized forsetiTime (fun _ => True)" in text
    assert "forsetiDrivers" not in text


def test_an_old_certificate_is_bound_to_its_driver_bound(no_lean) -> None:
    artifact = _artifact(DECAY_GOAL, BAND_CERTIFICATE)
    other = forced_goal(DECAY_GOAL.post, drivers=(("driver-u", Fraction(1, 2)),))
    outcome = replay.replay_root(artifact, other, LIMITS)
    assert isinstance(outcome, InvalidEvidence)
    assert "different root goal" in outcome.reason


@pytest.mark.parametrize(
    ("drivers", "match"),
    [
        ([("driver-u", Fraction(1))], "tuple"),
        ((("driver-u", 1),), "fractions.Fraction"),
        ((("driver-u", Fraction(-1, 2)),), "nonnegative"),
        ((("driver-v", Fraction(1)), ("driver-u", Fraction(1))), "sorted"),
    ],
)
def test_driver_bounds_are_checked_at_construction(drivers, match) -> None:
    with pytest.raises(GoalError, match=match):
        replace(DECAY_GOAL, drivers=drivers)


def test_too_many_driver_bounds_exceed_the_limits() -> None:
    goal = replace(
        DECAY_GOAL,
        drivers=tuple((f"driver-{i:03}", Fraction(1)) for i in range(40)),
    )
    small = EntailmentLimits(max_variables=8)
    with pytest.raises(GoalError, match="too many bounded drivers"):
        check_goal_limits(goal, small)


def test_the_initial_state_is_bounded_by_the_model_size_not_the_formula() -> None:
    """A 12-state model's goal reads; a 33-state initial list is refused."""
    from glitnir_forseti.trajectory.examples import ns_b2_goal
    from glitnir_forseti.trajectory.goal import MAX_STATES

    goal = ns_b2_goal(BAND)
    assert len(goal.initial) == 12 > EntailmentLimits().max_variables
    assert decode_goal(encode_goal(goal), EntailmentLimits()) == goal
    check_goal_limits(goal, EntailmentLimits())
    data = encode_goal(goal)
    data["initial"] = [[f"initial-{i}", "1"] for i in range(MAX_STATES + 1)]
    with pytest.raises(GoalError, match="bounded list"):
        decode_goal(data, EntailmentLimits())
    ports = tuple((f"initial-{i:03}", Fraction(1)) for i in range(MAX_STATES + 1))
    wide = replace(goal, initial=ports)
    with pytest.raises(GoalError, match="too many initial ports"):
        check_goal_limits(wide, EntailmentLimits())


def test_a_twelve_state_initial_witness_replays_its_binding(no_lean) -> None:
    """The witness decoder shares the model-size bound, not the formula's."""
    from glitnir_forseti.trajectory.examples import ns_b2_goal

    goal = ns_b2_goal(Atom(Relation.LE, Var(0)))  # E <= 0, false at the start
    witness = _witness(goal, Fraction(91, 20))
    artifact = replay.build_artifact(goal, witness, LIMITS, {"stage": "test"})
    outcome = replay.regenerate_application(artifact, goal, LIMITS)
    assert not isinstance(outcome, (InvalidEvidence, ReplayUnavailable)), outcome
    _, evidence, _ = outcome
    assert evidence == witness and len(evidence.initial) == 12
