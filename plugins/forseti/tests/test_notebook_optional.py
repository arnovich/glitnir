"""Core notebook reads do not load optional decision producers or simulators."""

import subprocess
import sys

import pytest


def test_editor_imports_do_not_load_decision_producers() -> None:
    code = """
import sys
from glitnir.notebook.__main__ import main
from glitnir.notebook.server import make_server
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
    assert evidence_provider.unavailable() is None
    assert evidence_provider.parse("simulate", "model", "(h := 1/10)").target
    with pytest.raises(ValueError) as refused:
        evidence_provider.parse("iterate", "update", "(state := 1)")
    assert str(refused.value) == evidence_provider.NEEDS_ASGARD


def test_missing_namespace_parent_means_no_simulator(monkeypatch) -> None:
    """Without any gimle package, find_spec raises instead of returning None."""
    from glitnir_forseti import evidence_provider

    def missing_parent(name: str) -> None:
        raise ModuleNotFoundError("No module named 'gimle'", name="gimle")

    monkeypatch.setattr(evidence_provider.importlib.util, "find_spec", missing_parent)
    assert evidence_provider.asgard_installed() is False
