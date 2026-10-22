r"""Live Lean tests for entailment search and replay (task 111).

These run against the pinned standalone release. They skip without one, and
fail instead under the gate::

    FORSETI_REQUIRE_LEAN_PROJECT=1 \
    FORSETI_LEAN_PROJECT=/path/to/forseti-lean-v1.26.0 \
        uv run python -m pytest tests/test_entailment_lean.py
"""

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from fractions import Fraction

import pytest

from glitnir_forseti.entailment import corpus, render
from glitnir_forseti.entailment.algebraic import (
    AlgebraicRoot,
    AlgebraicWitness,
    AtomProof,
)
from glitnir_forseti.entailment.artifact import build_artifact
from glitnir_forseti.entailment.evidence import (
    Certificate,
    PsatzCertificate,
    Row,
    SquareSum,
    Witness,
    nonstrict,
)
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.replay import (
    InvalidEvidence,
    ReplayUnavailable,
    ValidatedEntailment,
    replay_artifact,
)
from glitnir_forseti.entailment.service import Status, UnknownReason, decide_entailment
from glitnir_forseti.entailment.syntax import (
    Add,
    Atom,
    Const,
    Goal,
    Neg,
    Relation,
    Var,
    encode_goal,
)
from glitnir_forseti.lean import closure
from glitnir_forseti.lean.independent import IndependentStatus, check_independently
from glitnir_forseti.lean.session import SessionLimits

LIMITS = EntailmentLimits()
SESSION = SessionLimits(timeout_ms=600_000)
ONE = Const(Fraction(1))


@pytest.fixture(scope="module")
def decided(lean_test_project):
    """Decide every corpus case once, from the formulas alone."""
    return {
        case.name: decide_entailment(
            case.goal, dictionary=case.dictionary, project=lean_test_project
        )
        for case in corpus.CASES
    }


@pytest.mark.parametrize("case", corpus.CASES, ids=lambda case: case.name)
def test_the_corpus_is_decided_as_required(decided, case) -> None:
    result = decided[case.name]
    got = result.status.value + (f":{result.reason.value}" if result.reason else "")
    assert got == case.expected, result.lanes
    if result.status is not Status.UNKNOWN:
        assert result.artifact is not None
        assert set(result.axioms) <= {"propext", "Classical.choice", "Quot.sound"}
        checked = [lane for lane in result.lanes if lane.check == "validated"]
        assert len(checked) == 1 and checked[0].check_ms > 0


def test_a_restricted_dictionary_does_not_refute(decided) -> None:
    """A failed certificate search is never a refutation."""
    result = decided["quartic-restricted"]
    assert [lane.outcome for lane in result.lanes] == ["none", "none"]


