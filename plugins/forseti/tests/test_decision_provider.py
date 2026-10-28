"""Tests for the Forseti decision provider.

The deciders run for real, with Z3 (installed with the ``z3`` extra) and
without any Lean: every block the provider proposes goes through Glitnir's own
outcome validation here, and only the core's project check, exercised by the
live test beside this file, makes one a proof.
"""

import re
import threading
import time
from pathlib import Path

import pytest
from glitnir.providers import DecisionProvider, DecisionRequest, load_providers
from glitnir.providers.outcomes import checked_decision

from glitnir_forseti import decision_provider as forseti
from glitnir_forseti import providers
from glitnir_forseti.claims.decide import examples
from glitnir_forseti.claims.text import render_goal
from glitnir_forseti.trajectory.examples import EXAMPLES

pytest.importorskip("z3", reason="deciding needs the z3 extra")

DISK = "entailment x, y : x^2 + y^2 <= 1 |- x^2 <= 1 and y^2 <= 1"
SHIFTED = "entailment x : x >= 1 |- x >= 2"
ROOT_TWO = "entailment x : x*x = 2 |- x < 0"
#: True, but outside both lanes' reach: neither a counterexample nor a
#: certificate over the dictionary exists.
CUBE = "entailment x : x*x*x >= 0 |- x >= 0"
STEPPED = (
    "stepped hedged-spread from (0, 0) at 0 step 1/256 shocks variance 64"
    " within 256 observe s breach 1 probability <= 1/4"
)
HEDGING = (
    "hedging call strike 100 spot 100 band down 1/50 up 1/50 steps 20"
    " premium 3555/1000 covers"
)
TRAJECTORIES = {example.name: example.goal for example in EXAMPLES}


def _decided(name: str, goal: str, cancel: threading.Event | None = None) -> dict:
    request = forseti.parse(name, goal.split()[0], goal)
    outcome = forseti.decide(request, cancel or threading.Event())
    # Glitnir refuses anything else; the provider's output is exactly valid.
    assert checked_decision(outcome) == {"block": None, "theorem": None, **outcome}
    return outcome


# ----- the provider -----------------------------------------------------------------


def test_the_provider_decides_five_kinds_of_goal() -> None:
    provider = forseti.decision_provider()
    assert isinstance(provider, DecisionProvider) and provider.name == "forseti"
    assert provider.kinds == {
        "entailment",
        "trajectory",
        "stream",
        "stepped",
        "hedging",
    }
    # A block's imports depend on its goal; each outcome lists them instead.
    assert provider.imports == ()
    assert provider in providers()


def test_glitnir_finds_it_through_the_entry_point() -> None:
    found = load_providers()
    assert "forseti" not in found.errors
    assert {found.decisions[kind].name for kind in forseti.KINDS} == {"forseti"}


def test_it_is_available_with_z3_and_names_the_extra_without(monkeypatch) -> None:
    assert forseti.unavailable() is None
    real = forseti.importlib.util.find_spec

    def without_z3(name: str, *args):
        return None if name == "z3" else real(name, *args)

    monkeypatch.setattr(forseti.importlib.util, "find_spec", without_z3)
    assert forseti.unavailable() == "Install glitnir-forseti[z3] to decide goals."


# ----- parse --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        DISK,
        "trajectory three-state from (1, 2, -1) at 2 observe V always : V <= 6",
        "stream formal-heat interpretation evaluated-field basis ogf profile x*x"
        " field u over t in [0, 1], x in [-1, 1] : u >= 0",
        STEPPED,
        HEDGING,
    ],
    ids=["entailment", "trajectory", "stream", "stepped", "hedging"],
)
def test_parse_reads_each_kind_of_goal(goal: str) -> None:
    kind = goal.split()[0]
    assert forseti.parse("claim", kind, goal) == DecisionRequest(
        "claim", kind, goal, {}
    )


