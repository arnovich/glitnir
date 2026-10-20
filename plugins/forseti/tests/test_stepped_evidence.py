"""Tests for the ``sample`` evidence directive of the stepped lane.

``sample`` reads a registered stepped model's run as an exact plan and has
Lean print the model's field, initial state, step and runtime coordinates for
the sampler, which draws seeded shock paths in float64 and shows a few of them
with the running breach fraction on a ``glitnir.evidence/v1`` card. Evidence is
never a proof: nothing here touches a decision.
"""

import ast
import dataclasses
import hashlib
import json
import math
import random
import re
import statistics
import tracemalloc
from fractions import Fraction
from pathlib import Path

import pytest
from glitnir.notebook.evidence_cards import (
    MAX_PAIRS,
    MAX_SAMPLES,
    MAX_SERIES,
    MAX_TEXT,
    checked_evidence,
)

from glitnir_forseti import evidence_provider as forseti
from glitnir_forseti.evidence_provider import _sampled
from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import (
    MAX_PATHS,
    MAX_STEPS,
    MAX_WORK,
    NOT_PLAIN,
    EvidenceError,
    SamplePlan,
    check_sample_work,
    emission_command,
    read_directive,
    sample_plan,
)
from glitnir_forseti.stepped import sampler
from glitnir_forseti.stepped.models import HEDGED_SPREAD_STEPPED
from glitnir_forseti.stepped.numbers import expected_energy, rho
from glitnir_forseti.stepped.sampler import (
    draw,
    ensemble,
    sample,
    sample_payload,
    wilson,
)

HEDGED = "Gimle.Forseti.Examples.HedgedSpread.compiled"
REQUIRED = "(law := gaussian) (variance := 64) (observe := s)"
#: What Lean printed for the hedged spread's ``sample`` emission
#: (`tests/test_stepped_lean.py` runs it live): ``ds = d − s − h`` and
#: ``dh = s − h`` over the coordinates ``[d, s, h]``, from the flat book.
HEDGED_EMITTED = {
    "coordinates": ["driver-d", "state-s", "state-h"],
    "driver": 0,
    "field": [
        ["add", ["add", ["neg", ["var", "1"]], ["neg", ["var", "2"]]], ["var", "0"]],
        ["add", ["var", "1"], ["neg", ["var", "2"]]],
    ],
    "initial": [["0", "1"], ["0", "1"]],
    "states": ["state-s", "state-h"],
    "step": ["1", "256"],
}
HEDGED_PAYLOAD = sample_payload(HEDGED_EMITTED)


def _sample(options: str, model: str = HEDGED) -> evidence.Simulation:
    return read_directive("sample", model, options)


def plan(**changes) -> SamplePlan:
    """The directive's default plan for the hedged spread, with ``changes``."""
    base = sample_plan({"law": "gaussian", "variance": "64", "observe": "s"})
    exact = {
        key: Fraction(value) if key in ("variance", "barrier") else value
        for key, value in changes.items()
    }
    return dataclasses.replace(base, **exact)


def directive_with(**options) -> evidence.Simulation:
    """A parsed ``sample`` directive with the required options and ``options``."""
    written = "".join(f" ({key} := {value})" for key, value in options.items())
    simulation = _sample(REQUIRED + written)
    assert simulation.error == ""
    return simulation


def _provenance(card: dict) -> dict[str, str]:
    return dict(card["provenance"])


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
        "(law := gaussian) (variance := 64) (observe := V)",
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
        (f"{REQUIRED} (paths := 0)", f"paths must be 1–{MAX_PATHS}"),
        (f"{REQUIRED} (paths := 100001)", f"paths must be 1–{MAX_PATHS}"),
        (
            # 70,922 × 282 is the least count over the cap that the step and
            # path limits allow; neither the terms nor the states are known
            # before Lean prints the field, so each path-step counts once.
            f"{REQUIRED} (paths := 70922) (steps := 282)",
            "this run would take 20,000,004 updates of field terms and states, "
            f"more than the {MAX_WORK:,} allowed; use fewer paths or steps",
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
            "(law := gaussian) (variance := 64) (observe := V)",
            "observe names a state, as (observe := s); V is the model's energy,"
            " not a state",
        ),
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
    # 100,000 paths, the most there may be, of 200 steps is the cap exactly.
    at_work = _sample(f"{REQUIRED} (paths := {MAX_PATHS}) (steps := 200)")
    assert at_work.error == "" and at_work.sample is not None
    assert MAX_PATHS * 200 == MAX_WORK
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


