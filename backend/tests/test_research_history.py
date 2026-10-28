"""Taskvalet history stays evidence-bound, bounded and observational."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from glitnir.notebook.document import NotebookError
from glitnir.service import research_history
from glitnir.service.research_history import (
    FORMAT,
    MAX_ROUNDS,
    ResearchHistoryError,
    TaskvaletReader,
    research_links,
)

PROBLEM = "00000000-0000-4000-8000-000000000003"
ROUND = "00000000-0000-4000-8000-000000000010"
NEXT = "00000000-0000-4000-8000-000000000011"
FINDING = "00000000-0000-4000-8000-000000000020"
ASSIGNMENT = "00000000-0000-4000-8000-000000000030"
PROPOSAL = "00000000-0000-4000-8000-000000000040"
JOB = "00000000-0000-4000-8000-000000000050"
SOURCE = "theorem target : True := by\n  trivial\n"


def _binding(**changes):
    source = "theorem target : True := by\n  trivial\n"
    value = {
        "claim": "Lab.target",
        "project": "lab",
        "problem": PROBLEM,
        "problem_version": 1,
        "declaration": "Lab.target",
        "type": "True",
        "context_sha256": hashlib.sha256(source.rstrip("\n").encode()).hexdigest(),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "environment": {
            "toolchain": "leanprover/lean4:v4.35.0",
            "release_sha256": "d" * 64,
            "fingerprint": "e" * 64,
            "check": "c" * 64,
        },
        "axiom_policy": ["Classical.choice", "Quot.sound", "propext"],
    }
    value.update(changes)
    return source, value


def _write_links(path: Path, links: list[dict]) -> None:
    path.with_suffix(".research.json").write_text(
        json.dumps({"format": FORMAT, "links": links})
    )


def _write_records(path: Path, source: str, claims: list[dict]) -> None:
    path.with_suffix(".glitnir.json").write_text(
        json.dumps(
            {
                "format": "glitnir.project-checks/v2",
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "environment": {"fingerprint": "e" * 64, "check": "c" * 64},
                "withheld": None,
                "claims": claims,
            }
        )
    )


def _claim_record(name: str, kind: str, prefix: str, first: int = 1, last: int = 2):
    return {
        "name": name,
        "status": "checked",
        "axioms": [],
        "start_line": first,
        "end_line": last,
        "declaration_sha256": "a" * 64,
        "prefix_sha256": prefix,
        "proposition_sha256": "b" * 64,
        "type": kind,
        "type_sha256": hashlib.sha256(kind.encode()).hexdigest(),
    }


def test_links_bind_exact_type_context_and_saved_source(tmp_path: Path) -> None:
    source, value = _binding()
    notebook = tmp_path / "Lab.lean"
    notebook.write_text(source)
    _write_records(
        notebook, source, [_claim_record("Lab.target", "True", value["context_sha256"])]
    )
    _write_links(notebook, [value])
    claim = {
        "name": "Lab.target",
        "start_line": 1,
        "end_line": 2,
        "status": "checked",
        "axioms": [],
        "current": True,
    }
    links = research_links(notebook, source, [claim])
    assert links["Lab.target"]["stale_reasons"] == ()
    _write_links(notebook, [{**value, "type": "False", "source_sha256": "f" * 64}])
    changed = research_links(notebook, source, [claim])["Lab.target"]
    assert changed["stale_reasons"] == (
        "The claim type changed",
        "The notebook source changed",
    )
    with pytest.raises(NotebookError, match="fully qualified"):
        _write_links(notebook, [{**value, "declaration": "target"}])
        research_links(notebook, source, [claim])
    with pytest.raises(NotebookError, match="worker environment"):
        _write_links(
            notebook,
            [{**value, "environment": {"fingerprint": "e" * 64, "check": "c" * 64}}],
        )
        research_links(notebook, source, [claim])


def test_links_expose_only_current_environment_bound_glitnir_checks(
    tmp_path: Path,
) -> None:
    source, value = _binding()
    source = "theorem helper : True := by\n  trivial\n" + source
    notebook = tmp_path / "Lab.lean"
    notebook.write_text(source)
    helper_prefix = hashlib.sha256(
        source.split("theorem target")[0].rstrip("\n").encode()
    ).hexdigest()
    target_prefix = hashlib.sha256(source.rstrip("\n").encode()).hexdigest()
    value["context_sha256"] = target_prefix
    value["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    target = _claim_record("Lab.target", "True", target_prefix, 3, 4)
    target["axioms"] = [{"name": "propext", "module": "Init.Classical"}]
    target["uses"] = [{"module": "M" * 500_000, "name": "large", "in_notebook": False}]
    _write_records(
        notebook, source, [_claim_record("Lab.helper", "True", helper_prefix), target]
    )
    _write_links(notebook, [value])
    views = [
        {
            "name": "Lab.helper",
            "start_line": 1,
            "end_line": 2,
            "status": "checked",
            "axioms": [],
            "current": True,
        },
        {
            "name": "Lab.target",
            "start_line": 3,
            "end_line": 4,
            "status": "checked",
            "axioms": target["axioms"],
            "current": True,
        },
    ]
    links = research_links(notebook, source, views)
    independent = links["Lab.target"]["independent_checks"]
    assert len(independent) == 1
    assert independent[0]["declaration"] == "Lab.target"
    assert independent[0]["record"] == {
        "status": "checked",
        "type_sha256": hashlib.sha256(b"True").hexdigest(),
        "prefix_sha256": target_prefix,
    }
    assert independent[0]["external_axioms"] == ["propext"]


def _bundle(with_type=True):
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    theorem = {"name": "Lab.target", "status": "checked", "axioms": []}
    if with_type:
        theorem["type"] = "True"
    value = {
        "format": "taskvalet.lean-lemma/v1",
        "origin": {
            "server": "https://taskvalet.example",
            "project": "lab",
            "storage_id": "sqlite:test",
            "actor": "agent/producer",
            "run_id": "00000000-0000-4000-8000-000000000060",
            "task": "001-proof",
            "problem": PROBLEM,
            "problem_version": 1,
            "assignment": ASSIGNMENT,
            "statement": "Prove the target.",
            "assumptions": "Lean standard axioms only.",
        },
        "job": {
            "id": JOB,
            "project": "lab",
            "state": "completed",
            "phase": None,
            "created": 1.0,
            "deadline": 61.0,
            "request": {
                "request_id": JOB,
                "kind": "glitnir.check/v1",
                "environment": "lean-project",
                "checker_sha256": "a" * 64,
                "source": SOURCE,
                "source_sha256": digest,
                "context_sha256": _binding()[1]["context_sha256"],
                "path": "Lab.lean",
                "through_line": None,
                "timeout_seconds": 60,
                "memory_mb": 4096,
            },
            "worker": "agent/checker",
            "result": {
                "status": "checked_snapshot",
                "source_sha256": digest,
                "toolchain": "leanprover/lean4:v4.35.0",
                "release_sha256": "d" * 64,
                "module": "Lab",
                "withheld": None,
                "theorems": [theorem],
            },
        },
        "declarations": ["Lab.target"],
    }
    value["sha256"] = hashlib.sha256(
        __import__("json")
        .dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        .encode()
    ).hexdigest()
    return value


def _record(kind="proof_candidate", text=None):
    return {
        "id": FINDING,
        "kind": "finding",
        "version": 1,
        "problem": PROBLEM,
        "problem_version": 1,
        "author": "agent/producer",
        "at": "2026-10-28T12:00:00Z",
        "data": {
            "kind": kind,
            "text": text if text is not None else __import__("json").dumps(_bundle()),
            "assignment": ASSIGNMENT,
            "artifacts": [],
        },
    }


def _round(identity, parent=None, finding=None, status="review"):
    finding = finding or _record()
    return {
        "id": identity,
        "created_at": "2026-10-28T12:00:00Z" if not parent else "2026-10-28T13:00:00Z",
        "status": status,
        "plan": {
            "problem": PROBLEM,
            "problem_version": 1,
            "approach": "First principles" if not parent else "Deepen the checked path",
            "instructions": "Keep failed cases visible.",
            "parent_round": parent,
        },
        "handoff": {
            "context_inputs": [
                {
                    "record": finding,
                    "finding": finding,
                    "checks": [],
                    "originating_assignment": {
                        "id": ASSIGNMENT,
                        "data": {"task": "001-proof"},
                    },
                    "assignment": {
                        "id": ASSIGNMENT,
                        "data": {"task": "001-proof"},
                    },
                    "originating_task": "001-proof",
                    "artifacts": [],
                    "reason": "Carry exact evidence forward",
                }
            ]
        },
        "campaign": {
            "participants": [
                {
                    "role": "researcher",
                    "task": "001-proof",
                    "worker": "agent/producer",
                    "assignment": ASSIGNMENT,
                    "status": "complete",
                    "findings": [FINDING],
                }
            ]
        },
    }


def _responses(rounds=None, finding=None):
    rounds = rounds or [
        _round(ROUND, finding=finding),
        _round(NEXT, ROUND, finding=finding),
    ]
    responses = {
        f"/api/v1/projects/lab/research/{PROBLEM}": {
            "problem": {
                "id": PROBLEM,
                "version": 1,
                "data": {
                    "title": "Target",
                    "statement": "Prove the target.",
                    "assumptions": "Lean standard axioms only.",
                },
            },
            "assignments": [
                {
                    "id": ASSIGNMENT,
                    "problem_version": 1,
                    "data": {"task": "001-proof", "title": "Try induction."},
                }
            ],
        },
        f"/api/v1/projects/lab/rounds?problem={PROBLEM}": {"rounds": rounds},
        "/api/v1/projects/lab/round-proposals": {
            "proposals": [{"id": PROPOSAL, "source_round": ROUND}]
        },
        f"/api/v1/projects/lab/round-proposals/{PROPOSAL}": {
            "proposal": {
                "action": "deepen",
                "assessment": "partial_progress",
                "rationale": "The exact replay closed the central case.",
                "obstacles": ["Boundary case remains."],
                "uncertainty": "The target is not complete.",
                "allocations": [{"task": "001-proof", "mode": "deepen"}],
                "checker": {"task": "002-check"},
                "exploration_percent": 25,
            }
        },
    }
    for round_ in rounds:
        for item in round_["handoff"]["context_inputs"]:
            try:
                bundle = __import__("json").loads(item["finding"]["data"]["text"])
                job = bundle["job"]
                responses[f"/api/v1/projects/lab/compute/jobs/{job['id']}"] = job
            except (KeyError, TypeError, __import__("json").JSONDecodeError):
                pass
    return responses


def _reader(responses):
    return TaskvaletReader("https://taskvalet.example", "token", responses.__getitem__)


def test_two_round_history_keeps_decisions_assignments_and_exact_check_receipt() -> (
    None
):
    _, binding = _binding()
    view = _reader(_responses()).investigation({**binding, "stale_reasons": ()})
    assert view["state"] == "ready"
    assert [round_["id"] for round_ in view["rounds"]] == [ROUND, NEXT]
    first = view["rounds"][0]
    assert first["participants"][0]["assignment_title"] == "Try induction."
    assert first["decision"]["obstacles"] == ["Boundary case remains."]
    evidence = first["evidence"][0]
    assert evidence["category"] == "reported_target_check"
    assert evidence["receipt"]["independent_replay"] is False
    assert evidence["originating_task"] == "001-proof"
    assert view["proof_authority"] is False


@pytest.mark.parametrize(
    ("finding", "category"),
    [
        (_record("dead_end", "The boundary fails."), "failed_approach"),
        (_record("counterexample", "n = 0 refutes it."), "refutation"),
        (
            _record("proof_candidate", __import__("json").dumps(_bundle(False))),
            "stale_or_unavailable",
        ),
    ],
)
def test_negative_and_incomplete_evidence_remains_distinct(finding, category) -> None:
    _, binding = _binding()
    view = _reader(_responses(finding=finding)).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["rounds"][0]["evidence"][0]["category"] == category


def test_exact_helper_check_is_distinct_from_the_target() -> None:
    bundle = _bundle()
    theorem = bundle["job"]["result"]["theorems"][0]
    theorem.update(name="Lab.helper", type="False → True")
    bundle["declarations"] = ["Lab.helper"]
    bundle["sha256"] = hashlib.sha256(
        __import__("json")
        .dumps(
            {key: value for key, value in bundle.items() if key != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        .encode()
    ).hexdigest()
    finding = _record("proof_candidate", __import__("json").dumps(bundle))
    _, binding = _binding()
    view = _reader(_responses(finding=finding)).investigation(
        {
            **binding,
            "local_checks": (
                {
                    "claim": "Lab.helper",
                    "declaration": "Lab.helper",
                    "type": "False → True",
                    "context_sha256": binding["context_sha256"],
                    "source_sha256": binding["source_sha256"],
                },
            ),
            "stale_reasons": (),
        }
    )
    assert view["rounds"][0]["evidence"][0]["category"] == "reported_helper_check"


def test_current_glitnir_replay_upgrades_the_exact_target_only() -> None:
    bundle = _bundle()
    source_sha256 = bundle["job"]["request"]["source_sha256"]
    _, binding = _binding()
    context_sha256 = binding["context_sha256"]
    independent = {
        "claim": "Lab.target",
        "declaration": "Lab.target",
        "type": "True",
        "context_sha256": context_sha256,
        "source_sha256": source_sha256,
        "record": {
            "name": "Lab.target",
            "status": "checked",
            "type": "True",
            "type_sha256": hashlib.sha256(b"True").hexdigest(),
            "prefix_sha256": context_sha256,
        },
        "environment": {"fingerprint": "e" * 64, "check": "c" * 64},
        "external_axioms": [],
    }
    view = _reader(_responses()).investigation(
        {
            **binding,
            "source_sha256": source_sha256,
            "independent_checks": (independent,),
            "stale_reasons": (),
        }
    )
    evidence = view["rounds"][0]["evidence"][0]
    assert evidence["category"] == "checked_target"
    assert evidence["receipt"]["independent_replay"] is True

    stale = _reader(_responses()).investigation(
        {
            **binding,
            "source_sha256": source_sha256,
            "independent_checks": (independent,),
            "stale_reasons": ("The notebook source changed",),
        }
    )
    assert stale["rounds"][0]["evidence"][0]["category"] == "stale_or_unavailable"


@pytest.mark.parametrize("changed", ["source", "context"])
def test_reported_target_check_requires_the_linked_source_and_context(changed) -> None:
    bundle = _bundle()
    request = bundle["job"]["request"]
    if changed == "source":
        request["source"] += "\n-- another saved source"
        request["source_sha256"] = hashlib.sha256(
            request["source"].encode()
        ).hexdigest()
        bundle["job"]["result"]["source_sha256"] = request["source_sha256"]
    else:
        request["context_sha256"] = "f" * 64
    bundle["sha256"] = hashlib.sha256(
        __import__("json")
        .dumps(
            {key: item for key, item in bundle.items() if key != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        .encode()
    ).hexdigest()
    finding = _record("proof_candidate", __import__("json").dumps(bundle))
    _, binding = _binding()
    view = _reader(_responses(finding=finding)).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["rounds"][0]["evidence"][0]["category"] == "stale_or_unavailable"


def test_worker_report_cannot_invent_a_verified_dependency_axiom() -> None:
    bundle = _bundle()
    bundle["job"]["result"]["theorems"][0]["axioms"] = [
        "forsetiClaim_not_a_verified_dependency"
    ]
    bundle["sha256"] = hashlib.sha256(
        __import__("json")
        .dumps(
            {key: item for key, item in bundle.items() if key != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        .encode()
    ).hexdigest()
    finding = _record("proof_candidate", __import__("json").dumps(bundle))
    _, binding = _binding()
    view = _reader(_responses(finding=finding)).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["rounds"][0]["evidence"][0]["category"] == "stale_or_unavailable"


@pytest.mark.parametrize(
    ("method", "text", "category"),
    [
        ("numerical", "candidate", "numerical_evidence"),
        ("lean", "not a receipt", "inconclusive"),
    ],
)
def test_consumer_checks_keep_numerical_and_inconclusive_results_distinct(
    method, text, category
) -> None:
    round_ = _round(ROUND, finding=_record("proof_candidate", text))
    round_["handoff"]["context_inputs"][0]["checks"] = [
        {
            "id": str(UUID(int=99)),
            "data": {
                "assignment": ASSIGNMENT,
                "method": method,
                "verdict": "inconclusive",
                "text": "The check did not establish the target.",
            },
        }
    ]
    _, binding = _binding()
    view = _reader(_responses(rounds=[round_])).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["rounds"][0]["evidence"][0]["category"] == category


def test_selected_verification_keeps_its_verdict_and_source_supersession() -> None:
    finding = _record()
    verification = {
        "id": str(UUID(int=98)),
        "kind": "verification",
        "version": 1,
        "problem": PROBLEM,
        "problem_version": 1,
        "author": "agent/reviewer",
        "at": "2026-10-28T12:30:00Z",
        "data": {
            "assignment": ASSIGNMENT,
            "method": "lean",
            "verdict": "inconclusive",
            "text": "The replay did not establish the target.",
        },
    }
    round_ = _round(ROUND, finding=finding)
    selected = round_["handoff"]["context_inputs"][0]
    selected["record"] = verification
    selected["assignment"] = {
        "id": str(UUID(int=31)),
        "data": {"task": "002-review"},
    }
    responses = _responses(rounds=[round_])
    responses[f"/api/v1/projects/lab/research/{PROBLEM}"]["findings"] = [
        {"id": str(UUID(int=97)), "version": 1, "data": {"supersedes": FINDING}}
    ]
    _, binding = _binding()
    view = _reader(responses).investigation({**binding, "stale_reasons": ()})
    evidence = view["rounds"][0]["evidence"][0]
    assert evidence["superseded"] is True
    assert evidence["category"] == "stale_or_unavailable"
    assert evidence["author"] == "agent/reviewer"
    assert evidence["task"] == "002-review"
    assert evidence["originating_finding"]["author"] == "agent/producer"
    assert evidence["originating_finding"]["task"] == "001-proof"

    responses[f"/api/v1/projects/lab/research/{PROBLEM}"]["findings"] = []
    current = _reader(responses).investigation({**binding, "stale_reasons": ()})
    assert current["rounds"][0]["evidence"][0]["category"] == "inconclusive"


def test_changed_problem_and_large_history_are_explicit_and_bounded() -> None:
    _, binding = _binding()
    rounds = [_round(str(UUID(int=index + 1))) for index in range(MAX_ROUNDS + 7)]
    responses = _responses(rounds=rounds)
    research = responses[f"/api/v1/projects/lab/research/{PROBLEM}"]
    research["history"] = [research["problem"]]
    research["problem"] = {
        **research["problem"],
        "version": 2,
        "data": {**research["problem"]["data"], "assumptions": "A revised problem."},
    }
    view = _reader(responses).investigation(
        {**binding, "stale_reasons": ("source changed",)}
    )
    assert view["state"] == "stale"
    assert "source changed" in view["reason"]
    assert "problem has a newer version" in view["reason"]
    assert len(view["rounds"]) == MAX_ROUNDS
    assert view["truncated_rounds"] == 7


def test_missing_configuration_and_partial_proposals_are_recoverable() -> None:
    _, binding = _binding()
    assert (
        TaskvaletReader(base_url="", token="").investigation(binding)["state"]
        == "unavailable"
    )
    responses = _responses()

    def broken(path):
        if path.endswith("round-proposals"):
            raise ResearchHistoryError("offline")
        return responses[path]

    view = TaskvaletReader("https://taskvalet.example", "token", broken).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["state"] == "partial"
    assert view["partial"] == ["Allocation proposal index is unavailable"]
    unsafe = TaskvaletReader("http://example.com", "token").investigation(binding)
    assert unsafe["state"] == "unavailable" and "HTTPS" in unsafe["reason"]


def test_pinned_problem_version_newest_proposal_and_supersession_are_preserved() -> (
    None
):
    old = _round(ROUND)
    revised = _round(NEXT)
    revised["plan"]["problem_version"] = 2
    responses = _responses(rounds=[old, revised])
    research = responses[f"/api/v1/projects/lab/research/{PROBLEM}"]
    research["history"] = [research["problem"]]
    research["problem"] = {
        **research["problem"],
        "version": 2,
        "data": {**research["problem"]["data"], "statement": "A revised target."},
    }
    research["findings"] = [
        {
            "id": str(UUID(int=77)),
            "version": 1,
            "data": {"supersedes": FINDING},
        }
    ]
    newer = str(UUID(int=78))
    responses["/api/v1/projects/lab/round-proposals"]["proposals"] = [
        {"id": newer, "source_round": ROUND},
        {"id": PROPOSAL, "source_round": ROUND},
    ]
    responses[f"/api/v1/projects/lab/round-proposals/{newer}"] = {
        "proposal": {
            "action": "deepen",
            "rationale": "Newest proposal",
            "obstacles": [],
            "allocations": [],
        }
    }
    _, binding = _binding()
    view = _reader(responses).investigation({**binding, "stale_reasons": ()})
    assert [round_["id"] for round_ in view["rounds"]] == [ROUND]
    assert view["problem"]["statement"] == "Prove the target."
    assert view["rounds"][0]["decision"]["rationale"] == "Newest proposal"
    evidence = view["rounds"][0]["evidence"][0]
    assert evidence["superseded"] is True
    assert evidence["category"] == "stale_or_unavailable"


def test_supersession_after_one_thousand_records_is_not_ignored() -> None:
    responses = _responses(rounds=[_round(ROUND)])
    research = responses[f"/api/v1/projects/lab/research/{PROBLEM}"]
    research["findings"] = [
        {"id": str(UUID(int=1_000 + index)), "version": 1, "data": {}}
        for index in range(1_000)
    ] + [
        {
            "id": str(UUID(int=9_999)),
            "version": 1,
            "data": {"supersedes": FINDING},
        }
    ]
    _, binding = _binding()
    evidence = _reader(responses).investigation({**binding, "stale_reasons": ()})[
        "rounds"
    ][0]["evidence"][0]
    assert evidence["superseded"] is True
    assert evidence["category"] == "stale_or_unavailable"


def test_truncated_project_proposal_index_falls_back_to_the_round() -> None:
    responses = _responses(rounds=[_round(ROUND)])
    responses["/api/v1/projects/lab/round-proposals"] = {
        "proposals": [
            {
                "id": str(UUID(int=1_000 + index)),
                "source_round": str(UUID(int=2_000 + index)),
                "created_at": f"2026-10-28T12:{index % 60:02d}:00Z",
            }
            for index in range(100)
        ]
    }
    round_query = f"/api/v1/projects/lab/round-proposals?source_round={ROUND}"
    responses[round_query] = {
        "proposals": [
            {
                "id": PROPOSAL,
                "source_round": ROUND,
                "created_at": "2026-10-28T13:00:00Z",
            }
        ]
    }
    _, binding = _binding()
    view = _reader(responses).investigation({**binding, "stale_reasons": ()})
    assert view["rounds"][0]["decision"]["rationale"] == (
        "The exact replay closed the central case."
    )


def test_hostile_remote_shapes_are_normalized_instead_of_breaking_the_view() -> None:
    round_ = _round(ROUND)
    round_["campaign"]["participants"] = "not a list"
    round_["handoff"]["context_inputs"][0]["checks"] = [
        {"id": "check", "data": "not an object"}
    ]
    responses = _responses(rounds=[round_])
    responses[f"/api/v1/projects/lab/round-proposals/{PROPOSAL}"]["proposal"].update(
        obstacles="not a list", allocations="not a list", checker="not an object"
    )
    _, binding = _binding()
    view = _reader(responses).investigation({**binding, "stale_reasons": ()})
    projected = view["rounds"][0]
    assert projected["participants"] == []
    assert projected["decision"]["obstacles"] == []
    assert projected["decision"]["allocations"] == []
    assert projected["evidence"][0]["consumer_checks"][0]["method"] is None


def test_investigation_request_and_projected_output_budgets_return_partial_views(
    monkeypatch,
) -> None:
    _, binding = _binding()
    responses = _responses()
    monkeypatch.setattr(research_history, "MAX_REQUESTS", 3)
    limited = _reader(responses).investigation({**binding, "stale_reasons": ()})
    assert limited["state"] == "partial"
    assert "read budget was exhausted" in " ".join(limited["partial"])

    monkeypatch.setattr(research_history, "MAX_REQUESTS", 48)
    monkeypatch.setattr(research_history, "MAX_PROJECTED_BYTES", 10_000)
    oversized = _record("dead_end", "x" * 50_000)
    bounded = _reader(
        _responses(
            rounds=[
                _round(str(UUID(int=index + 1)), finding=oversized)
                for index in range(8)
            ]
        )
    ).investigation({**binding, "stale_reasons": ()})
    encoded = json.dumps(bounded, allow_nan=False).encode()
    assert len(encoded) <= research_history.MAX_PROJECTED_BYTES
    assert bounded["state"] == "partial"
    assert bounded["truncated_rounds"] > 0


def test_non_finite_and_nested_proposal_values_are_normalized() -> None:
    responses = _responses(rounds=[_round(ROUND)])
    proposal = responses[f"/api/v1/projects/lab/round-proposals/{PROPOSAL}"]["proposal"]
    proposal["exploration_percent"] = float("nan")
    proposal["checker"] = {"nested": [[float("inf")]]}
    proposal["allocations"] = [{"task": "x" * 60_000, "score": float("-inf")}]
    _, binding = _binding()
    view = _reader(responses).investigation({**binding, "stale_reasons": ()})
    decision = view["rounds"][0]["decision"]
    assert decision["exploration_percent"] is None
    assert decision["checker"] == {"nested": [[None]]}
    assert decision["allocations"][0]["score"] is None
    assert len(decision["allocations"][0]["task"]) == research_history.MAX_TEXT
    json.dumps(view, allow_nan=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("theorems", ["not an object"]),
        ("source", 7),
        ("job_id", 7),
    ],
)
def test_hostile_bundle_fields_stay_inside_the_evidence_boundary(field, value) -> None:
    bundle = _bundle()
    if field == "theorems":
        bundle["job"]["result"]["theorems"] = value
    elif field == "source":
        bundle["job"]["request"]["source"] = value
    else:
        bundle["job"]["id"] = value
    bundle["sha256"] = hashlib.sha256(
        __import__("json")
        .dumps(
            {key: item for key, item in bundle.items() if key != "sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        .encode()
    ).hexdigest()
    finding = _record("proof_candidate", __import__("json").dumps(bundle))
    _, binding = _binding()
    view = _reader(_responses(finding=finding)).investigation(
        {**binding, "stale_reasons": ()}
    )
    assert view["rounds"][0]["evidence"][0]["category"] == "unchecked_candidate"
