"""Tests for the Forseti evidence provider (`glitnir_forseti.evidence_provider`).

Lean's side is a recorded payload: the field Lean printed for the
``DissipativeModel`` fixture and the update circuit it printed for
``PIWindup``. ``simulate`` needs no Asgard, so its tests run everywhere;
``iterate`` runs here against the test-only Asgard stand-in
(``tests/fake_asgard``) and, where Asgard is installed, against the real one.
"""

import hashlib
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from glitnir.notebook.check_records import load_records, saved_evidence_failures
from glitnir.providers import EvidenceProvider, Providers, load_providers
from glitnir.providers.outcomes import checked_evidence

from glitnir_forseti import evidence_provider as forseti
from glitnir_forseti import providers
from glitnir_forseti.notebook import evidence
from glitnir_forseti.notebook.evidence import EvidenceError

FIXTURES = Path(__file__).parent / "fixtures" / "notebooks"
FIELD = json.loads((FIXTURES / "dissipative-field.json").read_text(encoding="utf-8"))
UPDATE = json.loads((FIXTURES / "pi-windup-update.json").read_text(encoding="utf-8"))
WINDUP_LINE = next(
    line
    for line in (FIXTURES / "PIWindup.lean").read_text(encoding="utf-8").splitlines()
    if line.startswith("-- @evidence iterate")
)
#: `x⁺ = x · p` is what the stand-in's decoder always returns.
HALVING = "(state := 1) (parameter := 1/2) (steps := 3) (names := x, p)"


def _provenance(body: dict) -> dict[str, str]:
    return dict(body["provenance"])


def _simulated(options: str = "(h := 1/100) (steps := 400)", payload=None):
    request = forseti.parse("simulate", "dissipative", options)
    return forseti.compute(request, FIELD if payload is None else payload)


# ----- the provider -----------------------------------------------------------------


def test_the_providers_run_simulate_sample_and_iterate() -> None:
    simulate, iterate = forseti.evidence_providers()
    assert (simulate.verbs, iterate.verbs) == ({"simulate", "sample"}, {"iterate"})
    for provider in (simulate, iterate):
        assert isinstance(provider, EvidenceProvider) and provider.name == "forseti"
        assert provider.imports == (
            "Gimle.Asgard.Compile.RationalInterchange",
            "Gimle.Asgard.Model.Continuous",
        )
        assert provider.marker == "forsetiEvidence:" and provider.preamble == ""
        assert provider in providers()


def test_glitnir_finds_it_through_the_entry_point() -> None:
    found = load_providers()
    assert "forseti" not in found.errors
    verbs = ("simulate", "iterate", "sample")
    assert {found.evidence[verb].name for verb in verbs} == {"forseti"}


# ----- parse --------------------------------------------------------------------------


def test_parse_fills_in_the_defaults() -> None:
    request = forseti.parse("simulate", "dissipative", "")
    assert (request.verb, request.target) == ("simulate", "dissipative")
    assert request.options == {
        "solver": "euler",
        "step": "1/100",
        "steps": 100,
        "state": [],
        "inputs": [],
        "parameter": [],
        "names": [],
        "plane": [],
    }


def test_parse_keeps_exact_options_as_json(fake_asgard) -> None:
    request = forseti.parse(
        "simulate", "m", "(h := 1/3) (steps := 9) (solver := rk4) (plane := x, y)"
    )
    assert request.options["step"] == "1/3" and request.options["steps"] == 9
    assert request.options["plane"] == ["x", "y"]
    options = WINDUP_LINE.split("update", 1)[1]
    windup = forseti.parse("iterate", "update", options)
    assert windup.options["state"] == ["1", "1"]
    assert windup.options["inputs"][0] == ["1", "-1"]
    assert windup.options["parameter"] == ["1/4"]
    assert windup.options["names"] == ["x", "q", "r", "d", "θ"]
    assert json.loads(json.dumps(windup.options)) == windup.options


