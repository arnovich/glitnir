"""Handoff preparation resolves real worker copies without submitting a check."""

import copy
import json
import os
import threading

import pytest

from glitnir.compute.handoff import prepare_handoff
from glitnir.compute.lemma_bundle import load_bundle
from glitnir.compute.protocol import checker_digest, context_digest, digest
from glitnir.lean.project_inspection import inspect_project


def test_shared_bundle_and_tampering(lemma_bundle, tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(lemma_bundle))
    assert load_bundle(path) == lemma_bundle
    changed = copy.deepcopy(lemma_bundle)
    changed["job"]["request"]["source"] += "-- altered\n"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        load_bundle(path)
    path.write_text('{"format":"one","format":"two"}')
    with pytest.raises(ValueError, match="Duplicate"):
        load_bundle(path)


@pytest.mark.parametrize(
    "change", ["withheld", "partial", "axiom", "module", "toolchain"]
)
def test_rehashed_untrusted_bundle_still_rejected(lemma_bundle, tmp_path, change):
    value = copy.deepcopy(lemma_bundle)
    if change == "withheld":
        value["job"]["result"]["withheld"] = {"reason": "later failure"}
    elif change == "partial":
        value["job"]["request"]["through_line"] = 2
    elif change == "axiom":
        value["job"]["result"]["theorems"][0]["axioms"] = [{"name": "sorryAx"}]
    elif change == "module":
        value["job"]["result"]["module"] = "../Escape"
    else:
        value["job"]["result"]["toolchain"] = "nightly"
    payload = {k: v for k, v in value.items() if k != "sha256"}
    value["sha256"] = digest(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_bundle(path)


@pytest.mark.handoff_build
def test_live_prepare_builds_lake_artifacts_and_reuses_receipt(
    handoff_project, tmp_path
):
    base, bundle, candidate = handoff_project
    root = tmp_path / "handoff"
    receipt = prepare_handoff(
        base,
        bundle,
        candidate,
        root,
        environment="reuse",
        candidate_path="Example/Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=100_000_000,
    )
    project = root / "build/project"
    assert receipt["binding"]["context_sha256"] == context_digest(
        project / "Example/Next.lean"
    )
    assert receipt["binding"]["path"] == "Example/Next.lean"
    assert receipt["binding"]["checker_sha256"] == checker_digest()
    assert receipt["preflight"]["status"] == "resolved"
    # Lake must have produced the complete import metadata, not just a raw olean.
    assert list((project / ".lake/build").rglob("*.trace"))
    assert (base / "Example/Checked.lean").exists() is False
    assert (base / "Example/Next.lean").exists() is False
    assert (
        inspect_project(root / "preflight/project/Example/Next.lean")["status"]
        == "resolved"
    )
    assert (
        prepare_handoff(
            base,
            bundle,
            candidate,
            root,
            environment="reuse",
            candidate_path="Example/Next.lean",
            timeout_seconds=180,
            memory_mb=4096,
            max_bytes=100_000_000,
        )
        == receipt
    )
    from glitnir.compute.worker import execute

    probe = json.loads((root / "state.json").read_text())["probe_request"]
    result = execute(project, probe, tmp_path / "native", threading.Event())
    assert result["status"] == "checked_snapshot", result
    assert result.get("withheld") is None, result
    theorem = next(t for t in result["theorems"] if t["name"] == "reused")
    assert theorem["status"] == "checked", theorem
    assert any(use["name"] == "candidate" for use in theorem["uses"]), theorem


def test_resume_preflight_without_rebuild_or_budget_reset(
    stub_handoff, tmp_path, monkeypatch
):
    import glitnir.compute.handoff as module

    base, bundle, candidate, _ = stub_handoff
    root = tmp_path / "handoff"
    inspect = module.inspect_project
    monkeypatch.setattr(
        module,
        "inspect_project",
        lambda *a, **kw: {"status": "unavailable", "message": "interrupted"},
    )
    options = dict(
        environment="reuse",
        candidate_path="Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=100_000_000,
    )
    with pytest.raises(ValueError, match="preflight"):
        prepare_handoff(base, bundle, candidate, root, **options)
    saved = json.loads((root / "state.json").read_text())
    monkeypatch.setattr(module, "inspect_project", inspect)
    monkeypatch.setattr(
        module, "build_module", lambda *a, **kw: pytest.fail("completed build repeated")
    )
    receipt = prepare_handoff(base, bundle, candidate, root, **options)
    assert (
        json.loads((root / "state.json").read_text())["deadline"] == saved["deadline"]
    )
    assert receipt["job"]["id"] == bundle["job"]["id"]
    with pytest.raises(ValueError, match="inputs"):
        prepare_handoff(base, bundle, candidate + "-- changed\n", root, **options)


def test_context_change_and_symlinks_fail_before_build(stub_handoff, tmp_path):
    base, bundle, candidate, _ = stub_handoff
    (base / "Unrelated.lean").write_text("-- changed base\n")
    options = dict(
        environment="reuse",
        candidate_path="Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=100_000_000,
    )
    with pytest.raises(ValueError, match="context"):
        prepare_handoff(base, bundle, candidate, tmp_path / "bad", **options)
    (base / "Unrelated.lean").unlink()
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / "link").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="link|private"):
        prepare_handoff(base, bundle, candidate, tmp_path / "link", **options)


