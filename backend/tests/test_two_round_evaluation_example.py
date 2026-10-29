"""The public comparison example stays pinned and candid after its invalid trial."""

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[2] / "examples/two-round-lean-evaluation"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {str(key).lower() for key in value} | set().union(
            *(_json_keys(item) for item in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_json_keys(item) for item in value), set())
    return set()


def test_executed_two_round_example_has_no_hidden_admitted_proof() -> None:
    source = (ROOT / "TwoRoundTrial.lean").read_text()
    report = (ROOT / "REPORT.md").read_text()
    result = json.loads((ROOT / "result.json").read_text())
    assert "theorem target (a b c : ℝ) (h : a + b + c = 0)" in source
    assert "/- BEGIN PROOF target -/\n  sorry\n/- END PROOF target -/" in source
    assert "trial invalidated; no admitted proof" in report
    assert "Allowed target axioms: none" in report
    assert result["status"] == "invalidated"
    assert result["admitted_targets"] == {"adaptive": 0, "baseline": 0}
    assert result["model_call_accounting"] == "operator_reported"
    assert result["replay_scope"] == "published_evidence_audit_only"


def test_report_pins_the_current_template_and_environment() -> None:
    source = (ROOT / "TwoRoundTrial.lean").read_bytes()
    report = (ROOT / "REPORT.md").read_text()
    assert hashlib.sha256(source).hexdigest() in report
    assert "leanprover/lean4:v4.35.0" in report
    assert "2c6238eff6de60d63e45e10c93cdc7239de7ad69" in report
    assert "c33461ada7554b5d422bfb549ea40e0c83355f115d1de52bd75f2e1227908c7e" in report


def test_evidence_archive_is_complete_redacted_and_hash_bound() -> None:
    evidence = ROOT / "evidence"
    sources = list(evidence.glob("**/*.lean"))
    assert len(sources) == 24
    assert len(list(evidence.glob("a*/**/*.lean"))) == 12
    assert len(list(evidence.glob("b*/**/*.lean"))) == 12
    assert len({path.read_bytes() for path in sources}) >= 12
    assert not list(evidence.glob("**/agent.json"))
    assert not list(evidence.glob("**/.taskvalet/**"))
    publication = "\n".join(
        path.read_text(errors="replace") for path in evidence.rglob("*") if path.is_file()
    )
    assert "/Users/" not in publication
    assert "/private/tmp" not in publication
    assert "github.com/ErikMathiesen/glitnir" not in publication
    assert "yes I am ahppy woth that" not in publication
    assert "automatic approval" not in publication

    sanitization = json.loads((evidence / "sanitization.json").read_text())
    assert sanitization["format"] == "taskvalet.evidence-sanitization/v1"
    assert len(sanitization["artifact_uri_errata"]["affected_record_ids"]) == 6
    entries = {entry["published_path"]: entry for entry in sanitization["files"]}
    published = {
        str(path.relative_to(evidence))
        for path in evidence.rglob("*")
        if path.is_file() and path.name != "sanitization.json"
    }
    assert set(entries) == published
    for relative, entry in entries.items():
        path = evidence / relative
        assert _sha256(path) == entry["published_sha256"]
        if entry["redacted"]:
            assert ".redacted" in path.name
            assert entry["original_sha256"] != entry["published_sha256"]
            assert entry["replacement_counts"]
        else:
            assert entry["original_sha256"] == entry["published_sha256"]

    original_entries = {entry["original_path"]: entry for entry in entries.values()}
    json_files = {
        path: json.loads(path.read_text()) for path in evidence.rglob("*.json")
    }
    receipts = {
        path: value
        for path, value in json_files.items()
        if isinstance(value, dict)
        and isinstance(value.get("command"), list)
        and value["command"][:3] == ["lake", "env", "lean"]
    }
    assert len(receipts) == 24
    for receipt_path, receipt in receipts.items():
        source_name = Path(receipt["command"][-1]).name
        original_dir = Path(entries[str(receipt_path.relative_to(evidence))]["original_path"]).parent
        source_entry = original_entries[str(original_dir / source_name)]
        assert receipt.get("source_sha256", receipt.get("sha256")) == source_entry[
            "original_sha256"
        ]
        log_sha256 = receipt.get("log_sha256")
        if log_sha256 is not None:
            log_entries = [
                entry
                for path, entry in original_entries.items()
                if Path(path).parent == original_dir
                and Path(path).stem.startswith(Path(source_name).stem)
                and Path(path).suffix == ".log"
            ]
            assert any(entry["original_sha256"] == log_sha256 for entry in log_entries)

    forbidden_keys = {"access_token", "api_key", "authorization", "password", "secret"}
    for value in json_files.values():
        assert not (_json_keys(value) & forbidden_keys)


def test_result_counts_and_record_ids_reconcile_with_the_archive() -> None:
    result = json.loads((ROOT / "result.json").read_text())
    evidence_text = "\n".join(
        path.read_text(errors="replace") for path in (ROOT / "evidence").rglob("*") if path.is_file()
    )
    assert result["completed_model_calls"] == {"adaptive": 6, "baseline": 6}
    assert result["lean_submissions"] == {"adaptive": 12, "baseline": 12}
    assert result["max_elapsed_seconds"] == 3600
    for arm in ("adaptive_final", "baseline_final"):
        for record_id in result[arm]["verifications"]:
            assert record_id in evidence_text
        assert result[arm]["synthesis"] in evidence_text
