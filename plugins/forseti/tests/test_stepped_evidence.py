"""Tests for the ``sample`` evidence directive of the stepped lane.

``sample`` reads a registered stepped model's run as an exact plan and has
Lean print the model's field, initial state, step and runtime coordinates for
the sampler. Evidence is never a proof: nothing here touches a decision.
"""

from fractions import Fraction

import pytest

from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import (
    MAX_STEPS,
    MAX_WORK,
    NOT_PLAIN,
    EvidenceError,
    SamplePlan,
    emission_command,
    read_directive,
    sample_plan,
)
from glitnir_forseti.stepped.models import HEDGED_SPREAD_STEPPED

HEDGED = "Gimle.Forseti.Examples.HedgedSpread.compiled"
REQUIRED = "(law := gaussian) (variance := 64) (observe := s)"


def _sample(options: str, model: str = HEDGED) -> evidence.Simulation:
    return read_directive("sample", model, options)


# ----- the directive ----------------------------------------------------------------


def test_sample_directive_parses_with_defaults() -> None:
    simulation = _sample(REQUIRED)
    assert (simulation.error, simulation.verb, simulation.model) == (
        "",
        "sample",
        HEDGED,
    )
    assert simulation.sample == SamplePlan(
        law="gaussian",
        variance=Fraction(64),
        paths=4000,
        steps=256,
        seed=7,
        show=8,
        observe="s",
        barrier=Fraction(1),
    )


def test_a_sample_directive_is_read_from_a_notebook() -> None:
    """Glitnir's core finds the line and hands its parts to `read_directive`."""
    from glitnir.notebook.evidence_cards import evidence_directives

    text = f"def u := 1\n-- @evidence sample {HEDGED} {REQUIRED} (paths := 10)\n"
    cell = {"index": 0, "kind": "source", "start": 1, "end": 3, "text": text}
    [found] = evidence_directives([cell], text)
    simulation = read_directive(found["verb"], found["target"], found["options"])
    assert simulation.verb == "sample" and simulation.error == ""
    assert simulation.sample is not None and simulation.sample.paths == 10


def test_every_sample_option_is_read_exactly() -> None:
    simulation = _sample(
        "(law := twopoint) (variance := 1/3) (paths := 12) (steps := 40) "
        "(seed := 0) (show := 12) (observe := h) (barrier := 3/2)"
    )
    assert simulation.error == ""
    assert simulation.sample == SamplePlan(
        "twopoint", Fraction(1, 3), 12, 40, 0, 12, "h", Fraction(3, 2)
    )


@pytest.mark.parametrize(
    "text",
    [
        "(law := cauchy) (variance := 64) (observe := s)",
        "(law := gaussian) (observe := s)",
        "(law := gaussian) (variance := 64) (observe := s) "
        "(paths := 100000) (steps := 100000)",
        "(law := threepoint) (variance := 70000) (observe := s)",
    ],
)
def test_sample_directive_refusals(text: str) -> None:
    simulation = _sample(text)
    assert simulation.error and simulation.sample is None
    assert simulation.verb == "sample"


@pytest.mark.parametrize(
    ("options", "error"),
    [
        (
            "(law := cauchy) (variance := 64) (observe := s)",
            "unknown law 'cauchy'; use gaussian, twopoint or threepoint",
        ),
        ("(variance := 64) (observe := s)", "sample needs a (law := …)"),
        ("(law := gaussian) (observe := s)", "sample needs a (variance := …)"),
        ("(law := gaussian) (variance := 64)", "sample needs a (observe := …)"),
        (f"{REQUIRED} (h := 1/100)", "unknown option 'h'"),
        (f"{REQUIRED} (steps := 0)", f"steps must be 1–{MAX_STEPS}"),
        (f"{REQUIRED} (steps := 100001)", f"steps must be 1–{MAX_STEPS}"),
        (f"{REQUIRED} (paths := 0)", "paths must be at least 1"),
        (
            f"{REQUIRED} (paths := 100001) (steps := 200)",
            f"this run would take 20,000,200 field-term evaluations, more than the "
            f"{MAX_WORK:,} allowed",
        ),
        (f"{REQUIRED} (show := 0)", "show must be 1–15"),
        (f"{REQUIRED} (show := 16)", "show must be 1–15"),
        (f"{REQUIRED} (paths := 4) (show := 5)", "show must be 1–4"),
        (f"{REQUIRED} (seed := -1)", "the seed must not be negative"),
        (f"{REQUIRED} (barrier := 0)", "the barrier must be positive"),
        (f"{REQUIRED} (barrier := -1)", "the barrier must be positive"),
        (
            "(law := gaussian) (variance := -1) (observe := s)",
            "the variance must not be negative",
        ),
        (f"{REQUIRED} (paths := 1/2)", "write the variance and barrier as numbers"),
        ("(law := gaussian) (variance := 1e3) (observe := s)", NOT_PLAIN),
        ("(law := gaussian) (variance := 64) (observe := 2s)", "observe names a state"),
        (
            "(law := threepoint) (variance := 66050) (observe := s)",
            "no three-point law",
        ),
    ],
)
def test_each_refusal_says_why(options: str, error: str) -> None:
    simulation = _sample(options)
    assert error in simulation.error
    assert (simulation.verb, simulation.model, simulation.sample) == (
        "sample",
        HEDGED,
        None,
    )