@pytest.mark.parametrize(
    "change",
    [
        "receipt",
        "probe",
        "artifact",
        "deadline",
        "nan",
        "infinity",
        "future",
        "boolean",
        "null_state",
    ],
)
def test_saved_state_and_artifacts_are_revalidated(
    stub_handoff, tmp_path, monkeypatch, change
):
    import glitnir.compute.handoff as module

    base, bundle, candidate, _ = stub_handoff
    root = tmp_path / "handoff"
    options = dict(
        environment="reuse",
        candidate_path="Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=100_000_000,
    )
    if change in {"deadline", "nan", "infinity", "future", "boolean"}:
        monkeypatch.setattr(
            module, "inspect_project", lambda *a, **kw: {"status": "unavailable"}
        )
        with pytest.raises(ValueError, match="preflight"):
            prepare_handoff(base, bundle, candidate, root, **options)
    else:
        prepare_handoff(base, bundle, candidate, root, **options)
    path = root / "state.json"
    saved = json.loads(path.read_text())
    if change == "receipt":
        saved["receipt"]["proof_authority"] = True
    elif change == "probe":
        saved["probe_request"]["path"] = "Other.lean"
    elif change == "artifact":
        (root / "build/project/.lake/artifact").write_text("corrupted")
    elif change != "null_state":
        saved["deadline"] = {
            "deadline": 0,
            "nan": float("nan"),
            "infinity": float("inf"),
            "future": 10**12,
            "boolean": True,
        }[change]
    path.write_text(json.dumps(None if change == "null_state" else saved))
    with pytest.raises(ValueError):
        prepare_handoff(base, bundle, candidate, root, **options)


def test_build_growth_is_reserved_before_second_copy(
    stub_handoff, tmp_path, monkeypatch
):
    import glitnir.compute.handoff as module

    base, bundle, candidate, _ = stub_handoff
    monkeypatch.setattr(
        module,
        "build_module",
        lambda project, *args: (project / ".lake/artifact").write_bytes(b"x" * 61000),
    )
    root = tmp_path / "handoff"
    with pytest.raises(ValueError, match="insufficient disk"):
        prepare_handoff(
            base,
            bundle,
            candidate,
            root,
            environment="reuse",
            candidate_path="Next.lean",
            timeout_seconds=180,
            memory_mb=4096,
            max_bytes=100000,
        )
    assert not (root / "preflight").exists()


def test_copying_checks_original_deadline(stub_handoff, tmp_path, monkeypatch):
    import glitnir.compute.handoff as module

    base, bundle, candidate, _ = stub_handoff
    original = module.stage_project

    def slow(*args, **kwargs):
        monkeypatch.setattr(module.time, "time", lambda: 10**12)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "stage_project", slow)
    with pytest.raises(ValueError, match="deadline"):
        prepare_handoff(
            base,
            bundle,
            candidate,
            tmp_path / "handoff",
            environment="reuse",
            candidate_path="Next.lean",
            timeout_seconds=180,
            memory_mb=4096,
            max_bytes=100000,
        )


@pytest.mark.parametrize("slow_scan", [False, True])
def test_many_files_do_not_rescan_tree_per_chunk(
    stub_handoff, tmp_path, monkeypatch, slow_scan
):
    import glitnir.compute.handoff as module

    base, bundle, candidate, _ = stub_handoff
    for index in range(200):
        (base / ".lake" / str(index)).write_bytes(b"x" * 1024)
    scans = []
    size = module._size
    clock = [100.0]
    if slow_scan:
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def counted(root):
        scans.append(root)
        if slow_scan:
            clock[0] += 0.3
        return size(root)

    monkeypatch.setattr(module, "_size", counted)
    prepare_handoff(
        base,
        bundle,
        candidate,
        tmp_path / "handoff",
        environment="reuse",
        candidate_path="Next.lean",
        timeout_seconds=180,
        memory_mb=4096,
        max_bytes=10000000,
    )
    assert len(scans) < 30, len(scans)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory access bits")
def test_unreadable_artifacts_are_not_omitted(tmp_path):
    from glitnir.compute.handoff import _inventory, _size

    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "data").write_bytes(b"x")
    hidden.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _size(tmp_path)
        with pytest.raises(PermissionError):
            _inventory(tmp_path, lambda: None)
    finally:
        hidden.chmod(0o700)
