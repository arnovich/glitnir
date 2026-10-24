"""Providers load from entry points; a broken one never hides the others."""

import dataclasses
from types import SimpleNamespace

import pytest

from glitnir import providers
from glitnir.providers.outcomes import checked_drawing


def _provider(name, kinds):
    return providers.ViewProvider(
        name=name,
        kinds=frozenset(kinds),
        imports=("Demo.View",),
        preamble="",
        marker="demoView:",
        command=lambda kind, target, using: f"#demo_view {kind} {target}",
    )


def _entry(name, load):
    return SimpleNamespace(name=name, load=load)


def test_providers_are_read_by_kind(monkeypatch):
    good = _entry("demo", lambda: lambda: [_provider("demo", {"circuit"})])
    monkeypatch.setattr(providers, "_entry_points", lambda: [good])
    kinds, _, _, errors = providers.load_providers()
    assert set(kinds) == {"circuit"} and kinds["circuit"].name == "demo"
    assert errors == {}


def test_a_broken_provider_is_reported_not_raised(monkeypatch):
    def broken():
        raise ImportError("missing dependency")

    good = _entry("demo", lambda: lambda: [_provider("demo", {"model"})])
    monkeypatch.setattr(
        providers, "_entry_points", lambda: [_entry("bad", broken), good]
    )
    kinds, _, _, errors = providers.load_providers()
    assert set(kinds) == {"model"}
    assert "missing dependency" in errors["bad"]


def test_drawings_are_validated():
    assert checked_drawing("<svg xmlns='http://www.w3.org/2000/svg'/>")["svg"]
    assert "error" in checked_drawing("<html/>")
    assert "error" in checked_drawing("<svg>" + "x" * 1_000_001 + "</svg>")
    report = checked_drawing('{"svg": "<svg/>", "graph": {}, "script": "x"}')
    assert set(report) == {"svg", "graph"}