def test_the_run_counts_the_fields_terms_and_states_in_its_work() -> None:
    # Every path-step evaluates each term and updates each state.
    check_sample_work(4000, 256, terms=17, states=2)  # 19,456,000
    with pytest.raises(EvidenceError, match="take 20,480,000 updates of field terms"):
        check_sample_work(4000, 256, terms=18, states=2)
    # A field of no terms still moves, draws and checks every state.
    with pytest.raises(EvidenceError, match="take 64,512,000 updates of field terms"):
        check_sample_work(4000, 256, terms=0, states=63)
    # Before Lean prints the field, a path-step counts once.
    check_sample_work(MAX_PATHS, 200)
    with pytest.raises(EvidenceError, match="take 20,000,004 updates of field terms"):
        check_sample_work(70922, 282)


def test_a_malformed_sample_option_gets_the_sample_hint() -> None:
    simulation = _sample("(law := gaussian) (variance := 64) observe s")
    assert simulation.error == (
        "options are written (law := gaussian) (variance := 64) (observe := s)"
    )


def test_the_plan_is_exact() -> None:
    plan = sample_plan(
        {"law": "gaussian", "variance": "0.1", "observe": "s", "barrier": "1/3"}
    )
    assert plan.variance == Fraction(1, 10) and type(plan.variance) is Fraction
    assert plan.barrier == Fraction(1, 3) and type(plan.barrier) is Fraction


def test_an_unknown_verb_lists_all_three() -> None:
    simulation = read_directive("fly", HEDGED, REQUIRED)
    assert "use simulate, iterate or sample" in simulation.error


def test_an_omitted_show_is_at_most_the_paths() -> None:
    few = _sample(f"{REQUIRED} (paths := 5)")
    assert few.error == "" and few.sample is not None and few.sample.show == 5
    many = _sample(f"{REQUIRED} (paths := 9)")
    assert many.sample is not None and many.sample.show == 8
    # A show written out is still held to the paths.
    assert "show must be 1–5" in _sample(f"{REQUIRED} (paths := 5) (show := 6)").error


def test_the_unknown_law_message_lists_the_laws(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "LAWS", ("gaussian", "laplace", "cauchy"))
    with pytest.raises(EvidenceError) as refused:
        sample_plan({"law": "uniform", "variance": "1", "observe": "s"})
    assert str(refused.value) == (
        "unknown law 'uniform'; use gaussian, laplace or cauchy"
    )


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
    # The step and the driver's place come from Lean, not from Python: the
    # driver's index is looked up by the driver binding's stable ID among the
    # runtime coordinates, and the only number in it is the one-driver test.
    assert "1/256" not in cmd
    assert "let ids := e.drivenIds ds" in cmd
    driver = cmd.split('("driver", ')[1].split('("field", ')[0]
    assert "(ds.head?.bind fun d => Gimle.Asgard.Model.index ids d.driverId)" in (
        driver
    )
    assert re.findall(r"\d+", driver) == ["1"] and "width ds = 1" in driver


def test_the_sample_emission_refuses_an_unregistered_model() -> None:
    with pytest.raises(EvidenceError, match="not a registered stepped model"):
        emission_command("sample", "hedgedStepped")
    with pytest.raises(EvidenceError, match="is not a Lean name"):
        emission_command("sample", f"{HEDGED}) (x")


# ----- the payload ------------------------------------------------------------------


def _var(index: int) -> list[str]:
    return ["var", str(index)]


_COUNTS = r"states \({}\), initial values \({}\) and field rows \({}\) differ in number"


