"""Tests for the Forseti view provider."""

from importlib import metadata

from glitnir.providers import DecisionProvider, EvidenceProvider, ViewProvider

from glitnir_forseti import providers


def test_provider_declares_kinds_and_imports() -> None:
    """One provider draws the three views; two run evidence; one decides."""
    found = providers()
    assert len(found) == 4
    provider = found[0]
    assert isinstance(provider, ViewProvider)
    assert all(isinstance(item, EvidenceProvider) for item in found[1:3])
    assert isinstance(found[3], DecisionProvider)
    assert provider.name == "forseti"
    assert provider.kinds == {"circuit", "model", "dynamics"}
    assert provider.imports == (
        "Lean",
        "Gimle.Asgard.Visualization.Model",
        "Gimle.Forseti",
    )
    assert provider.marker == "forsetiView:"


def test_preamble_has_syntax_and_no_imports() -> None:
    """The preamble is the adapter source without its import lines."""
    preamble = providers()[0].preamble
    assert '"#forseti_view" ident ident "using"' in preamble
    assert not any(line.startswith("import") for line in preamble.splitlines())


def test_command_always_emits_using_list() -> None:
    """The command syntax requires `using [...]`, which may be empty."""
    command = providers()[0].command
    assert command("circuit", "c", ()) == "#forseti_view circuit c using []"
    assert (
        command("circuit", "c", ("h1", "h2"))
        == "#forseti_view circuit c using [h1, h2]"
    )


def test_entry_point_is_registered() -> None:
    """The installed package advertises itself to Glitnir."""
    names = {e.name for e in metadata.entry_points(group="glitnir.providers")}
    assert "forseti" in names
