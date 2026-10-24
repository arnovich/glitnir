r"""Live Lean check of every block template the stepped lane proposes.

The stepped decider replays nothing: a decided goal is only a proposed block,
and the project's Lean is what makes it a proof. This test takes each example
through the notebook's decision provider, as the editor does, writes a scratch
notebook that imports what the block needs, and has Glitnir check the saved
file followed by the block (``check_candidate``, the one proof authority for a
decision). It runs against any built Forseti Lean checkout holding the stepped
library, and fails instead of skipping under the gate::

    FORSETI_REQUIRE_LEAN_PROJECT=1 \
    FORSETI_LEAN_PROJECT=/path/to/lean-checkout \
        uv run python -m pytest tests/test_stepped_lean.py

Beside the examples, which fix the constants at the catalogue's (variance 64,
a year of 256 marks, the barrier 1), variants reach template branches those
constants do not: a fixed-date bound whose ``r`` is lowered to
`service.EXP_CAP`, a fixed-date claim at a fractional barrier, and the lane's
caps. At the horizon cap of 4096 marks a horizon hold and an exact refutation
are checked; the refutation makes the kernel evaluate a power over all 4096
marks, where the hold does no work that long. The proofs of a fixed-date bound
and an expectation may unroll a recursion over every mark (``proxyRec``,
``geomSum``), so their cap is lower, and each is checked at it.

A check is held to a budget of `CHECK_BUDGET_MS`, five minutes unless the
environment variable ``FORSETI_CHECK_BUDGET_MS`` names another number of
milliseconds, as a loaded or slower machine may need.

Each scratch notebook is a new module of the checkout's ``Gimle.Forseti.Tests``
library, written by the ``scratch_notebook`` fixture, which creates it
exclusively and removes only what it created.

The ``sample`` evidence verb's emission is run here too, through the runner
Glitnir runs evidence with: what Lean prints is read for the sampler, and the
provider turns it into the card Glitnir would save.
"""

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from glitnir.lean.project_checking import check_candidate
from glitnir.providers.outcomes import checked_decision, checked_evidence

from glitnir_forseti import evidence_provider
from glitnir_forseti.claims.text import render_goal
from glitnir_forseti.decision_provider import decide, parse
from glitnir_forseti.notebook import evidence
from glitnir_forseti.stepped import service
from glitnir_forseti.stepped.examples import EXAMPLES, Example
from glitnir_forseti.stepped.models import HEDGED_SPREAD_STEPPED

#: The axioms Lean itself provides; a block may rest on nothing else.
STANDARD_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
#: The longest a block may take to check, in milliseconds, unless the
#: environment says otherwise (`_check_budget_ms`).
CHECK_BUDGET_MS = 300_000
#: A block that needs nothing, to have the checker replay a notebook's imports.
WARM_BLOCK = "theorem warm_up : True := trivial"
#: The compiled hedged spread a ``sample`` directive names, and its options. The
#: notebook imports the modules the evidence provider declares for every verb
#: too, though Glitnir's runner loads them from the project's build either way.
HEDGED = "Gimle.Forseti.Examples.HedgedSpread.compiled"
SAMPLE_OPTIONS = "(law := gaussian) (variance := 64) (observe := s)"
SAMPLE_DIRECTIVE = f"-- @evidence sample {HEDGED} {SAMPLE_OPTIONS}"
SAMPLE_NOTEBOOK = f"""import Gimle.Asgard.Compile.RationalInterchange
import Gimle.Asgard.Model.Continuous
import Gimle.Forseti.Examples.HedgedSpread
import Gimle.Forseti.Examples.HedgedSpreadStepped

/-! # The stepped hedged spread, sampled -/

{SAMPLE_DIRECTIVE}
"""


def _check_budget_ms() -> int:
    """Return the budget a check is held to.

    An unset or empty ``FORSETI_CHECK_BUDGET_MS`` means `CHECK_BUDGET_MS`.

    Raises:
        pytest.fail.Exception: If the variable is not a positive whole number.
    """
    value = os.environ.get("FORSETI_CHECK_BUDGET_MS", "").strip()
    if not value:
        return CHECK_BUDGET_MS
    try:
        budget = int(value)
    except ValueError:
        budget = 0
    if budget <= 0:
        pytest.fail(
            "FORSETI_CHECK_BUDGET_MS must be a positive whole number of"
            f" milliseconds, got {value!r}",
            pytrace=False,
        )
    return budget


@pytest.fixture(autouse=True)
def check_budget_ms() -> int:
    """Return the check budget, failing a test at once if the variable is bad."""
    return _check_budget_ms()