def test_the_payload_is_read_exactly() -> None:
    payload = HEDGED_PAYLOAD
    assert payload.step == Fraction(1, 256) and type(payload.step) is Fraction
    assert payload.initial == (Fraction(0), Fraction(0))
    assert payload.coordinates == ("driver-d", "state-s", "state-h")
    assert (payload.driver, payload.states, payload.places) == (0, ("s", "h"), (1, 2))
    d, s, h = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    assert [dict(output) for output in payload.field.outputs] == [
        {d: 1, s: -1, h: -1},
        {s: 1, h: -1},
    ]


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"driver": None}, "exactly one driver coordinate; this model has none"),
        ({"driver": True}, "the driver is a coordinate index"),
        ({"driver": 3}, "the driver is a coordinate index"),
        ({"driver": 1}, "the driver's coordinate is a state"),
        ({"initial": [["0", "1"]]}, _COUNTS.format(2, 1, 2)),
        (
            {"field": [HEDGED_EMITTED["field"][0]]},
            _COUNTS.format(2, 2, 1),
        ),
        ({"states": ["state-s"]}, _COUNTS.format(1, 2, 2)),
        (
            {"coordinates": ["driver-d", "state-s", "state-h", "driver-e"]},
            "the coordinates are not the driver and the states",
        ),
        (
            {"coordinates": ["driver-d", "state-s", "state-x"]},
            "the coordinates are not the driver and the states",
        ),
        (
            {"coordinates": ["driver-d", "state-s", "s"], "states": ["state-s", "s"]},
            "the state names are not distinct",
        ),
        ({"states": "state-s"}, "the states are a list of IDs"),
        ({"step": ["0", "1"]}, "the step must be positive"),
        ({"step": ["2", "512"]}, "must be reduced"),
        ({"field": [["pow", _var(1), "2"], _var(2)]}, "unknown expression"),
    ],
)
def test_a_payload_that_cannot_be_sampled_is_refused(changes, error) -> None:
    with pytest.raises(EvidenceError, match=error):
        sample_payload({**HEDGED_EMITTED, **changes})


@pytest.mark.parametrize("key", sorted(HEDGED_EMITTED))
def test_a_payload_missing_a_key_is_refused(key: str) -> None:
    emitted = {k: v for k, v in HEDGED_EMITTED.items() if k != key}
    with pytest.raises(EvidenceError, match=f"the emitted model has no '{key}'"):
        sample_payload(emitted)


# ----- the sampler ------------------------------------------------------------------


def test_sample_is_deterministic_and_bounded() -> None:
    a = sample(HEDGED_PAYLOAD, plan(paths=500, steps=256, seed=7, show=4))
    b = sample(HEDGED_PAYLOAD, plan(paths=500, steps=256, seed=7, show=4))
    assert a.breach_fraction == b.breach_fraction
    assert len(a.paths) == 4 and all(len(p) == 257 for p in a.paths)
    assert all(0 <= x <= y <= 1 for x, y in zip(a.breach_series, a.breach_series[1:]))
    assert a.interval[0] <= a.breach_fraction <= a.interval[1]
    assert a == b and a.finite is True
    assert sample(HEDGED_PAYLOAD, plan(paths=500, seed=8, show=4)).paths != a.paths


def test_gaussian_breach_fraction_matches_the_spec_range() -> None:
    r = sample(
        HEDGED_PAYLOAD,
        plan(paths=4000, steps=256, seed=7, show=0, law="gaussian", variance=64),
    )
    assert 0.002 <= r.breach_fraction <= 0.012


def test_three_point_breach_fraction_near_exact() -> None:
    r = sample(
        HEDGED_PAYLOAD,
        plan(paths=4000, steps=256, seed=7, show=0, law="threepoint", variance=64),
    )
    assert 0.19 <= r.breach_fraction <= 0.25  # exact value 0.2198


def test_the_sampled_energy_matches_its_exact_expectation() -> None:
    """The ensemble's mean ``s² + h²`` matches ``ρ^n·V₀ + Δ²·v·Σ_{k<n} ρ^k``.

    At the marks 64, 128 and 256 the sampled mean lies within three standard
    errors of the kernel-checked exact expectation (0.108507… at mark 256).
    """
    run = plan(paths=2000, seed=1)
    step = HEDGED_PAYLOAD.step
    start = sum(x * x for x in HEDGED_PAYLOAD.initial)
    checked = []
    for mark, (s, h) in ensemble(HEDGED_PAYLOAD, run):
        if mark in (64, 128, 256):
            energy = [a * a + b * b for a, b in zip(s, h, strict=True)]
            exact = expected_energy(start, rho(step), step, run.variance, mark)
            error = statistics.stdev(energy) / math.sqrt(run.paths)
            assert abs(statistics.fmean(energy) - float(exact)) <= 3 * error, mark
            checked.append(mark)
    assert checked == [64, 128, 256]