@pytest.mark.parametrize(
    ("kind", "goal", "message"),
    [
        ("entailment", "entailment x : y = 0 |- true", "unknown coordinate"),
        ("entailment", "entailment x : x = |- true", "Unreadable goal"),
        (
            "trajectory",
            "trajectory nowhere from (1) at 0 observe V always : V <= 6",
            "nowhere",
        ),
        (
            "trajectory",
            "trajectory three-state from (1, 2, -1) at 2 observe W always : W <= 6",
            "W",
        ),
        ("stream", "stream nowhere interpretation formal-stream", "Unreadable goal"),
        ("sum", "sum 2 + 2 = 4", "Forseti decides only"),
    ],
)
def test_parse_refuses_goals_it_cannot_decide(
    kind: str, goal: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        forseti.parse("claim", kind, goal)


def test_parse_refuses_a_goal_of_another_kind() -> None:
    with pytest.raises(ValueError, match="reads as an entailment goal, not trajectory"):
        forseti.parse("claim", "trajectory", DISK)


#: A Lean declaration in a block template: in a string, at its start or a line's.
_TEMPLATE_DECLARATION = re.compile(
    r"(?:[\"']|\\n)(?:noncomputable )?(?:def|abbrev|theorem|lemma) ([A-Za-z_{][\w{}']*)"
)


#: The modules whose Lean a decided block is made of.
_BLOCK_TEMPLATES = (
    "entailment/render.py",
    "streams/render.py",
    "streams/mild_render.py",
    "trajectory/render.py",
    "stepped/render.py",
    "hedging/render.py",
    "notebook/readable.py",
    "notebook/decisions.py",
)


def _block_definitions() -> set[str]:
    """The names the block templates declare, other than the claim's theorem."""
    package = Path(forseti.__file__).parent
    found = set()
    for module in _BLOCK_TEMPLATES:
        text = (package / module).read_text()
        found.update(_TEMPLATE_DECLARATION.findall(text))
    return {name for name in found if not name.startswith("{")}


def test_every_name_a_block_defines_is_reserved() -> None:
    defined = _block_definitions()
    assert {
        "forsetiRoot",
        "forsetiContract",
        "forsetiAntecedent",
        "forsetiClaim",
    } <= defined
    reserved = forseti.RESERVED_PREFIX
    assert all(name.startswith(reserved) for name in defined), defined
    for name in sorted(defined) + [reserved, reserved + "Driver3"]:
        with pytest.raises(ValueError, match=f"`{name}` is reserved"):
            forseti.parse(name, "entailment", DISK)
    assert forseti.parse("forset", "entailment", DISK).name == "forset"


# ----- decide -------------------------------------------------------------------------


def test_a_proven_entailment_proposes_the_named_theorem() -> None:
    outcome = _decided("disk", DISK)
    assert outcome["status"] == "proven" and outcome["theorem"] == "disk"
    block = outcome["block"]
    assert block.startswith("namespace Decided_disk\n\ndef forsetiContext ")
    assert block.endswith("\n\nend Decided_disk")
    assert (
        "theorem disk :\n    ∀ x y : ℝ, x * x + y * y ≤ 1 → x * x ≤ 1 ∧ y * y ≤ 1"
        " := by\n  have root : " in block
    )
    assert "sorry" not in block and "import" not in block
    assert outcome["stages"][-2][:2] == ["certificate", "found"]
    assert outcome["stages"][-1] == [
        "imports",
        "needed",
        "import Gimle.Forseti.Syntax.Certificate",
    ]
    assert outcome["reason"] == "Found by the certificate stage."


def test_a_refuted_entailment_proposes_its_negation() -> None:
    outcome = _decided("shifted", SHIFTED)
    assert outcome["status"] == "refuted"
    assert outcome["theorem"] == "shifted_refuted"
    block = outcome["block"]
    assert (
        "theorem shifted_refuted : ¬ (∀ x : ℝ, x ≥ 1 → x ≥ 2) := by\n"
        "  have refuted : ¬ Gimle.Forseti.Syntax.FormulaEntailment" in block
    )
    # Only the refutation is declared; the false claim itself is not.
    assert "theorem shifted " not in block and "sorry" not in block
    assert outcome["stages"][0][:2] == ["refutation", "found"]


def test_an_irrational_counterexample_needs_the_algebraic_checker() -> None:
    outcome = _decided("root_two", ROOT_TWO)
    assert outcome["theorem"] == "root_two_refuted"
    assert "algebraicRefutes_sound" in outcome["block"]
    assert outcome["stages"][-1][2] == "import Gimle.Forseti.Syntax.AlgebraicWitness"


@pytest.mark.parametrize("example", examples(), ids=lambda e: e["model"] or e["kind"])
def test_every_example_goal_proposes_a_valid_block(example: dict) -> None:
    outcome = _decided("example", example["goal"])
    expected = {"proven": "example", "refuted": "example_refuted"}
    assert outcome["theorem"] == expected[outcome["status"]]
    assert outcome["block"].startswith("namespace Decided_example\n")
    assert f"theorem {outcome['theorem']} " in outcome["block"]


def test_a_refuted_trajectory_proposes_its_negation() -> None:
    outcome = _decided("below", render_goal(TRAJECTORIES["energy-below-five"]))
    assert (outcome["status"], outcome["theorem"]) == ("refuted", "below_refuted")
    assert "theorem below_refuted : ¬ (" in outcome["block"]
    assert outcome["stages"][-1] == [
        "imports",
        "needed",
        "import Gimle.Forseti.Syntax.Certificate\n"
        "import Gimle.Forseti.Examples.ThreeStateContract",
    ]


def test_a_goal_neither_lane_decides_is_unknown() -> None:
    outcome = _decided("cube", CUBE)
    assert outcome["status"] == "unknown"
    assert outcome["reason"] == "dictionary-exhausted"
    assert "block" not in outcome and "theorem" not in outcome
    assert [stage[:2] for stage in outcome["stages"]] == [
        ["refutation", "none"],
        ["certificate", "none"],
    ]


def test_a_stepped_goal_proposes_its_block_without_z3_or_lean() -> None:
    outcome = _decided("year", STEPPED)
    assert (outcome["status"], outcome["theorem"]) == ("proven", "year")
    assert outcome["block"].startswith("namespace Decided_year\n\nopen MeasureTheory\n")
    assert "def forsetiClaim : Prop :=" in outcome["block"]
    assert outcome["stages"][-2][:2] == ["horizon", "found"]
    assert outcome["stages"][-1] == [
        "imports",
        "needed",
        "import Gimle.Forseti.Stepped\n"
        "import Gimle.Forseti.Examples.HedgedSpreadStepped",
    ]
    assert outcome["reason"] == "Found by the horizon stage."
    refuted = _decided("tight", STEPPED.replace("1/4", "1/10"))
    assert (refuted["status"], refuted["theorem"]) == ("refuted", "tight_refuted")


def test_a_hedging_goal_proposes_its_block_without_z3_or_lean() -> None:
    outcome = _decided("held", HEDGING)
    assert (outcome["status"], outcome["theorem"]) == ("proven", "held")
    assert outcome["block"].startswith(
        "namespace Decided_held\n\nopen Gimle.Forseti.Hedging\n"
    )
    assert "def forsetiClaim : Prop :=" in outcome["block"]
    assert outcome["stages"][-2][:2] == ["price", "found"]
    assert outcome["stages"][-1] == [
        "imports",
        "needed",
        "import Gimle.Forseti.Hedging",
    ]
    assert outcome["reason"] == "Found by the price stage."
    refuted = _decided("low", HEDGING.replace("3555/1000", "7/2"))
    assert (refuted["status"], refuted["theorem"]) == ("refuted", "low_refuted")
    assert "theorem low_refuted : ¬ (∃ δ : ℕ → ℝ → ℝ," in refuted["block"]
    gap = _decided(
        "gap",
        HEDGING.replace("band down 1/50 up 1/50", "band down 1/100 up 1/100").replace(
            "premium 3555/1000 covers", "against down 1/50 up 1/50 shortfall <= 5"
        ),
    )
    assert (gap["status"], gap["reason"]) == ("unknown", "gap")
    assert "block" not in gap


def test_an_undecided_stepped_goal_is_unknown_with_its_numbers() -> None:
    outcome = _decided("gap", STEPPED.replace("1/4", "22/100"))
    assert (outcome["status"], outcome["reason"]) == ("unknown", "gap")
    assert "block" not in outcome
    [_, said, detail] = outcome["stages"][-1]
    assert said == "none" and "1/4" in detail and "0.1988" in detail
    refused = _decided("sub", STEPPED.replace("variance", "subgaussian"))
    assert (refused["status"], refused["reason"]) == ("unknown", "unsupported")


def test_an_unsupported_trajectory_is_unknown_never_refuted() -> None:
    outcome = _decided("noisy", render_goal(TRAJECTORIES["stochastic"]))
    assert (outcome["status"], outcome["reason"]) == ("unknown", "unsupported")
    assert outcome["stages"][0][:2] == ["binding", "unsupported"]


def test_deciding_never_replays_in_the_pinned_release(monkeypatch) -> None:
    from glitnir_forseti.entailment import service as entailment
    from glitnir_forseti.streams import service as streams
    from glitnir_forseti.trajectory import service as trajectory

    def refused(*args, **kwargs):
        raise AssertionError("a decision replayed or built an artifact")

    for module, names in (
        (entailment, ("replay_artifact", "build_artifact")),
        (trajectory, ("replay_root", "build_artifact")),
        (streams, ("replay_stream", "build_artifact")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, refused)
    for example in examples():
        assert _decided("example", example["goal"])["status"] != "unknown"


def test_a_stream_misfit_is_not_called_a_lean_check(monkeypatch) -> None:
    """Without a replay no Lean runs: evidence that does not fit says only that."""
    from glitnir_forseti.streams import service as streams

    def misfit(*args, **kwargs):
        raise ValueError("the bound is off")

    monkeypatch.setattr(streams, "application", misfit)
    [goal] = [e["goal"] for e in examples() if e["goal"].startswith("stream")][:1]
    outcome = _decided("misfit", goal)
    unfit = [stage for stage in outcome["stages"] if "the bound is off" in stage[2]]
    assert unfit
    for name, said, detail in unfit:
        assert said == "does not fit the claim"
        assert detail == "the evidence does not fit the claim: the bound is off"
    assert not any("Lean" in text for stage in outcome["stages"] for text in stage)


def test_a_cancelled_decision_is_unknown_at_once() -> None:
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    outcome = _decided("disk", DISK, cancel)
    assert (outcome["status"], outcome["reason"]) == ("unknown", "cancelled")
    assert time.monotonic() - started < 5


def test_cancelling_stops_the_running_solver(
    tmp_path, monkeypatch, live_children
) -> None:
    from glitnir_forseti.entailment import solver

    # A solver worker that would run for half a minute.
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(solver.sys, "executable", str(fake))
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    outcome = _decided("disk", DISK, cancel)
    assert (outcome["status"], outcome["reason"]) == ("unknown", "cancelled")
    assert time.monotonic() - started < 10
    assert live_children() == []


def test_a_goal_that_no_longer_reads_is_an_error() -> None:
    request = DecisionRequest("claim", "entailment", "entailment x : y = 0 |- true", {})
    with pytest.raises(ValueError, match="unknown coordinate"):
        forseti.decide(request, threading.Event())