@pytest.mark.parametrize(
    ("verb", "options", "message"),
    [
        ("simulate", "(dt := 1)", "Unknown option 'dt'."),
        ("simulate", "(h := 1) (h := 2)", "Option 'h' is given twice."),
        (
            "simulate",
            "(h := 1/10) extra",
            "Options are written (h := 1/100) (steps := 400) (solver := rk4).",
        ),
        ("simulate", "h := 1/10", "Options are written"),
        (
            "simulate",
            "(steps := 100001)",
            "The step h must be positive, and steps 1–100000.",
        ),
        ("simulate", "(solver := ode45)", "Unknown solver 'ode45'; use euler or rk4."),
        ("simulate", "(plane := x)", "Plane names two states, as (plane := x, z)."),
        ("iterate", "(steps := 3)", "Iterate needs an initial (state := …)."),
        ("iterate", "(state := 1) (steps := 0)", "Steps must be 1–100000."),
        (
            "iterate",
            "(state := 1, 0) (names := y, y)",
            "State names must be distinct; 'y' is given twice.",
        ),
        (
            "plot",
            "",
            "Unknown evidence 'plot'; use simulate, iterate or sample.",
        ),
    ],
)
def test_parse_refuses_what_cannot_run(verb, options, message) -> None:
    with pytest.raises(ValueError) as refused:
        forseti.parse(verb, "m", options)
    assert str(refused.value).startswith(message)


NOT_PLAIN = "Numbers in evidence options must be plain decimals or fractions."


@pytest.mark.parametrize(
    ("verb", "options"),
    [
        ("simulate", "(h := 1e-999999999)"),
        ("simulate", "(h := 1e-5000)"),
        ("simulate", f"(h := {'9' * 10_000})"),
        ("simulate", f"(h := 1/{'7' * 10_000})"),
        ("simulate", "(steps := 1e5)"),
        ("simulate", f"(steps := {'1' * 41})"),
        ("simulate", "(h := 1_000)"),
        ("simulate", "(h := +1)"),
        ("simulate", "(h := 1.5/3)"),
        ("simulate", "(h := inf)"),
        ("iterate", "(state := 1e999999999)"),
        ("iterate", f"(state := 1) (inputs := 1; {'2' * 10_000})"),
        ("iterate", "(state := 1) (parameter := 0x10)"),
        ("iterate", "(state := 1) (steps := 1e9)"),
    ],
)
def test_numbers_must_be_plain_and_are_refused_quickly(verb, options) -> None:
    started = time.monotonic()
    with pytest.raises(ValueError) as refused:
        forseti.parse(verb, "m", options)
    assert time.monotonic() - started < 0.5
    assert str(refused.value) == NOT_PLAIN


@pytest.mark.parametrize(
    ("options", "step"),
    [
        ("(h := 0.25)", "1/4"),
        ("(h := 1 / 64)", "1/64"),
        ("(h := 3)", "3"),
        (f"(h := 1/{'3' * 40})", f"1/{'3' * 40}"),
        (f"(h := {'1' * 40}.{'5' * 40})", None),
    ],
)
def test_plain_numbers_still_parse(options, step) -> None:
    request = forseti.parse("simulate", "m", options)
    if step is not None:
        assert request.options["step"] == step


def test_plain_vectors_still_parse(fake_asgard) -> None:
    request = forseti.parse(
        "iterate", "u", "(state := -1.5, 2/3) (inputs := 0, 1; 1, 0) (steps := 7)"
    )
    assert request.options["state"] == ["-3/2", "2/3"]
    assert request.options["inputs"] == [["0", "1"], ["1", "0"]]
    assert request.options["steps"] == 7


def test_parse_refuses_a_target_that_is_not_a_lean_name() -> None:
    with pytest.raises(ValueError, match="is not a Lean name"):
        forseti.parse("simulate", "1bad", "")


def test_iterate_needs_asgard_and_simulate_does_not(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "gimle.asgard", None)
    assert forseti.asgard_installed() is False
    assert forseti.unavailable() is None
    assert forseti.iterate_unavailable() == forseti.NEEDS_ASGARD
    assert forseti.parse("simulate", "m", "").verb == "simulate"
    # Without Asgard the card cannot run; a run that gets here anyway fails
    # with the reason, as the setup failure (an ImportError) it is.
    with pytest.raises(ImportError) as missing:
        forseti.compute(forseti.parse("iterate", "update", HALVING), UPDATE)
    assert not isinstance(missing.value, ValueError)
    assert str(missing.value).startswith(forseti.NEEDS_ASGARD)


def test_iterate_shows_at_most_sixteen_states(fake_asgard) -> None:
    state = ", ".join(["1"] * 17)
    with pytest.raises(ValueError, match="at most 16 states"):
        forseti.parse("iterate", "update", f"(state := {state})")


# ----- command ------------------------------------------------------------------------