def test_the_paths_start_at_the_initial_state_and_mark_every_step() -> None:
    result = sample(HEDGED_PAYLOAD, plan(paths=20, steps=10, show=3))
    assert result.marks == tuple(range(11))
    assert [path[0] for path in result.paths] == [0.0, 0.0, 0.0]
    assert result.breach_series[-1] == result.breach_fraction
    assert len(result.breach_series) == 11


def test_the_sampler_follows_the_printed_coordinates() -> None:
    """The same model printed as ``[s, h, d]``, its states as ``[h, s]``."""
    permuted = {
        "coordinates": ["state-s", "state-h", "driver-d"],
        "driver": 2,
        "field": [
            ["add", _var(0), ["neg", _var(1)]],
            ["add", ["add", ["neg", _var(0)], ["neg", _var(1)]], _var(2)],
        ],
        "initial": [["0", "1"], ["0", "1"]],
        "states": ["state-h", "state-s"],
        "step": ["1", "256"],
    }
    payload = sample_payload(permuted)
    assert (payload.driver, payload.states, payload.places) == (2, ("h", "s"), (1, 0))
    run = plan(paths=50, show=3)
    moved, original = sample(payload, run), sample(HEDGED_PAYLOAD, run)
    assert moved.breach_series == original.breach_series
    for a, b in zip(moved.paths, original.paths, strict=True):
        assert a == pytest.approx(b, rel=1e-12, abs=1e-15)


def test_the_observed_state_is_the_one_named() -> None:
    s = sample(HEDGED_PAYLOAD, plan(paths=5, steps=20, show=1))
    h = sample(HEDGED_PAYLOAD, plan(paths=5, steps=20, show=1, observe="h"))
    # A shock moves the exposure at once and the hedge a step later.
    assert s.paths[0][1] != 0.0 and h.paths[0][1] == 0.0 and h.paths[0][2] != 0.0


def test_an_observed_state_the_model_lacks_is_refused() -> None:
    with pytest.raises(
        EvidenceError, match="observe names 'V', not a state of the model; its states"
    ):
        sample(HEDGED_PAYLOAD, plan(observe="V"))


class _Ran(Exception):
    """Raised in place of drawing shocks: the run got past its work check."""


def _ran(*_) -> None:
    raise _Ran


def test_the_work_is_counted_with_the_fields_terms_and_states(monkeypatch) -> None:
    """The hedged field has five terms over two states: seven per path-step.

    11,160 paths of 256 steps are 19,998,720 updates, within the cap; one path
    more is 20,000,512, refused before a shock is drawn. Both pass the
    directive's floor of one per path-step.
    """
    monkeypatch.setattr(sampler, "shocks", _ran)
    within = forseti.parse("sample", HEDGED, f"{REQUIRED} (paths := 11160)")
    with pytest.raises(_Ran):
        forseti.compute(within, HEDGED_EMITTED)
    over = forseti.parse("sample", HEDGED, f"{REQUIRED} (paths := 11161)")
    with pytest.raises(
        EvidenceError,
        match="this run would take 20,000,512 updates of field terms and states, "
        f"more than the {MAX_WORK:,} allowed; use fewer paths or steps",
    ):
        forseti.compute(over, HEDGED_EMITTED)


#: A print Lean's output cannot be trusted not to be: a notebook may define its
#: own ``HedgedSpread.compiled`` with an ``s`` and 62 more states, 63 being the
#: most a field has, whose right-hand sides are all zero, so the field has no
#: terms at all.
IDLE_STATES = ["state-s", *(f"state-x{i}" for i in range(1, 63))]
IDLE_EMITTED = {
    "coordinates": ["driver-d", *IDLE_STATES],
    "driver": 0,
    "field": [["const", "0", "1"]] * len(IDLE_STATES),
    "initial": [["0", "1"]] * len(IDLE_STATES),
    "states": IDLE_STATES,
    "step": ["1", "256"],
}


