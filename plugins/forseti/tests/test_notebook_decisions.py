"""The Lean blocks that decided goals become.

The decisions carry real artifacts and regenerated Lean, so the blocks are the
ones the decision provider proposes; Glitnir's project check of such blocks
is exercised by ``test_decision_provider_lean.py``.
"""

import dataclasses
from fractions import Fraction

import pytest
from glitnir.notebook.document import parse_notebook
from glitnir.providers.outcomes import checked_decision

from glitnir_forseti.claims.decide import Decision, LeanApplication
from glitnir_forseti.claims.text import parse_goal
from glitnir_forseti.entailment.evidence import Certificate, Row, SquareSum
from glitnir_forseti.entailment.limits import EntailmentLimits
from glitnir_forseti.entailment.syntax import Add, And, Atom, Const, Neg, Relation, Var
from glitnir_forseti.notebook.decisions import (
    Block,
    DecisionError,
    block,
    namespace,
)
from glitnir_forseti.notebook.readable import readable
from glitnir_forseti.trajectory import models as trajectory_models
from glitnir_forseti.trajectory import render as trajectory_render
from glitnir_forseti.trajectory import replay
from glitnir_forseti.trajectory.examples import EXAMPLES, energy_goal

GOALS = {example.name: example.goal for example in EXAMPLES}
# The band as a person writes it: `V >= 0 and V <= 6`.
GOALS["energy-band"] = energy_goal(
    And(
        Atom(Relation.GE, Var(0)),
        Atom(Relation.LE, Add(Var(0), Neg(Const(Fraction(6))))),
    )
)
LIMITS = EntailmentLimits()
MOVED = (
    ("initial-x", Fraction(1)),
    ("initial-y", Fraction(2)),
    ("initial-z", Fraction(1)),
)
ONE = Const(Fraction(1))
BAND_CERTIFICATE = Certificate(
    (
        Row(SquareSum(()), (SquareSum(((Fraction(1), ONE),)), SquareSum(()))),
        Row(SquareSum(()), (SquareSum(()), SquareSum(((Fraction(1), ONE),)))),
    )
)


def _decision(goal, status: str) -> Decision:
    """A decision with a genuine artifact and the Lean it regenerates."""
    if status == "UNKNOWN":
        return Decision("trajectory", goal, "UNKNOWN", "exhausted")
    evidence = (
        BAND_CERTIFICATE
        if status == "PROVEN"
        else replay.InitialWitness(goal.start, goal.initial, Fraction(6))
    )
    artifact = replay.build_artifact(goal, evidence, LIMITS, {"stage": "test"})
    claimed, _, unit = replay.regenerate_application(artifact, goal, LIMITS)
    assert claimed == status
    return Decision(
        "trajectory",
        goal,
        status,
        axioms=("propext",),
        artifact=artifact,
        lean=LeanApplication(unit.prelude, unit.statement, tuple(unit.script)),
    )


def _valid(proposed: Block, status: str) -> Block:
    """Glitnir accepts the block exactly as it is, under the theorem it names."""
    outcome = {"status": status, "block": proposed.source, "theorem": proposed.theorem}
    assert checked_decision(outcome)["block"] == proposed.source
    return proposed


def _script(claim) -> tuple[str, ...]:
    """The claim's non-blank proof lines, dedented, as the block writes them."""
    return tuple(line for line in claim.proof.split("\n") if line.strip())


