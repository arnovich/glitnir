"""The application and reusable proof tools have no embedded agent dependency."""

import importlib.util


def test_hugin_is_not_an_application_package() -> None:
    """Installing Forseti does not install an agent harness."""
    assert importlib.util.find_spec("glitnir.hugin") is None
    assert importlib.util.find_spec("glitnir.notebook.agents") is None


def test_headless_decisions_are_available_without_hugin() -> None:
    """Decisions remain usable outside the UI."""
    from glitnir_forseti.claims.decide import decide

    assert callable(decide)