def test_malformed_provider_results_are_reported_not_raised(monkeypatch):
    bad_kinds = providers.ViewProvider(
        name="odd",
        kinds=42,  # type: ignore[arg-type]
        imports=(),
        preamble="",
        marker="odd:",
        command=lambda kind, target, using: "",
    )
    entries = [
        _entry("scalar", lambda: lambda: 7),
        _entry("stranger", lambda: lambda: ["not a provider"]),
        _entry("empty", lambda: lambda: []),
        _entry("odd", lambda: lambda: [bad_kinds]),
        _entry("demo", lambda: lambda: [_provider("demo", {"circuit"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert set(kinds) == {"circuit"} and kinds["circuit"].name == "demo"
    assert set(errors) == {"scalar", "stranger", "empty", "odd"}
    assert "non-provider" in errors["stranger"]
    assert "no providers" in errors["empty"]


def test_empty_marker_and_string_kinds_are_rejected(monkeypatch):
    def variant(**changes):
        return dataclasses.replace(_provider("odd", {"circuit"}), **changes)

    entries = [
        _entry("nomarker", lambda: lambda: [variant(marker="")]),
        _entry("strkinds", lambda: lambda: [variant(kinds="circuit")]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert kinds == {} and set(errors) == {"nomarker", "strkinds"}


def test_a_provider_calling_exit_is_reported_not_raised(monkeypatch):
    def leaving():
        raise SystemExit(3)

    entries = [
        _entry("leaves", leaving),
        _entry("demo", lambda: lambda: [_provider("demo", {"circuit"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert set(kinds) == {"circuit"} and "SystemExit" in errors["leaves"]


def test_malformed_provider_fields_are_rejected(monkeypatch):
    def variant(**changes):
        return dataclasses.replace(_provider("odd", {"circuit"}), **changes)

    entries = [
        _entry("strimports", lambda: lambda: [variant(imports="Demo.View")]),
        _entry("listimports", lambda: lambda: [variant(imports=["Demo.View"])]),
        _entry("badimport", lambda: lambda: [variant(imports=("Demo", 3))]),
        _entry("preamble", lambda: lambda: [variant(preamble=None)]),
        _entry("command", lambda: lambda: [variant(command="#demo")]),
        _entry("demo", lambda: lambda: [_provider("demo", {"model"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert set(kinds) == {"model"}
    assert set(errors) == {
        "strimports",
        "listimports",
        "badimport",
        "preamble",
        "command",
    }


def test_a_kind_clash_keeps_the_first_provider_and_is_reported(monkeypatch):
    entries = [
        _entry("first", lambda: lambda: [_provider("first", {"circuit"})]),
        _entry("second", lambda: lambda: [_provider("second", {"circuit", "model"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert kinds["circuit"].name == "first" and kinds["model"].name == "second"
    assert set(errors) == {"second"}
    assert "circuit" in errors["second"] and "first" in errors["second"]


def test_a_kind_clash_within_one_entry_is_reported(monkeypatch):
    entries = [
        _entry(
            "pair",
            lambda: lambda: [_provider("a", {"circuit"}), _provider("b", {"circuit"})],
        ),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    kinds, _, _, errors = providers.load_providers()
    assert kinds["circuit"].name == "a"
    assert "circuit" in errors["pair"] and "a" in errors["pair"]


def _evidence(name, verbs, **changes):
    provider = providers.EvidenceProvider(
        name=name,
        verbs=frozenset(verbs),
        imports=("Demo.Evidence",),
        preamble="",
        marker="demoEvidence:",
        parse=lambda verb, target, options: providers.EvidenceRequest(verb, target, {}),
        command=lambda request: f"#demo_evidence {request.target}",
        compute=lambda request, payload: {},
        unavailable=lambda: None,
    )
    return dataclasses.replace(provider, **changes)


def test_one_entry_point_may_return_views_and_evidence(monkeypatch):
    mixed = _entry(
        "mixed",
        lambda: lambda: [_provider("draw", {"circuit"}), _evidence("run", {"go"})],
    )
    monkeypatch.setattr(providers, "_entry_points", lambda: [mixed])
    loaded = providers.load_providers()
    assert loaded.views["circuit"].name == "draw"
    assert loaded.evidence["go"].name == "run"
    assert loaded.errors == {}


def test_a_view_kind_and_an_evidence_verb_may_share_a_word(monkeypatch):
    entries = [
        _entry("draw", lambda: lambda: [_provider("draw", {"simulate"})]),
        _entry("run", lambda: lambda: [_evidence("run", {"simulate"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert set(loaded.views) == set(loaded.evidence) == {"simulate"}
    assert loaded.errors == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"marker": ""},
        {"verbs": "simulate"},
        {"imports": "Demo.Evidence"},
        {"imports": ("Demo", 3)},
        {"preamble": None},
        {"parse": None},
        {"command": "#demo"},
        {"compute": {}},
        {"unavailable": "missing"},
    ],
)
def test_malformed_evidence_providers_are_rejected(monkeypatch, changes):
    entries = [
        _entry("bad", lambda: lambda: [_evidence("bad", {"go"}, **changes)]),
        _entry("demo", lambda: lambda: [_evidence("demo", {"other"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert set(loaded.evidence) == {"other"} and set(loaded.errors) == {"bad"}


def test_a_bad_evidence_provider_drops_its_whole_entry(monkeypatch):
    entry = _entry(
        "mixed",
        lambda: lambda: [
            _provider("draw", {"circuit"}),
            _evidence("run", {"go"}, marker=""),
        ],
    )
    monkeypatch.setattr(providers, "_entry_points", lambda: [entry])
    loaded = providers.load_providers()
    assert loaded.views == {} and loaded.evidence == {}
    assert "empty marker" in loaded.errors["mixed"]


def test_a_verb_clash_keeps_the_first_provider_and_is_reported(monkeypatch):
    entries = [
        _entry("first", lambda: lambda: [_evidence("first", {"simulate"})]),
        _entry(
            "second",
            lambda: lambda: [
                _evidence("second", {"simulate", "iterate"}),
                _evidence("third", {"iterate"}),
            ],
        ),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert loaded.evidence["simulate"].name == "first"
    assert loaded.evidence["iterate"].name == "second"
    assert loaded.errors == {
        "second": "Verb `iterate` is already run by provider second."
        " Verb `simulate` is already run by provider first."
    }


def _decision(name, kinds, **changes):
    provider = providers.DecisionProvider(
        name=name,
        kinds=frozenset(kinds),
        imports=("Demo.Decide",),
        parse=lambda name, kind, goal: providers.DecisionRequest(name, kind, goal, {}),
        decide=lambda request, cancel: {},
        unavailable=lambda: None,
    )
    return dataclasses.replace(provider, **changes)


def test_decision_providers_are_read_by_kind(monkeypatch):
    entry = _entry(
        "mixed",
        lambda: lambda: [
            _provider("draw", {"circuit"}),
            _evidence("run", {"simulate"}),
            _decision("decide", {"entailment", "stream"}),
        ],
    )
    monkeypatch.setattr(providers, "_entry_points", lambda: [entry])
    loaded = providers.load_providers()
    assert set(loaded.decisions) == {"entailment", "stream"}
    assert loaded.decisions["stream"].name == "decide"
    assert set(loaded.views) == {"circuit"} and set(loaded.evidence) == {"simulate"}
    assert loaded.errors == {}


def test_a_decision_kind_never_clashes_with_a_view_kind_or_an_evidence_verb(
    monkeypatch,
):
    entries = [
        _entry("draw", lambda: lambda: [_provider("draw", {"stream"})]),
        _entry("run", lambda: lambda: [_evidence("run", {"stream"})]),
        _entry("decide", lambda: lambda: [_decision("decide", {"stream"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert set(loaded.views) == set(loaded.evidence) == set(loaded.decisions)
    assert loaded.decisions["stream"].name == "decide"
    assert loaded.errors == {}


def test_a_decision_kind_clash_keeps_the_first_provider(monkeypatch):
    entries = [
        _entry("first", lambda: lambda: [_decision("first", {"entailment"})]),
        _entry(
            "second",
            lambda: lambda: [
                _decision("second", {"entailment", "stream"}),
                _decision("third", {"stream"}),
            ],
        ),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert loaded.decisions["entailment"].name == "first"
    assert loaded.decisions["stream"].name == "second"
    assert loaded.errors == {
        "second": "Kind `stream` is already decided by provider second."
        " Kind `entailment` is already decided by provider first."
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"kinds": "entailment"},
        {"imports": "Demo.Decide"},
        {"imports": ("Demo", 3)},
        {"parse": None},
        {"decide": "z3"},
        {"unavailable": "missing"},
    ],
)
def test_malformed_decision_providers_are_rejected(monkeypatch, changes):
    entries = [
        _entry("bad", lambda: lambda: [_decision("bad", {"go"}, **changes)]),
        _entry("demo", lambda: lambda: [_decision("demo", {"other"})]),
    ]
    monkeypatch.setattr(providers, "_entry_points", lambda: entries)
    loaded = providers.load_providers()
    assert set(loaded.decisions) == {"other"} and set(loaded.errors) == {"bad"}


def test_outcomes_module_owns_provider_output_validation() -> None:
    from glitnir.providers import outcomes

    svg = '<svg xmlns="http://www.w3.org/2000/svg"/>'
    assert outcomes.checked_drawing(svg) == {"svg": svg}
    with pytest.raises(ValueError, match="Evidence"):
        outcomes.checked_evidence({})
    with pytest.raises(ValueError, match="status must be"):
        outcomes.checked_decision({"status": "nonsense"})
    assert outcomes.MAX_SAMPLES > 0 and outcomes.MAX_BLOCK_BYTES > 0