@dataclass(frozen=True)
class Checked:
    """What the project's Lean said about a proposed block.

    Attributes:
        ok: Whether Glitnir reports the block's theorem as checked.
        message: Glitnir's message, which names the failing line otherwise.
        axioms: The axioms the theorem depends on.
        check_ms: How long the check took, in milliseconds.
        stage_ms: How long each of the check's stages took, in milliseconds.
    """

    ok: bool
    message: str
    axioms: tuple[str, ...]
    check_ms: int
    stage_ms: dict[str, int]


def _check(
    write: Callable[[str, str], Path], imports: str, block: str, theorem: str
) -> Checked:
    """Check a block as the editor does, in a scratch notebook of the project.

    The notebook holds the imports, then a directive line; the block is checked
    right after it. ``write`` is the ``scratch_notebook`` fixture's, so no file
    of the checkout is overwritten and only the scratch file is removed.
    """
    header = "".join(f"{line}\n" for line in imports.split("\n"))
    text = f"{header}\n-- @decide {theorem} : (scratch)\n"
    path = write("GlitnirSteppedLive", text)
    stage_ms: dict[str, int] = {}
    started = time.monotonic()
    verdict = check_candidate(
        path,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        insert_after_line=text.count("\n"),
        block=block,
        theorem=theorem,
        cancel=threading.Event(),
        stage_ms=stage_ms,
    )
    elapsed = round((time.monotonic() - started) * 1000)
    return Checked(
        verdict["status"] == "checked",
        verdict["message"],
        tuple(verdict["axioms"]),
        elapsed,
        stage_ms,
    )


@pytest.fixture(scope="module")
def replayed() -> set[str]:
    """The import sets whose one-off kernel replay the session cache holds."""
    return set()


@pytest.fixture
def check_block(scratch_notebook, replayed: set[str], record_check_stages):
    """Return a function that checks a decision outcome's block, timed.

    The outcome's own last stage lists the imports the block needs, so a block
    whose imports are wrong fails here. The first check of an import set
    replays its whole closure in the kernel, which takes minutes and is cached
    for the session (`conftest.private_replay_cache`); a trivial block is
    checked first, untimed, so that a block's time is its own, and the block's
    check must reuse that replay rather than replay the imports again. Each
    check's stage times are written to the log (`record_check_stages`).
    """

    def check(outcome: dict) -> Checked:
        assert outcome["stages"][-1][:2] == ["imports", "needed"], outcome["stages"]
        imports = outcome["stages"][-1][2]
        if imports not in replayed:
            warm = _check(scratch_notebook, imports, WARM_BLOCK, "warm_up")
            record_check_stages("warm_up", warm.stage_ms)
            assert warm.ok, warm.message
            replayed.add(imports)
            print(f"replay of {imports!r}: {warm.check_ms} ms")
        checked = _check(
            scratch_notebook, imports, outcome["block"], outcome["theorem"]
        )
        record_check_stages(outcome["theorem"], checked.stage_ms)
        # The warm-up's import replay was recorded, so this check reuses it:
        # a refused record would replay the closure (minutes) on every check.
        assert "replay_imports" not in checked.stage_ms, checked.stage_ms
        if checked.ok:
            # A checked block was replayed over the recorded imports.
            assert "replay" in checked.stage_ms, checked.stage_ms
        return checked

    return check


def _decided(example: Example) -> dict:
    """Decide an example as the provider does, from its goal's text."""
    request = parse(example.name, "stepped", render_goal(example.goal))
    return decide(request, threading.Event())


def _status(outcome: dict) -> str:
    """Write a provider outcome as an example's expected status."""
    if outcome["status"] == "unknown":
        return f"UNKNOWN:{outcome['reason']}"
    return outcome["status"].upper()


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)
def test_emitted_block_checks(
    check_block, check_budget_ms: int, example: Example
) -> None:
    outcome = _decided(example)
    assert _status(outcome) == example.expected, outcome["stages"]
    if example.expected.startswith("UNKNOWN"):
        # An undecided goal proposes nothing for Lean to check.
        assert "block" not in outcome
        return
    # Glitnir accepts the outcome's shape before it checks the block.
    assert checked_decision(outcome)["block"] == outcome["block"]
    result = check_block(outcome)
    assert result.ok, result.message
    assert set(result.axioms) <= STANDARD_AXIOMS
    assert result.check_ms < check_budget_ms
    print(f"{example.name}: {result.check_ms} ms, axioms {sorted(result.axioms)}")