def test_a_field_of_many_idle_states_is_refused_before_it_allocates(
    monkeypatch,
) -> None:
    """Each state costs a column per path-step even when no term reads it.

    The directive passes at the floor, 100,000 paths of 200 steps; the printed
    field has no terms but 63 states, 1,260,000,000 updates. The same field at
    20,000,000 paths of one step, past the path cap, is refused the same way.
    Neither run allocates a column or draws a shock.
    """
    payload = sample_payload(IDLE_EMITTED)
    assert sum(len(output) for output in payload.field.outputs) == 0
    monkeypatch.setattr(sampler, "shocks", _ran)
    request = forseti.parse(
        "sample", HEDGED, f"{REQUIRED} (paths := {MAX_PATHS}) (steps := 200)"
    )
    tracemalloc.start()
    try:
        with pytest.raises(EvidenceError, match="take 1,260,000,000 updates"):
            forseti.compute(request, IDLE_EMITTED)
        with pytest.raises(EvidenceError, match="take 1,260,000,000 updates"):
            ensemble(payload, plan(paths=20_000_000, steps=1))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2_000_000, peak


def test_a_jump_past_float64_is_refused() -> None:
    # A printed step of 10⁻⁴⁰⁰ puts the three-point jump at 10⁴⁰⁰ + 1.
    options = "(law := threepoint) (variance := 64) (observe := s) (paths := 1)"
    request = forseti.parse("sample", HEDGED, options)
    tiny = {**HEDGED_EMITTED, "step": ["1", "1" + "0" * 400]}
    with pytest.raises(EvidenceError, match="cannot be run in float64"):
        forseti.compute(request, tiny)


def test_times_past_float64_are_refused_before_the_run(monkeypatch) -> None:
    # Without shocks the flat book stays finite, so only the times overflow.
    options = "(law := gaussian) (variance := 0) (observe := s) (paths := 1)"
    request = forseti.parse("sample", HEDGED, options)
    huge = {**HEDGED_EMITTED, "step": ["1" + "0" * 306, "1"]}
    monkeypatch.setattr(sampler, "shocks", lambda *_: pytest.fail("it ran"))
    with pytest.raises(EvidenceError, match="times would exceed float64"):
        forseti.compute(request, huge)


def test_a_breach_between_the_shown_marks_still_counts() -> None:
    """Every mark is checked; only the series the card shows are downsampled.

    With ``Δ = 1`` the step permutes ``(a, b, c)`` to ``(c, a, b)``, so ``b`` is
    2 at the marks 1, 4, 7, … and 0 at every third mark, which are the only
    marks a run of 9,000 steps shows.
    """
    cycle = {
        "coordinates": ["driver-d", "state-a", "state-b", "state-c"],
        "driver": 0,
        "field": [
            ["add", _var(3), ["neg", _var(1)]],
            ["add", _var(1), ["neg", _var(2)]],
            ["add", _var(2), ["neg", _var(3)]],
        ],
        "initial": [["2", "1"], ["0", "1"], ["0", "1"]],
        "states": ["state-a", "state-b", "state-c"],
        "step": ["1", "1"],
    }
    run = plan(paths=1, steps=9000, show=1, variance=0, observe="b")
    result = sample(sample_payload(cycle), run)
    assert result.marks == tuple(range(0, 9001, 3))
    assert set(result.paths[0]) == {0.0}
    assert result.breach_series == (0.0,) + (1.0,) * (len(result.marks) - 1)


def test_long_runs_keep_the_last_mark_within_the_card() -> None:
    result = sample(HEDGED_PAYLOAD, plan(paths=2, steps=9001, show=2))
    assert len(result.marks) <= MAX_SAMPLES and result.marks[-1] == 9001
    assert result.marks[:3] == (0, 3, 6)
    assert all(len(path) == len(result.marks) for path in result.paths)
    assert len(result.breach_series) == len(result.marks)


#: ``ds = s²`` with ``Δ = 1`` from 2 squares past float64 within a dozen steps.
SQUARE = {
    "coordinates": ["driver-d", "state-s"],
    "driver": 0,
    "field": [["mul", _var(1), _var(1)]],
    "initial": [["2", "1"]],
    "states": ["state-s"],
    "step": ["1", "1"],
}


def test_a_blow_up_stops_the_run_and_is_flagged() -> None:
    result = sample(sample_payload(SQUARE), plan(paths=3, steps=50, show=2))
    assert result.finite is False and len(result.marks) < 51
    assert all(math.isfinite(v) for path in result.paths for v in path)
    assert result.breach_series[-1] == result.breach_fraction == 1.0