def test_the_work_and_three_point_limits_are_inclusive() -> None:
    at_work = _sample(f"{REQUIRED} (paths := 100000) (steps := 200)")
    assert at_work.error == "" and at_work.sample is not None
    # ε = v / (2·257²) is exactly 1/2 at v = 257²; the step 1/256 is the registry's.
    at_half = _sample("(law := threepoint) (variance := 66049) (observe := s)")
    assert at_half.error == ""
    assert HEDGED_SPREAD_STEPPED.delta == Fraction(1, 256)


def test_the_three_point_refusal_reads_the_directives_barrier() -> None:
    # A wider band moves the jump to ±513, so the same variance has a law.
    narrow = _sample("(law := threepoint) (variance := 70000) (observe := s)")
    assert "variance 70000 at reach 257" in narrow.error
    wide = _sample(
        "(law := threepoint) (variance := 70000) (observe := s) (barrier := 2)"
    )
    assert wide.error == ""


def test_a_sample_names_a_registered_stepped_model() -> None:
    simulation = _sample(REQUIRED, model="hedgedStepped")
    assert "'hedgedStepped' is not a registered stepped model" in simulation.error
    assert HEDGED in simulation.error


def test_the_plan_counts_the_fields_terms_in_its_work() -> None:
    options = {"law": "gaussian", "variance": "64", "observe": "s"}
    assert sample_plan(options, terms=19).paths == 4000
    with pytest.raises(EvidenceError, match="20,480,000 field-term evaluations"):
        sample_plan(options, terms=20)


def test_the_plan_is_exact() -> None:
    plan = sample_plan(
        {"law": "gaussian", "variance": "0.1", "observe": "s", "barrier": "1/3"}
    )
    assert plan.variance == Fraction(1, 10) and type(plan.variance) is Fraction
    assert plan.barrier == Fraction(1, 3) and type(plan.barrier) is Fraction


def test_an_unknown_verb_lists_all_three() -> None:
    simulation = read_directive("fly", HEDGED, REQUIRED)
    assert "use simulate, iterate or sample" in simulation.error


# ----- the emission -----------------------------------------------------------------


def test_sample_emission_command_names_the_model() -> None:
    cmd = emission_command("sample", HEDGED)
    assert "forsetiEvidence:" in cmd and "compiled" in cmd
    assert cmd.endswith(
        '#eval IO.println ("forsetiEvidence:" ++ (_root_.forsetiEvidenceJson '
        f"{HEDGED} {HEDGED_SPREAD_STEPPED.delta_name}).compress)"
    )


def test_the_stepped_template_prints_a_driven_model() -> None:
    cmd = emission_command("sample", HEDGED)
    assert "(p : Gimle.Asgard.Model.DrivenModel b e ds)" in cmd
    assert "ContinuousModel" not in cmd
    for key in ("states", "initial", "step", "coordinates", "driver", "field"):
        assert f'("{key}",' in cmd
    assert '("observations"' not in cmd and '("start"' not in cmd
    # The field trees are printed by `_EMIT`'s own expression printer.
    model = "\n\ndef _root_.forsetiEvidenceJson"
    printer = emission_command("simulate", "m").split(model)[0]
    assert cmd.startswith(printer + model)
    assert "_root_.forsetiExprJson (p.rates.expressions i)" in cmd
    # The step and the driver's place come from Lean, not from Python.
    assert "1/256" not in cmd and '"driver", Lean.toJson 0' not in cmd


def test_the_sample_emission_refuses_an_unregistered_model() -> None:
    with pytest.raises(EvidenceError, match="not a registered stepped model"):
        emission_command("sample", "hedgedStepped")
    with pytest.raises(EvidenceError, match="is not a Lean name"):
        emission_command("sample", f"{HEDGED}) (x")