def test_artifacts_replay_offline_in_a_fresh_process(
    decided, lean_test_project, tmp_path
) -> None:
    """Replay runs with Z3 and every producer module made unimportable."""
    blocked = [
        "z3",
        "glitnir_forseti.entailment.search",
        "glitnir_forseti.entailment.solver",
        "glitnir_forseti.entailment.worker",
        "glitnir_forseti.entailment.service",
        "glitnir_forseti.entailment.algebraic_search",
    ]
    for name in ("disk", "half-disk", "root-two", "product"):
        case = next(c for c in corpus.CASES if c.name == name)
        artifact = tmp_path / f"{name}.json"
        goal = tmp_path / f"{name}.goal.json"
        artifact.write_bytes(decided[name].artifact)
        goal.write_text(json.dumps(encode_goal(case.goal)))
        code = (
            "import sys\n"
            f"for name in {blocked!r}:\n"
            "    sys.modules[name] = None\n"
            "from glitnir_forseti.entailment.__main__ import main\n"
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
        assert report["status"] == case.expected


def test_replay_rejects_an_artifact_for_another_goal(
    decided, lean_test_project
) -> None:
    artifact = decided["box"].artifact
    for other in (corpus.BOX_THREE_HALVES, corpus.DISK_REORDERED):
        outcome = replay_artifact(artifact, other, LIMITS, lean_test_project)
        assert isinstance(outcome, InvalidEvidence)


def _certificate(artifact: bytes) -> Certificate:
    from glitnir_forseti.entailment.evidence import decode_evidence

    document = json.loads(artifact)
    evidence = decode_evidence(document["evidence"], corpus.PLANE, LIMITS)
    assert isinstance(evidence, Certificate)
    return evidence


def test_lean_rejects_a_changed_weight_with_consistent_source(
    decided, lean_test_project
) -> None:
    """Changing a weight and regenerating the source reaches Lean, which refuses."""
    certificate = _certificate(decided["disk"].artifact)
    first = certificate.rows[0]
    (weight, square), *rest = first.base.terms
    changed = Certificate(
        (
            Row(SquareSum(((weight * 2, square), *rest)), first.multipliers),
            *certificate.rows[1:],
        )
    )
    artifact = build_artifact(
        corpus.DISK, changed, render.prove(corpus.DISK, changed), LIMITS, {}
    )
    outcome = replay_artifact(artifact, corpus.DISK, LIMITS, lean_test_project)
    assert isinstance(outcome, InvalidEvidence)
    assert "Lean rejected" in outcome.reason


def _lean(prelude: str, statement: str, script: str, project) -> IndependentStatus:
    return check_independently(prelude, statement, (script,), SESSION, project).status


def test_goal_binding_lives_in_lean_too(decided, lean_test_project) -> None:
    """Bypass the Python goal comparison: Lean still refuses mismatched evidence."""
    box = _certificate_for(decided["box"].artifact, corpus.LINE)
    stale = Goal(
        corpus.LINE,
        corpus.BOX.antecedent,
        Atom(Relation.LE, Add(Var(0), Neg(Const(Fraction(1, 2))))),
    )
    application = render.prove(stale, box)
    status = _lean(
        application.prelude,
        application.statement,
        application.script[0],
        lean_test_project,
    )
    assert status is IndependentStatus.REJECTED
    dropped = Certificate(tuple(Row(row.base, ()) for row in box.rows))
    application = render.prove(corpus.BOX, dropped)
    status = _lean(
        application.prelude,
        application.statement,
        application.script[0],
        lean_test_project,
    )
    assert status is IndependentStatus.REJECTED
    # The counterexample to x ≤ 0 ⊨ y ≤ 0, read against the goal with the two
    # sides' coordinates swapped, is no counterexample: y ≤ 0 fails there.
    witness = _witness(decided["unrelated"].artifact)
    swapped = Goal(
        corpus.PLANE, corpus.UNRELATED.consequent, corpus.UNRELATED.antecedent
    )
    application = render.refute(swapped, witness)
    status = _lean(
        application.prelude,
        application.statement,
        application.script[0],
        lean_test_project,
    )
    assert status is IndependentStatus.REJECTED


def _certificate_for(artifact: bytes, context) -> Certificate:
    from glitnir_forseti.entailment.evidence import decode_evidence

    evidence = decode_evidence(json.loads(artifact)["evidence"], context, LIMITS)
    assert isinstance(evidence, Certificate)
    return evidence


def _witness(artifact: bytes) -> Witness:
    from glitnir_forseti.entailment.evidence import decode_evidence

    evidence = decode_evidence(json.loads(artifact)["evidence"], corpus.PLANE, LIMITS)
    assert isinstance(evidence, Witness)
    return evidence


def test_the_rendered_goals_are_the_lean_corpus_goals(lean_test_project) -> None:
    """The trusted translation, checked against 017's own Lean definitions.

    Each rendered formula must equal the Lean example's formula, and the Python
    reading of the positive fragment must equal ``Formula.nonstrict?``.
    """
    pairs = {
        "disk": corpus.DISK,
        "quartic": corpus.QUARTIC,
        "box": corpus.BOX,
        "halfDisk": corpus.HALF_DISK,
        "unrelated": corpus.UNRELATED,
    }
    claims = []
    for name, goal in pairs.items():
        n = goal.context.dimension
        for side in ("antecedent", "consequent"):
            formula = getattr(goal, side)
            # `show`, not an ascription: Lean 4.35 finds no `Decidable` for an
            # equation whose side keeps the type `Formula context.dimension`.
            claims.append(
                f"(({render.formula(formula)} : Formula {n}) = "
                f"(show Formula {n} from PredicateCertificates.{name}.{side}))"
            )
            constraints = nonstrict(formula)
            if constraints is not None:
                listed = ", ".join(render.poly(p) for p in constraints)
                claims.append(
                    f"(({render.formula(formula)} : Formula {n}).nonstrict? = "
                    f"(some [{listed}] : Option (List (Polynomial.Expr {n}))))"
                )
        names = ", ".join(f'"{v}"' for v in goal.context.names)
        claims.append(f"(PredicateCertificates.{name}.context.names = [{names}])")
    prelude = (
        "import Gimle.Forseti.Examples.PredicateCertificates\n\n"
        "open Gimle.Forseti Gimle.Forseti.Syntax Gimle.Asgard "
        "Gimle.Forseti.Examples\n"
    )
    status = _lean(
        prelude, ": " + " ∧\n  ".join(claims), "decide +kernel", lean_test_project
    )
    assert status is IndependentStatus.CONFIRMED


def test_negative_and_large_rationals_reach_lean_exactly(lean_test_project) -> None:
    big = Fraction(2**100, 3)
    x = Var(0)
    below = Atom(Relation.LE, Add(x, Const(Fraction(3, 2))))  # x ≤ -3/2
    proven = decide_entailment(
        Goal(corpus.LINE, below, Atom(Relation.LE, Add(x, Neg(Const(big))))),
        project=lean_test_project,
    )
    assert proven.status is Status.PROVEN, proven.lanes
    refuted = decide_entailment(
        Goal(
            corpus.LINE,
            Atom(Relation.GE, Add(x, Const(Fraction(3, 2)))),
            Atom(Relation.GE, Add(x, Neg(Const(big)))),
        ),
        project=lean_test_project,
    )
    assert refuted.status is Status.REFUTED, refuted.lanes


def test_a_timeout_is_unknown_and_leaves_no_child(
    lean_test_project, live_children
) -> None:
    limits = replace(LIMITS, wall_time_ms=2_500)
    result = decide_entailment(corpus.DISK, limits, project=lean_test_project)
    assert result.status is Status.UNKNOWN
    assert result.reason is UnknownReason.TIMEOUT, result.lanes
    assert live_children() == []


def test_cancellation_is_unknown_and_leaves_no_child(
    lean_test_project, live_children
) -> None:
    cancel = threading.Event()
    timer = threading.Timer(2.0, cancel.set)
    timer.start()
    started = time.monotonic()
    result = decide_entailment(corpus.DISK, project=lean_test_project, cancel=cancel)
    timer.cancel()
    assert result.reason is UnknownReason.CANCELLED, result.lanes
    assert time.monotonic() - started < 30
    assert live_children() == []


def test_lean_output_overflow_is_a_resource_limit(
    lean_test_project, live_children
) -> None:
    limits = replace(LIMITS, max_lean_output_bytes=16)
    result = decide_entailment(corpus.HALF_DISK, limits, project=lean_test_project)
    assert result.reason is UnknownReason.RESOURCE_LIMIT, result.lanes
    assert live_children() == []


def test_missing_lean_is_unavailable(tmp_path) -> None:
    result = decide_entailment(corpus.HALF_DISK, project=tmp_path / "absent")
    assert result.reason is UnknownReason.LEAN_UNAVAILABLE, result.lanes


def test_a_redefined_library_is_refused(
    decided, lean_test_project, monkeypatch
) -> None:
    """An environment whose closure differs from the replay pin is not used.

    The artifact's own environment metadata is unchanged, so only the caller's
    independent pin can catch this; the observed digests are altered here to
    stand for a library whose compiled modules were changed.
    """
    original = closure.digest_closure

    def redefined(*args, **kwargs):
        observed = original(*args, **kwargs)
        packages = tuple(
            (name, count, "0" * 64 if name == "forseti" else digest)
            for name, count, digest in observed.packages
        )
        return dataclasses.replace(observed, packages=packages)

    monkeypatch.setattr(closure, "digest_closure", redefined)
    outcome = replay_artifact(
        decided["disk"].artifact, corpus.DISK, LIMITS, lean_test_project
    )
    assert isinstance(outcome, ReplayUnavailable)
    assert "forseti differs" in outcome.reason


def test_the_replay_pin_matches_the_release(lean_test_project) -> None:
    check = closure.verify_closure(render.IMPORTS, SESSION, lean_test_project)
    assert check.status is IndependentStatus.CONFIRMED, check.reason


def test_a_validated_replay_names_the_expected_goal(decided, lean_test_project) -> None:
    outcome = replay_artifact(
        decided["unrelated"].artifact, corpus.UNRELATED, LIMITS, lean_test_project
    )
    assert isinstance(outcome, ValidatedEntailment)
    assert outcome.status == "REFUTED" and outcome.goal == corpus.UNRELATED


def test_the_replay_pin_is_current(lean_test_project) -> None:
    """The committed replay pin is what the tool computes from the release."""
    run = subprocess.run(
        [
            sys.executable,
            "tools/pin_lean_replay.py",
            str(lean_test_project),
            "--check",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert run.returncode == 0, run.stdout + run.stderr


# --- real-algebraic counterexamples (task 033) ------------------------------

_F = Fraction
SQRT_TWO = AlgebraicRoot((_F(-2), _F(0), _F(1)), _F(7, 5), _F(3, 2))
ROOT_TWO_WITNESS = AlgebraicWitness(
    (SQRT_TWO,), (AtomProof("zero", (ONE,)),), (AtomProof("enclosure"),)
)


def _replay_algebraic(goal: Goal, witness: AlgebraicWitness, project):
    """Encode with a consistently regenerated source, then replay."""
    application = render.refute_algebraic(goal, witness)
    artifact = build_artifact(goal, witness, application, LIMITS, {})
    return replay_artifact(artifact, goal, LIMITS, project)


def test_a_handwritten_algebraic_witness_replays(lean_test_project) -> None:
    outcome = _replay_algebraic(corpus.ROOT_TWO, ROOT_TWO_WITNESS, lean_test_project)
    assert isinstance(outcome, ValidatedEntailment), outcome
    assert outcome.status == "REFUTED"


@pytest.mark.parametrize(
    "witness",
    [
        # The interval [3/2, 2] holds no root: no sign change.
        replace(
            ROOT_TWO_WITNESS,
            coordinates=(replace(SQRT_TWO, lower=_F(3, 2), upper=_F(2)),),
        ),
        # x³ − 2x changes sign on [−2, 2] but has three roots there.
        replace(
            ROOT_TWO_WITNESS,
            coordinates=(AlgebraicRoot((_F(0), _F(-2), _F(0), _F(1)), _F(-2), _F(2)),),
        ),
        # x³ is not square-free: it changes sign on [−1, 1], but its root is
        # not simple, and only the derivative check can refuse it.
        replace(
            ROOT_TWO_WITNESS,
            coordinates=(AlgebraicRoot((_F(0), _F(0), _F(0), _F(1)), _F(-1), _F(1)),),
        ),
        # The antecedent is zero at √2, not excluded from zero by an enclosure.
        replace(ROOT_TWO_WITNESS, antecedent=(AtomProof("enclosure"),)),
        # x² − 2 is not 2 · (x² − 2).
        replace(ROOT_TWO_WITNESS, antecedent=(AtomProof("zero", (Const(_F(2)),)),)),
        # The consequent x < 0 holds at −√2: the point does not refute.
        replace(
            ROOT_TWO_WITNESS,
            coordinates=(replace(SQRT_TWO, lower=_F(-3, 2), upper=_F(-7, 5)),),
        ),
    ],
    ids=[
        "no-root",
        "two-roots",
        "not-simple",
        "sign",
        "identity",
        "not-a-counterexample",
    ],
)
def test_lean_rejects_hostile_algebraic_witnesses(witness, lean_test_project) -> None:
    """Each witness is well formed; only Lean's own checks can refuse it."""
    outcome = _replay_algebraic(corpus.ROOT_TWO, witness, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome
    assert "Lean rejected" in outcome.reason


def test_an_algebraic_witness_is_bound_to_its_goal(decided, lean_test_project) -> None:
    artifact = decided["root-two"].artifact
    outcome = replay_artifact(
        artifact, corpus.ROOT_TWO_POSITIVE, LIMITS, lean_test_project
    )
    assert isinstance(outcome, InvalidEvidence)
    assert "different goal" in outcome.reason
    # Regenerated for the changed goal, the √2 witness reaches Lean and fails.
    outcome = _replay_algebraic(
        corpus.ROOT_TWO_POSITIVE, ROOT_TWO_WITNESS, lean_test_project
    )
    assert isinstance(outcome, InvalidEvidence)
    assert "Lean rejected" in outcome.reason


def test_oversized_algebraic_witnesses_never_reach_lean(
    decided, lean_test_project
) -> None:
    document = json.loads(decided["root-two"].artifact)
    root = document["evidence"]["coordinates"][0]
    for field, value in (
        ("coefficients", [[1, 1]] * (LIMITS.max_degree + 2)),
        ("interval", [[2**300, 1], [2**300 + 1, 1]]),
    ):
        changed = json.loads(json.dumps(document))
        changed["evidence"]["coordinates"][0] = {**root, field: value}
        artifact = json.dumps(changed).encode()
        outcome = replay_artifact(artifact, corpus.ROOT_TWO, LIMITS, lean_test_project)
        assert isinstance(outcome, InvalidEvidence)
        assert "malformed" in outcome.reason


# --- constraint products (Positivstellensatz) --------------------------------------

_ONE = Const(Fraction(1))
#: forseti-lean's own fixture: the pair (0, 1) adds −((−x)(−y)) ≤ 0, and
#: x·y = 1 · (−x)(−y) is the whole identity.
PRODUCT_NONNEG = PsatzCertificate(
    ((0, 1),),
    (
        Row(
            SquareSum(()),
            (SquareSum(()), SquareSum(()), SquareSum(((Fraction(1), _ONE),))),
        ),
    ),
)


def _replay_psatz(goal, certificate, project):
    artifact = build_artifact(
        goal, certificate, render.application(goal, certificate), LIMITS, {}
    )
    return replay_artifact(artifact, goal, LIMITS, project)


def test_a_handwritten_psatz_certificate_replays(lean_test_project) -> None:
    outcome = _replay_psatz(corpus.PRODUCT_NONNEG, PRODUCT_NONNEG, lean_test_project)
    assert isinstance(outcome, ValidatedEntailment), outcome
    assert outcome.status == "PROVEN"


@pytest.mark.parametrize(
    "certificate",
    [
        # 2 · (−x)(−y) is not x·y.
        replace(
            PRODUCT_NONNEG,
            rows=(
                Row(
                    SquareSum(()),
                    (SquareSum(()), SquareSum(()), SquareSum(((Fraction(2), _ONE),))),
                ),
            ),
        ),
        # The pair (0, 0) adds −x², not the product the identity needs.
        replace(PRODUCT_NONNEG, pairs=((0, 0),)),
    ],
    ids=["weight", "pair"],
)
def test_lean_rejects_hostile_psatz_certificates(
    certificate, lean_test_project
) -> None:
    """Each certificate is well formed; only Lean's own check can refuse it."""
    outcome = _replay_psatz(corpus.PRODUCT_NONNEG, certificate, lean_test_project)
    assert isinstance(outcome, InvalidEvidence), outcome
    assert "Lean rejected" in outcome.reason