def test_each_law_draws_its_support() -> None:
    rng = random.Random(1)
    step, barrier = Fraction(1, 256), Fraction(1)
    two = {draw("twopoint", Fraction(64), barrier, step, rng) for _ in range(100)}
    assert two == {8.0, -8.0}
    # ε = v/(2·257²) = 1/4 puts half the mass on zero and a quarter on each jump.
    three = [
        draw("threepoint", Fraction(66049, 2), barrier, step, rng) for _ in range(2000)
    ]
    assert set(three) == {0.0, 257.0, -257.0}
    assert 0.45 < three.count(0.0) / 2000 < 0.55
    # The two jumps share the rest equally, a quarter each (±0.05 is 5σ).
    assert 0.2 < three.count(257.0) / 2000 < 0.3
    assert 0.2 < three.count(-257.0) / 2000 < 0.3
    normal = [draw("gaussian", Fraction(64), barrier, step, rng) for _ in range(4000)]
    assert abs(statistics.fmean(normal)) < 0.5
    assert 7.5 < statistics.stdev(normal) < 8.5
    with pytest.raises(EvidenceError, match="no three-point law"):
        draw("threepoint", Fraction(70000), barrier, step, rng)


def test_the_wilson_interval() -> None:
    low, high = wilson(50, 100)
    assert (round(low, 4), round(high, 4)) == (0.4038, 0.5962)
    assert wilson(0, 4000)[0] == 0.0 and wilson(4000, 4000)[1] == 1.0
    low, high = wilson(23, 4000)
    assert low < 23 / 4000 < high


# ----- the card ---------------------------------------------------------------------


def test_card_has_no_bound_and_respects_caps() -> None:
    card = _sampled(directive_with(show=8), HEDGED_EMITTED)
    assert len(card["series"]["names"]) == 9 <= MAX_SERIES
    assert len(card["provenance"]) <= MAX_PAIRS
    assert not any(
        "<=" in v or "≤" in v or "bound" in k or "bound" in v
        for k, v in _provenance(card).items()
    )
    assert card["finite"] is True
    assert checked_evidence(card) == card


def test_the_card_names_its_series_and_provenance() -> None:
    options = f"{REQUIRED} (paths := 200) (show := 3)"
    card = forseti.compute(forseti.parse("sample", HEDGED, options), HEDGED_EMITTED)
    assert checked_evidence(card) == card and "plane" not in card
    series = card["series"]
    assert series["names"] == ["path 1", "path 2", "path 3", "breach fraction"]
    assert series["times"] == [k / 256 for k in range(257)]
    result = sample(HEDGED_PAYLOAD, plan(paths=200, show=3))
    assert [list(column) for column in zip(*series["values"])] == [
        *map(list, result.paths),
        list(result.breach_series),
    ]
    assert [label for label, _ in card["provenance"]] == [
        "verb",
        "model",
        "law",
        "variance",
        "paths",
        "steps",
        "step",
        "seed",
        "barrier",
        "observe",
        "arithmetic",
        "rounded",
        "breach fraction",
        "sampling interval",
        "field sha256",
        "provider",
        "created",
    ]
    provenance = _provenance(card)
    assert {k: provenance[k] for k in list(provenance)[:12]} == {
        "verb": "sample",
        "model": HEDGED,
        "law": "gaussian",
        "variance": "64",
        "paths": "200",
        "steps": "256",
        "step": "1/256",
        "seed": "7",
        "barrier": "1",
        "observe": "s",
        "arithmetic": "float64",
        "rounded": "none",
    }
    assert float(provenance["breach fraction"]) == pytest.approx(
        result.breach_fraction, rel=1e-5
    )
    interval = provenance["sampling interval"]
    assert interval.endswith(" (95 %, Wilson)")
    low, high = map(float, interval.removesuffix(" (95 %, Wilson)").split(" to "))
    assert (low, high) == pytest.approx(result.interval, rel=1e-3)
    field = json.dumps(HEDGED_EMITTED["field"], separators=(",", ":")).encode()
    assert provenance["field sha256"] == hashlib.sha256(field).hexdigest()
    assert provenance["provider"].startswith("glitnir-forseti ")
    assert all(len(value) <= MAX_TEXT for value in provenance.values())


