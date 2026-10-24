"""Core notebook reads do not load optional decision producers or simulators."""

import subprocess
import sys


def test_editor_imports_do_not_load_decision_producers() -> None:
    code = """
import sys
from glitnir.service.cli import main
from glitnir.service.server import make_server
from glitnir_forseti import providers
providers()
for name in (
    'glitnir_forseti.entailment.service',
    'glitnir_forseti.trajectory.service',
    'glitnir_forseti.streams.service',
    'gimle.asgard', 'z3', 'jax',
):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_missing_simulator_is_detected_without_importing_it(monkeypatch) -> None:
    from glitnir_forseti import evidence_provider

    monkeypatch.setattr(
        evidence_provider.importlib.util, "find_spec", lambda name: None
    )
    assert evidence_provider.asgard_installed() is False


def test_only_iterate_needs_the_simulator(monkeypatch) -> None:
    from glitnir_forseti import evidence_provider

    monkeypatch.setattr(evidence_provider, "asgard_installed", lambda: False)
    simulate, iterate = evidence_provider.evidence_providers()
    assert simulate.unavailable() is None
    assert iterate.unavailable() == evidence_provider.NEEDS_ASGARD
    assert "needs Python Asgard" in evidence_provider.NEEDS_ASGARD
    assert evidence_provider.parse("simulate", "model", "(h := 1/10)").target
    # The card shows the missing dependency, not a directive error.
    assert evidence_provider.parse("iterate", "update", "(state := 1)").target


def test_asgard_without_its_decoder_is_not_enough(tmp_path, monkeypatch) -> None:
    """An Asgard without the wire decoder cannot iterate; nothing is imported."""
    import importlib.machinery

    from glitnir_forseti import evidence_provider

    (tmp_path / "asgard").mkdir()
    spec = importlib.machinery.ModuleSpec("gimle.asgard", None, is_package=True)
    spec.submodule_search_locations = [str(tmp_path / "asgard")]
    monkeypatch.setattr(evidence_provider.importlib.util, "find_spec", lambda n: spec)
    assert evidence_provider.asgard_installed() is False
    (tmp_path / "asgard" / "rational_interchange.py").write_text("")
    assert evidence_provider.asgard_installed() is True


def test_missing_namespace_parent_means_no_simulator(monkeypatch) -> None:
    """Without any gimle package, find_spec raises instead of returning None."""
    from glitnir_forseti import evidence_provider

    def missing_parent(name: str) -> None:
        raise ModuleNotFoundError("No module named 'gimle'", name="gimle")

    monkeypatch.setattr(evidence_provider.importlib.util, "find_spec", missing_parent)
    assert evidence_provider.asgard_installed() is False