def test_the_command_prints_the_field_of_the_target() -> None:
    lean = forseti.command(forseti.parse("simulate", "dissipative", "(plane := x, y)"))
    assert "forsetiExprJson (p.rates.expressions i)" in lean
    assert lean.endswith(
        '#eval IO.println ("forsetiEvidence:" ++ '
        "(_root_.forsetiEvidenceJson dissipative).compress)"
    )
    assert "plane" not in lean and "{name}" not in lean and "{mark}" not in lean


def test_the_command_prints_the_update_circuit_for_iterate(fake_asgard) -> None:
    request = forseti.parse("iterate", "Ns.update", "(state := 1) (names := zz)")
    lean = forseti.command(request)
    assert "RationalWire.encodeCircuit c" in lean
    assert lean.endswith("(_root_.forsetiEvidenceJson Ns.update).compress)")
    assert "zz" not in lean


def test_the_command_writes_only_a_lean_name() -> None:
    request = forseti.parse("simulate", "m", "")
    for target in ("m) (x", "m\n#eval 1", "m\n", ""):
        with pytest.raises(ValueError, match="is not a Lean name"):
            forseti.command(type(request)("simulate", target, request.options))


# ----- compute: simulate --------------------------------------------------------------


def test_simulate_runs_the_recorded_field_without_asgard(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "gimle.asgard", None)
    before = datetime.now(UTC).replace(microsecond=0)
    body = _simulated()
    assert checked_evidence(body) == body
    series = body["series"]
    assert series["names"] == ["x"] and len(series["times"]) == 401
    assert series["times"][-1] == pytest.approx(4.0)
    assert all(len(row) == 1 for row in series["values"])
    assert series["values"][-1][0] == pytest.approx(0.995**400)
    assert abs(series["values"][-1][0] - math.exp(-2)) < 1e-3
    assert body["finite"] is True and "plane" not in body
    provenance = _provenance(body)
    assert provenance["verb"] == "simulate" and provenance["model"] == "dissipative"
    assert (provenance["solver"], provenance["step"], provenance["steps"]) == (
        "euler",
        "1/100",
        "400",
    )
    assert provenance["arithmetic"] == "float64" and provenance["rounded"] == "1/100"
    expected = hashlib.sha256(
        json.dumps(FIELD["field"], separators=(",", ":")).encode()
    )
    assert provenance["field sha256"] == expected.hexdigest()
    assert provenance["provider"].startswith("glitnir-forseti ")
    assert "asgard" not in provenance
    created = datetime.fromisoformat(provenance["created"])
    assert created.utcoffset().total_seconds() == 0 and created >= before


def test_a_plane_maps_state_names_to_series() -> None:
    field = {
        **FIELD,
        "states": ["state-x", "state-y"],
        "initial": [["1", "1"], ["0", "1"]],
        "field": [["var", "1"], ["neg", ["var", "0"]]],
    }
    body = _simulated("(plane := y, x) (h := 1/10) (steps := 10)", field)
    assert body["series"]["names"] == ["x", "y"] and body["plane"] == [1, 0]
    assert checked_evidence(body) == body
    with pytest.raises(EvidenceError, match="'q', not a state of the model"):
        _simulated("(plane := x, q)", field)


def test_unnamed_states_are_numbered() -> None:
    body = _simulated("(steps := 2)", {k: v for k, v in FIELD.items() if k != "states"})
    assert body["series"]["names"] == ["x0"]
    long = {**FIELD, "states": ["state-" + "x" * 300]}
    assert _simulated("(steps := 2)", long)["series"]["names"] == ["x0"]


def test_long_runs_are_downsampled_to_what_glitnir_keeps() -> None:
    body = _simulated("(steps := 9000)")
    assert 400 < len(body["series"]["times"]) <= evidence.MAX_SAMPLES
    assert checked_evidence(body) == body
    assert len(_simulated("(steps := 3000)")["series"]["times"]) == 3001


def test_a_blow_up_stops_the_run_and_is_flagged() -> None:
    body = _simulated("(h := 1000) (steps := 20000)")
    assert body["finite"] is False
    assert checked_evidence(body) == body
    assert all(math.isfinite(row[0]) for row in body["series"]["values"])


def test_rk4_is_the_declared_solver() -> None:
    body = _simulated("(solver := rk4) (h := 1/10) (steps := 40)")
    assert abs(body["series"]["values"][-1][0] - math.exp(-2)) < 1e-6
    assert _provenance(body)["solver"] == "rk4"