#: The hedged spread from the flat book in steps of 1/256, as every goal starts.
_HEDGED_GOAL = "stepped hedged-spread from (0, 0) at 0 step 1/256"
#: The most marks a fixed-date or expected claim ranges over.
_UNROLLED_CAP = HEDGED_SPREAD_STEPPED.unrolled_cap
#: Goals whose blocks take template branches no example takes, with the status
#: the decider gives each and a piece of text its block must hold to show it
#: took that branch.
VARIANTS = {
    # r = 8² / (2·c_up) ≈ 350.7, so ⌊r⌋ is above the cap and lowered to it:
    # `norm_num at hE` faces e₋^256.
    "capped_exponent": (
        f"{_HEDGED_GOAL} shocks subgaussian 64 at every mark within 256"
        " observe s breach 8 probability <= 1/100",
        "PROVEN",
        f"exp_ge_floor_cubic {service.EXP_CAP} (r := {service.EXP_CAP})",
    ),
    # The band |s| ≤ 3/2: the barrier is a fraction in the numeric core.
    "fractional_barrier": (
        f"{_HEDGED_GOAL} shocks subgaussian 64 at every mark within 256"
        " observe s breach 3/2 probability <= 1/100",
        "PROVEN",
        "2 * Real.exp (-(3/2) ^ 2 / (2 * c))",
    ),
    # N·Δ²·v/B² = 4096 · 4 / 256² = 1/4 at the lane's horizon cap.
    "horizon_cap": (
        f"{_HEDGED_GOAL} shocks variance 4 within 4096 observe s breach 1"
        " probability <= 1/4",
        "PROVEN",
        "(breach_within h (by norm_num) 4096).mono",
    ),
    # The proxy at mark 1024 is the 1024-step recursion `proxyRec`, which
    # `decide +kernel` evaluates before the numeric core. The independent
    # replay's kernel runs out of depth between 1936 and 2176 marks.
    "fixed_date_cap": (
        f"{_HEDGED_GOAL} shocks subgaussian 64 at every mark within {_UNROLLED_CAP}"
        " observe s breach 1 probability <= 1/100",
        "PROVEN",
        f"have hc : proxy 64 {_UNROLLED_CAP} ≤",
    ),
    # The expected energy at mark 1024 is the partial sum `geomSum 1024`
    # (≈ 0.1254, below 33/256), which `decide +kernel` evaluates. The replay's
    # kernel runs out of depth between 1216 and 1456 marks.
    "expectation_cap": (
        f"{_HEDGED_GOAL} shocks variance 64 observe V expected <= 33/256"
        f" at {_UNROLLED_CAP}",
        "PROVEN",
        f"Δ ^ 2 * 64 * geomSum {_UNROLLED_CAP} ≤ 33/256",
    ),
    # 9/10 is below the three-point law's 1 − (1 − 2ε)^4096 ≈ 0.9811 and above
    # Bernoulli's ≈ 0.7987: the exact refutation, whose power `decide +kernel`
    # evaluates over 4096 factors.
    "exact_refutation_cap": (
        f"{_HEDGED_GOAL} shocks variance 64 within 4096 observe s breach 1"
        " probability <= 9/10",
        "REFUTED",
        "(1 - 2 * (64 / (2 * 257 ^ 2) : ℚ)) ^ 4096 := by decide +kernel",
    ),
}


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_a_template_variant_checks(
    check_block, check_budget_ms: int, name: str
) -> None:
    """Each variant is decided by the real decider and checked like an example."""
    text, status, needle = VARIANTS[name]
    outcome = decide(parse(name, "stepped", text), threading.Event())
    assert _status(outcome) == status, outcome["stages"]
    assert needle in outcome["block"]
    assert checked_decision(outcome)["block"] == outcome["block"]
    result = check_block(outcome)
    print(f"{name}: {result.check_ms} ms, axioms {sorted(result.axioms)}")
    assert result.ok, result.message
    assert set(result.axioms) <= STANDARD_AXIOMS
    assert result.check_ms < check_budget_ms


@pytest.mark.parametrize("value", [None, "", "  "])
def test_an_unset_or_empty_budget_is_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("FORSETI_CHECK_BUDGET_MS", raising=False)
    else:
        monkeypatch.setenv("FORSETI_CHECK_BUDGET_MS", value)
    assert _check_budget_ms() == CHECK_BUDGET_MS == 300_000


def test_a_budget_is_a_whole_number_of_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORSETI_CHECK_BUDGET_MS", " 600000 ")
    assert _check_budget_ms() == 600_000