def test_a_proven_goal_becomes_a_readable_theorem_proved_from_the_root() -> None:
    decision = _decision(GOALS["energy-band"], "PROVEN")
    proposed = _valid(block("energy_band", decision), "proven")
    stated = readable(decision.goal)
    assert stated is not None
    assert proposed.theorem == "energy_band"
    assert "Gimle.Forseti.Examples.ThreeStateContract" in proposed.imports
    (claim,) = parse_notebook(proposed.source).claims
    assert claim.name == "energy_band"
    # The claim is the property, not the decider's root definition.
    assert claim.statement == stated.statement
    assert "forsetiRoot" not in claim.statement
    assert "have V := y t 3; V ≥ 0 ∧ V ≤ 6" in claim.statement
    # Its proof proves the root with the decider's own script, then bridges.
    assert _script(claim) == (
        "have root : forsetiRoot := by",
        *("  " + line for line in decision.lean.script),
        *stated.from_root,
    )
    assert "Goal (trajectory): trajectory three-state" in claim.docstring
    assert "The trajectory decider proved it." in claim.docstring
    assert "V is output 3 of the compiled circuit, the port obs-v" in claim.docstring
    assert "checked" not in claim.docstring
    assert proposed.source.startswith(f"namespace {namespace('energy_band')}\n")
    assert proposed.source.endswith(f"end {namespace('energy_band')}")
    assert "import" not in proposed.source


def test_a_refuted_goal_becomes_the_theorem_of_its_negation() -> None:
    decision = _decision(GOALS["energy-below-five"], "REFUTED")
    proposed = _valid(block("energy_below_five", decision), "refuted")
    stated = readable(decision.goal)
    assert stated is not None
    assert proposed.theorem == "energy_below_five_refuted"
    (claim,) = parse_notebook(proposed.source).claims
    assert claim.name == "energy_below_five_refuted" and claim.proof.strip() != "sorry"
    negated = stated.statement.removeprefix(":").strip()
    assert claim.statement == f": ¬ ({negated})"
    assert "have V := y t 3; V ≤ 5" in claim.statement
    lines = len(decision.lean.script)
    assert _script(claim)[0] == (
        "have refuted : forsetiIdentities ∧ ¬ forsetiContract := by"
    )
    assert _script(claim)[1 : lines + 1] == tuple(
        "  " + line for line in decision.lean.script
    )
    assert _script(claim)[lines + 1 :] == ("intro claim", *stated.to_root)
    assert "The trajectory decider refuted it." in claim.docstring
    # The false claim itself is not declared, so nothing is left `sorry`.
    assert "theorem energy_below_five " not in proposed.source
    assert "sorry" not in proposed.source


def test_an_entailment_claim_is_stated_over_the_reals() -> None:
    goal = parse_goal("entailment x : x*x = 2 |- x < 0")
    decision = Decision(
        "entailment",
        goal,
        "REFUTED",
        lean=LeanApplication(
            "import Gimle.Forseti.Syntax.AlgebraicWitness\n\ndef forsetiGoal := 1",
            ": ¬ Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent"
            " forsetiConsequent",
            ("exact proof",),
        ),
    )
    proposed = _valid(block("root_two", decision), "refuted")
    assert proposed.imports == ("Gimle.Forseti.Syntax.AlgebraicWitness",)
    (claim,) = parse_notebook(proposed.source).claims
    assert claim.statement == ": ¬ (∀ x : ℝ, x * x = 2 → x < 0)"
    assert _script(claim)[:3] == (
        "have refuted : ¬ Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent"
        " forsetiConsequent := by",
        "  exact proof",
        "intro claim",
    )
    assert (
        "refine (fun (bridge : _ ↔ Gimle.Forseti.Syntax.FormulaEntailment"
        " forsetiAntecedent forsetiConsequent) => refuted (bridge.mp claim)) ?_"
        in _script(claim)
    )


def _coefficient_refutation() -> Decision:
    goal = parse_goal(
        "stream formal-heat interpretation formal-stream basis ogf profile x^2"
        " coefficients u(1, 0), u(0, 2), u_xx(0, 0) : c0 >= 0"
    )
    return Decision(
        "stream",
        goal,
        "REFUTED",
        lean=LeanApplication(
            "", ": forsetiIdentities ∧ ¬ forsetiContract", ("exact proof",)
        ),
    )