def test_non_decimal_values_are_listed_as_rounded() -> None:
    third = {**FIELD, "field": [["mul", ["const", "-1", "3"], ["var", "0"]]]}
    body = _simulated("(h := 1/100) (steps := 400)", third)
    assert _provenance(body)["rounded"] == "-1/3, 1/100"
    assert body["series"]["values"][-1][0] == pytest.approx((1 - 1 / 300) ** 400)
    assert _provenance(_simulated("(h := 1/64)"))["rounded"] == "none"
    huge = {**FIELD, "field": [["mul", ["const", "1" + "0" * 320, "1"], ["var", "0"]]]}
    with pytest.raises(EvidenceError, match="float64"):
        _simulated("", huge)


def test_the_field_hash_follows_the_emitted_trees() -> None:
    other = {**FIELD, "field": [["mul", ["const", "-1", "3"], ["var", "0"]]]}
    first = _provenance(_simulated())["field sha256"]
    assert _provenance(_simulated(payload=other))["field sha256"] != first


def test_a_run_over_the_work_budget_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(evidence, "MAX_WORK", 100)
    with pytest.raises(EvidenceError, match="more than the 100 allowed"):
        _simulated()


def test_a_model_with_more_states_than_a_card_shows_is_refused() -> None:
    field = {
        "states": [f"state-s{i}" for i in range(17)],
        "initial": [["0", "1"]] * 17,
        "start": ["0", "1"],
        "field": [["var", str(i)] for i in range(17)],
    }
    with pytest.raises(EvidenceError, match="17 states; evidence shows at most 16"):
        _simulated("", field)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([1, 2], "malformed field"),
        ("field", "malformed field"),
        ({"initial": [["1", "1"]], "start": ["0", "1"]}, "has no 'field'"),
        ({**FIELD, "initial": [["2", "4"]]}, "must be reduced"),
        ({**FIELD, "start": "0"}, "numerator, denominator"),
        ({**FIELD, "field": [["pow", ["var", "0"], "2"]]}, "unknown expression"),
    ],
)
def test_malformed_payloads_are_refused(payload, error) -> None:
    with pytest.raises(EvidenceError, match=error):
        _simulated("", payload)


def test_long_provenance_values_are_shortened() -> None:
    body = _simulated("(steps := 1)", {**FIELD, "start": ["1" * 250, "1"]})
    start = _provenance(body)["start"]
    assert len(start) == forseti.MAX_TEXT and start.endswith("…")
    assert checked_evidence(body) == body


def test_times_past_float64_are_refused_before_the_run() -> None:
    request = forseti.parse("simulate", "m", "(steps := 100000)")
    huge = type(request)("simulate", "m", {**request.options, "step": "1" + "0" * 305})
    with pytest.raises(EvidenceError, match="times would exceed float64"):
        forseti.compute(huge, FIELD)


def test_duplicate_state_names_are_numbered() -> None:
    field = {
        **FIELD,
        "states": ["state-x", "state-x"],
        "initial": [["1", "1"], ["0", "1"]],
        "field": [["var", "1"], ["var", "0"]],
    }
    assert _simulated("(steps := 2)", field)["series"]["names"] == ["x0", "x1"]