@pytest.mark.parametrize(
    ("options", "rounded"),
    [
        # A Gaussian or two-point law takes the variance into float64.
        ("(law := gaussian) (variance := 1/3) (observe := s)", "1/3"),
        ("(law := twopoint) (variance := 1/3) (observe := s)", "1/3"),
        # A three-point law takes its mass ε = v/(2L²) instead, here with the
        # jump L = 86 that leaves the band 1/3, and that barrier too; the step
        # 1/256 and the jump are exact.
        (
            "(law := threepoint) (variance := 64) (observe := s) (barrier := 1/3)",
            "1/3, 8/1849",
        ),
        ("(law := threepoint) (variance := 64) (observe := s)", "32/66049"),
        ("(law := gaussian) (variance := 1/4) (observe := s)", "none"),
    ],
)
def test_the_card_lists_what_float64_rounded(options: str, rounded: str) -> None:
    request = forseti.parse("sample", HEDGED, f"{options} (paths := 20) (steps := 8)")
    card = forseti.compute(request, HEDGED_EMITTED)
    assert _provenance(card)["rounded"] == rounded
    assert checked_evidence(card) == card


def test_the_card_lists_a_rounded_coefficient_and_initial_value() -> None:
    """The printed field's coefficients and initial state are run in float64."""
    third = {
        **HEDGED_EMITTED,
        "field": [
            ["add", HEDGED_EMITTED["field"][0], ["const", "1", "3"]],
            HEDGED_EMITTED["field"][1],
        ],
        "initial": [["1", "10"], ["0", "1"]],
    }
    request = forseti.parse("sample", HEDGED, f"{REQUIRED} (paths := 2) (steps := 4)")
    card = forseti.compute(request, third)
    assert _provenance(card)["rounded"] == "1/3, 1/10"


def test_a_long_run_is_downsampled_on_the_card() -> None:
    options = f"{REQUIRED} (paths := 2) (steps := 9001) (show := 2)"
    card = forseti.compute(forseti.parse("sample", HEDGED, options), HEDGED_EMITTED)
    assert checked_evidence(card) == card
    times = card["series"]["times"]
    assert len(times) <= MAX_SAMPLES and times[-1] == 9001 / 256


def test_a_blown_up_card_is_flagged_and_still_valid() -> None:
    options = (
        "(law := gaussian) (variance := 0) (observe := s) (paths := 3) (steps := 50)"
    )
    card = forseti.compute(forseti.parse("sample", HEDGED, options), SQUARE)
    assert card["finite"] is False
    assert checked_evidence(card) == card
    assert card["series"]["values"][-1][-1] == 1.0


def test_parse_carries_the_plan_not_the_simulate_defaults() -> None:
    request = forseti.parse(
        "sample", HEDGED, f"{REQUIRED} (paths := 100) (barrier := 3/2)"
    )
    assert request.options == {
        "law": "gaussian",
        "variance": "64",
        "paths": 100,
        "steps": 256,
        "seed": 7,
        "show": 8,
        "observe": "s",
        "barrier": "3/2",
    }
    assert json.loads(json.dumps(request.options)) == request.options
    rebuilt = forseti._simulation(request)
    assert rebuilt.verb == "sample" and rebuilt.model == HEDGED
    assert rebuilt.sample == directive_with(paths=100, barrier="3/2").sample


def test_compute_refuses_what_lean_did_not_print_as_a_model() -> None:
    request = forseti.parse("sample", HEDGED, REQUIRED)
    with pytest.raises(EvidenceError, match="malformed field"):
        forseti.compute(request, [1, 2])
    with pytest.raises(EvidenceError, match="exactly one driver coordinate"):
        forseti.compute(request, {**HEDGED_EMITTED, "driver": None})


def test_no_decision_reads_the_sampler() -> None:
    """Evidence is never proof: nothing in the decision lane imports the sampler."""
    package = Path(sampler.__file__).parent.parent
    lane = [
        package / "decision_provider.py",
        package / "stepped" / "service.py",
        package / "stepped" / "render.py",
        *sorted((package / "claims").rglob("*.py")),
    ]
    for path in lane:
        imported = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not {
            name
            for name in imported
            if name.endswith("sampler") or name.endswith("evidence_provider")
        }, path
