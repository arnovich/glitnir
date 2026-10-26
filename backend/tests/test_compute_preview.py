"""The editor delegates remote checks without losing snapshot freshness."""

import hashlib

import pytest
from conftest import settle

from glitnir.compute.protocol import context_digest
from glitnir.notebook.check_records import load_records
from glitnir.service.preview import SourcePreviewService


@pytest.mark.parametrize("change_before_acquisition", [False, True])
def test_remote_preview_records_exact_context_or_refuses_drift(
    compute_project, monkeypatch, change_before_acquisition
):
    """The fingerprint saved by the editor must describe the submitted context."""
    monkeypatch.setenv("GLITNIR_EXECUTION_BACKEND", "taskvalet")
    monkeypatch.setenv("GLITNIR_TASKVALET_URL", "https://tasks.example.test")
    monkeypatch.setenv("GLITNIR_TASKVALET_PROJECT", "lab")
    monkeypatch.setenv("GLITNIR_TASKVALET_ENVIRONMENT", "prepared")
    monkeypatch.setenv("GLITNIR_TASKVALET_TOKEN", "secret")
    source = compute_project / "Example.lean"
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    provenance = {"kind": "taskvalet", "job": "remote-job", "worker": "worker"}

    def remote(path, **kwargs):
        assert kwargs["expected_sha256"] == sha
        context = context_digest(path)
        if change_before_acquisition:
            (compute_project / "Helper.lean").write_text("def helper := 42\n")
        kwargs["acquired"](context)
        return {
            "status": "checked_snapshot",
            "source_sha256": sha,
            "environment_sha256": "e" * 64,
            "theorems": [],
            "withheld": None,
            "execution": provenance,
        }

    def local(*args, **kwargs):
        pytest.fail("The remote preview invoked the local checker")

    monkeypatch.setattr("glitnir.service.preview.check_remote", remote)
    monkeypatch.setattr("glitnir.service.preview.check_project", local)
    service = SourcePreviewService(source)
    try:
        service.check(sha)
        settle(service)
        assert not service._worker.is_alive()
        if change_before_acquisition:
            assert service._check["status"] == "unavailable"
            assert not source.with_suffix(".glitnir.json").exists()
        else:
            assert service._check["status"] == "checked_snapshot"
            assert load_records(source)["environment"]["execution"] == provenance
    finally:
        service.close()