def test_the_series_and_text_bounds_are_glitnirs() -> None:
    """The bounds are imported from the core, never copied, so they cannot drift."""
    import ast
    import inspect

    from glitnir.providers import outcomes

    assert (forseti.MAX_SERIES, forseti.MAX_TEXT) == (
        outcomes.MAX_SERIES,
        outcomes.MAX_TEXT,
    )
    assigned = {
        target.id
        for node in ast.parse(inspect.getsource(forseti)).body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert not assigned & {"MAX_SERIES", "MAX_TEXT"}


# ----- compute: iterate, against the Asgard stand-in ----------------------------------


def test_the_stand_in_makes_iterate_available(fake_asgard) -> None:
    assert fake_asgard.__version__ == "0.0.0-fake"
    assert forseti.asgard_installed() is True
    assert forseti.unavailable() is None


def test_iterate_runs_the_recorded_circuit_end_to_end(fake_asgard) -> None:
    request = forseti.parse("iterate", "update", HALVING)
    body = forseti.compute(request, UPDATE)
    assert checked_evidence(body) == body
    assert body["series"] == {
        "times": [0.0, 1.0, 2.0, 3.0],
        "values": [[1.0], [0.5], [0.25], [0.125]],
        "names": ["x"],
    }
    provenance = _provenance(body)
    assert provenance["asgard"] == "0.0.0-fake"
    assert provenance["verb"] == "iterate" and provenance["circuit"] == "update"
    assert (provenance["solver"], provenance["step"], provenance["steps"]) == (
        "iterate",
        "1",
        "3",
    )
    expected = hashlib.sha256(
        json.dumps(UPDATE["circuit"], separators=(",", ":")).encode()
    )
    assert provenance["field sha256"] == expected.hexdigest()
    assert provenance["parameter"] == "1/2" and provenance["rounded"] == "none"
    assert provenance["inputs"] == "none"


def test_simulate_does_not_use_the_stand_in(fake_asgard) -> None:
    """The stand-in's decoder returns 1/2 for everything; simulate reads its own."""
    body = _simulated()
    assert checked_evidence(body) == body
    assert body["series"]["values"][0] == [1.0]
    assert "asgard" not in _provenance(body)


def test_ports_that_do_not_match_the_directive_are_refused(fake_asgard) -> None:
    request = forseti.parse("iterate", "update", "(state := 1, 1) (parameter := 1)")
    with pytest.raises(EvidenceError, match="maps 2 ports to 1, not 3"):
        forseti.compute(request, UPDATE)


@pytest.mark.parametrize(
    ("degree", "words"), [(1, "maps 1 port to 2,"), (3, "maps 3 ports to 2,")]
)
def test_the_port_mismatch_message_counts_ports(
    fake_asgard, monkeypatch, degree: int, words: str
) -> None:
    """One port is singular, any other number plural."""
    stub = SimpleNamespace(input_degree=degree, output_degree=2)
    monkeypatch.setattr(evidence, "exact_polynomials", lambda _node: stub)
    request = forseti.parse("iterate", "update", "(state := 1) (parameter := 1)")
    with pytest.raises(EvidenceError, match=words):
        forseti.compute(request, UPDATE)


def test_a_decode_error_is_an_evidence_error(fake_asgard) -> None:
    request = forseti.parse("iterate", "update", HALVING)
    with pytest.raises(EvidenceError, match="fake: unreadable circuit"):
        forseti.compute(request, {"circuit": "not a tree"})
    with pytest.raises(EvidenceError, match="could not read the circuit"):
        forseti.compute(request, {"inputs": 2})


NOTEBOOK = (
    "def update := 1\n"
    f"-- @evidence iterate update {HALVING}\n"
    "-- @evidence simulate dissipative (h := 1/100) (steps := 400)\n"
)


def _service(tmp_path, monkeypatch, payload: str):
    """A preview of `NOTEBOOK` whose Lean runner prints `payload`."""
    from glitnir.preview import snapshot as preview_snapshot
    from glitnir.service import preview as source_preview

    simulate, iterate = forseti.evidence_providers()
    monkeypatch.setattr(
        source_preview,
        "load_providers",
        lambda: Providers({}, {"simulate": simulate, "iterate": iterate}, {}, {}),
    )
    for module in (source_preview, preview_snapshot):
        monkeypatch.setattr(
            module, "environment_fingerprint", lambda *a: {"sha256": "f" * 64}
        )
    monkeypatch.setattr(
        source_preview, "run_provider_lean", lambda source, **o: {"payload": payload}
    )
    path = tmp_path / "Notebook.lean"
    path.write_text(NOTEBOOK)
    return source_preview.SourcePreviewService(path)


def _run_card(service, line: int) -> dict:
    sha = hashlib.sha256(NOTEBOOK.encode()).hexdigest()
    service.run_evidence(sha, line)
    service._evidence_worker.join(timeout=30)
    assert not service._evidence_worker.is_alive()
    return next(c for c in service.snapshot()["evidence"] if c["line"] == line)


def test_without_asgard_iterate_is_unavailable_and_never_runs(
    tmp_path, monkeypatch
) -> None:
    """The card names what is missing, the run is refused, simulate still runs."""
    from glitnir.lean.project_snapshot import SnapshotError

    monkeypatch.setattr(forseti, "asgard_installed", lambda: False)
    service = _service(tmp_path, monkeypatch, json.dumps(FIELD))
    try:
        snapshot = service.snapshot()
        iterate, simulate = snapshot["evidence"]
        assert (iterate["error"], iterate["unavailable"]) == ("", forseti.NEEDS_ASGARD)
        assert (simulate["error"], simulate["unavailable"]) == ("", None)
        assert snapshot["directive_unavailable"]["evidence"] == {
            "iterate": forseti.NEEDS_ASGARD
        }
        sha = hashlib.sha256(NOTEBOOK.encode()).hexdigest()
        with pytest.raises(SnapshotError, match="needs Python Asgard"):
            service.run_evidence(sha, 2)
        assert _run_card(service, 3)["status"] == "current"
    finally:
        service.close()


def test_glitnir_reports_iterate_as_unavailable_without_asgard(monkeypatch) -> None:
    """The provider report the editor shows lists iterate with its reason."""
    monkeypatch.setattr(forseti, "asgard_installed", lambda: False)
    found = load_providers()
    assert found.evidence["simulate"].unavailable() is None
    assert found.evidence["iterate"].unavailable() == forseti.NEEDS_ASGARD


def test_a_decode_rational_error_becomes_the_card_run_error(
    tmp_path, monkeypatch, fake_asgard
) -> None:
    service = _service(tmp_path, monkeypatch, '{"circuit": "not a tree"}')
    try:
        card = _run_card(service, 2)
    finally:
        service.close()
    assert (card["provider"], card["error"], card["unavailable"]) == (
        "forseti",
        "",
        None,
    )
    # A refusal: shown, and saved as the run's failure.
    assert (card["status"], card["snapshot"]) == ("failed", None)
    assert card["run_error"] == (
        "Python Asgard could not read the circuit: fake: unreadable circuit"
    )
    [failure] = saved_evidence_failures(load_records(service.path)).values()
    assert (failure["kind"], failure["message"]) == ("refused", card["run_error"])


def test_asgard_missing_at_run_time_is_shown_but_never_saved(
    tmp_path, monkeypatch
) -> None:
    """Removed or broken since the card was read: a setup failure, not saved."""
    monkeypatch.setattr(forseti, "asgard_installed", lambda: True)
    monkeypatch.setitem(sys.modules, "gimle.asgard", None)
    service = _service(tmp_path, monkeypatch, json.dumps(UPDATE))
    try:
        card = _run_card(service, 2)
    finally:
        service.close()
    assert card["run_error"].startswith(
        f"Provider forseti failed: ImportError: {forseti.NEEDS_ASGARD}"
    )
    assert card["status"] == "never"
    assert saved_evidence_failures(load_records(service.path)) == {}


@pytest.mark.parametrize(
    ("line", "payload", "last"),
    [(2, json.dumps(UPDATE), 0.125), (3, json.dumps(FIELD), 0.995**400)],
    ids=["iterate", "simulate"],
)
def test_runs_from_the_preview_save_a_current_snapshot(
    tmp_path, monkeypatch, fake_asgard, line, payload, last
) -> None:
    service = _service(tmp_path, monkeypatch, payload)
    try:
        card = _run_card(service, line)
    finally:
        service.close()
    assert (card["status"], card["run_error"]) == ("current", None)
    assert card["snapshot"]["series"]["values"][-1][0] == pytest.approx(last)


# ----- compute: iterate, against Python Asgard ----------------------------------------


@pytest.mark.simulation
def test_iterate_runs_the_pi_windup_update() -> None:
    import gimle.asgard

    request = forseti.parse("iterate", "update", WINDUP_LINE.split("update", 1)[1])
    body = forseti.compute(request, UPDATE)
    assert checked_evidence(body) == body
    assert body["series"]["names"] == ["x", "q"] and body["finite"] is True
    assert body["series"]["times"][-1] == 200
    assert max(abs(row[1]) for row in body["series"]["values"]) < 28
    assert _provenance(body)["asgard"] == str(gimle.asgard.__version__)


@pytest.mark.simulation
def test_python_asgard_refuses_ports_that_do_not_match() -> None:
    options = WINDUP_LINE.split("update", 1)[1]
    options = options.replace("(parameter := 1/4)", "(parameter := 1/4, 1)")
    options = options.replace(" (names := x, q, r, d, θ)", "")
    request = forseti.parse("iterate", "update", options)
    with pytest.raises(EvidenceError, match="maps 5 ports to 2, not 6"):
        forseti.compute(request, UPDATE)
