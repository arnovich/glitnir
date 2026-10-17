"""Forseti views, simulation evidence and decisions for Glitnir notebooks."""

from importlib import resources

from glitnir.providers import DecisionProvider, EvidenceProvider, ViewProvider


def _preamble() -> str:
    source = (
        resources.files("glitnir_forseti")
        .joinpath("ViewDiagram.lean")
        .read_text(encoding="utf-8")
    )
    lines = source.splitlines()
    return "\n".join(line for line in lines if not line.startswith("import ")) + "\n"


def _command(kind: str, name: str, using: tuple[str, ...]) -> str:
    return f"#forseti_view {kind} {name} using [{', '.join(using)}]"


def providers() -> list[ViewProvider | EvidenceProvider | DecisionProvider]:
    """Return the Forseti view, evidence and decision providers."""
    from glitnir_forseti.decision_provider import decision_provider
    from glitnir_forseti.evidence_provider import evidence_provider

    return [
        ViewProvider(
            name="forseti",
            kinds=frozenset({"circuit", "model", "dynamics"}),
            imports=("Lean", "Gimle.Asgard.Visualization.Model", "Gimle.Forseti"),
            preamble=_preamble(),
            marker="forsetiView:",
            command=_command,
        ),
        evidence_provider(),
        decision_provider(),
    ]