@pytest.mark.parametrize("value", ["abc", "1.5", "10s", "0", "-5"])
def test_any_other_budget_fails_with_a_message(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("FORSETI_CHECK_BUDGET_MS", value)
    with pytest.raises(pytest.fail.Exception) as raised:
        _check_budget_ms()
    message = str(raised.value)
    assert "FORSETI_CHECK_BUDGET_MS" in message and repr(value) in message
    assert "positive whole number of milliseconds" in message


def test_a_false_block_is_rejected(check_block) -> None:
    """A false bound fails in Lean's proof step, not in the parse.

    The block states the bound twice, as ``(1/4)``: in ``forsetiClaim`` and in
    its theorem. Stating ``(1/5)`` leaves its proof untouched, so the step
    ``(by norm_num [Δ])`` must show ``1/4 ≤ 1/5``, which Lean reports as
    ``unsolved goals``, at that step's line of the block. A parse error would
    read ``unexpected …`` instead, and could pass a weaker check.
    """
    [example] = [e for e in EXAMPLES if e.name == "breach_within_year"]
    outcome = _decided(example)
    assert outcome["block"].count("(1/4)") == 2
    false_block = outcome["block"].replace("(1/4)", "(1/5)")
    step = next(
        number
        for number, line in enumerate(false_block.split("\n"), 1)
        if ".mono (by norm_num [Δ])" in line
    )
    result = check_block({**outcome, "block": false_block})
    assert not result.ok
    assert result.message.startswith(f"Block line {step}: "), result.message
    assert "unsolved goals" in result.message
    assert "unexpected" not in result.message


def test_live_sample_emission_prints_the_stepped_model(scratch_notebook) -> None:
    """Lean prints the hedged spread's field, initial state, step, coordinates.

    The directive goes through the provider's own ``parse``, ``command`` and
    ``compute``, and its Lean through the runner Glitnir runs evidence with.
    The printed JSON is read here first: the module header puts the runtime
    coordinates at ``[d, s, h]``. The card is then checked as Glitnir checks
    it, and carries a sampled breach fraction, never a bound.
    """
    from glitnir.lean.provider_lean import run_provider_lean

    path = scratch_notebook("GlitnirSampleLive", SAMPLE_NOTEBOOK)
    [provider] = [
        p for p in evidence_provider.evidence_providers() if "sample" in p.verbs
    ]
    request = provider.parse("sample", HEDGED, SAMPLE_OPTIONS)
    result = run_provider_lean(
        path,
        expected_sha256=hashlib.sha256(SAMPLE_NOTEBOOK.encode()).hexdigest(),
        line=SAMPLE_NOTEBOOK.split("\n").index(SAMPLE_DIRECTIVE) + 1,
        provider_name=provider.name,
        imports=provider.imports,
        preamble=provider.preamble,
        command=provider.command(request),
        marker=provider.marker,
        needs="evidence",
        cancel=threading.Event(),
    )
    assert "payload" in result, result
    emitted = json.loads(result["payload"])
    print(f"emitted: {json.dumps(emitted)}")
    assert set(emitted) == {
        "states",
        "initial",
        "step",
        "coordinates",
        "driver",
        "field",
    }
    assert emitted["step"] == ["1", "256"]
    assert emitted["states"] == ["state-s", "state-h"]
    assert emitted["initial"] == [["0", "1"], ["0", "1"]]
    coordinates = emitted["coordinates"]
    assert coordinates == ["driver-d", "state-s", "state-h"]
    assert emitted["driver"] == 0 and coordinates[emitted["driver"]] == "driver-d"
    # `ds := (-s + -h) + d` and `dh := s + -h`, over the three coordinates.
    field = evidence.expression_polynomials(emitted["field"], len(coordinates))
    assert (field.input_degree, field.output_degree) == (3, 2)
    d, s, h = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
    assert [dict(output) for output in field.outputs] == [
        {d: 1, s: -1, h: -1},
        {s: 1, h: -1},
    ]
    card = provider.compute(request, emitted)
    assert checked_evidence(card) == card and card["finite"] is True
    provenance = dict(card["provenance"])
    assert card["series"]["names"][-1] == "breach fraction"
    assert 0.002 <= float(provenance["breach fraction"]) <= 0.012
    assert provenance["step"] == "1/256" and provenance["paths"] == "4000"
    assert not any(
        "<=" in value or "≤" in value or "bound" in value or "bound" in label
        for label, value in provenance.items()
    )