def test_a_refuted_coefficient_claim_is_readable_and_bridged() -> None:
    """Coefficient claims read as the coefficients, not the root."""
    proposed = _valid(block("coefficient", _coefficient_refutation()), "refuted")
    (claim,) = parse_notebook(proposed.source).claims
    assert claim.statement.startswith(": ¬ (")
    assert "forsetiRoot" not in claim.statement
    assert "have c0 : ℝ := y 1" in claim.statement
    assert _script(claim)[0] == (
        "have refuted : forsetiIdentities ∧ ¬ forsetiContract := by"
    )
    assert (
        "refine (fun (bridge : _ ↔ forsetiContract) => refuted.2 (bridge.mp claim))"
        " ?_" in _script(claim)
    )


def test_a_goal_without_a_readable_form_keeps_the_root(monkeypatch) -> None:
    """The lane's own statement stays the claim for a goal of no known form."""
    from glitnir_forseti.notebook import decisions

    monkeypatch.setattr(decisions, "readable", lambda goal: None)
    proposed = _valid(block("coefficient", _coefficient_refutation()), "refuted")
    (claim,) = parse_notebook(proposed.source).claims
    assert claim.name == "coefficient_refuted"
    assert claim.statement == ": ¬ (forsetiRoot)"
    assert _script(claim)[-1].startswith("exact fun root => refuted.2")
    assert "Stated as the decider states it." in claim.docstring


def _rendered(goal, status: str = "PROVEN") -> Decision:
    """A decision whose Lean is the lane's own prelude for ``goal``."""
    entry = trajectory_models.MODELS[goal.model]
    band = entry.observations[goal.observation]
    statement = (
        ": forsetiRoot"
        if status == "PROVEN"
        else ": forsetiIdentities ∧ ¬ forsetiContract"
    )
    return Decision(
        "trajectory",
        goal,
        status,
        lean=LeanApplication(
            trajectory_render.prelude(goal, entry, band), statement, ("sorry",)
        ),
    )


def test_a_block_carries_every_definition_it_uses() -> None:
    """Claims about one start each define what they use; nothing is shared."""
    band = block("energy_band", _decision(GOALS["energy-band"], "PROVEN"))
    below = block("energy_below_five", _decision(GOALS["energy-below-five"], "REFUTED"))
    lane = _decision(GOALS["energy-band"], "PROVEN").lean.prelude
    chunks = [chunk.strip() for chunk in lane.split("\n\n") if chunk.startswith("def ")]
    assert chunks
    for chunk in chunks:
        assert band.source.count(chunk) == 1
    for proposed in (band, below):
        for name in ("Time", "Initial", "Observation", "Identities", "Root"):
            assert proposed.source.count(f"def forseti{name} ") == 1
        assert "DecidedShared" not in proposed.source
        assert "\nopen " not in proposed.source


def test_a_block_is_trimmed_for_insertion() -> None:
    """Editors trim trailing blanks on save, so the block has none to lose."""
    decision = dataclasses.replace(
        _rendered(GOALS["energy-band"]),
        lean=LeanApplication(
            "import Gimle.Forseti.Syntax.Certificate\n\n"
            "def forsetiRoot : Prop := True  ",
            ": forsetiRoot",
            ("trivial   ",),
        ),
    )
    proposed = _valid(block("trimmed", decision), "proven")
    assert all(line == line.rstrip() for line in proposed.source.split("\n"))
    assert not proposed.source.endswith("\n")


def test_undecided_or_unfamiliar_decisions_make_no_block() -> None:
    with pytest.raises(DecisionError, match="only a decided goal"):
        block("x", _decision(GOALS["energy-at-least-one"], "UNKNOWN"))
    odd = Decision(
        "trajectory",
        GOALS["energy-band"],
        "REFUTED",
        lean=LeanApplication("", ": something else", ("rfl",)),
    )
    with pytest.raises(DecisionError, match="no claim shape"):
        block("x", odd)
    # An entailment's Lean under a trajectory goal is not bridged either.
    crossed = Decision(
        "trajectory",
        GOALS["energy-band"],
        "PROVEN",
        lean=LeanApplication(
            "",
            ": Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent"
            " forsetiConsequent",
            ("rfl",),
        ),
    )
    with pytest.raises(DecisionError, match="no claim shape"):
        block("x", crossed)
