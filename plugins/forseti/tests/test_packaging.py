"""The installed package carries its typing marker."""

from importlib import resources


def test_the_package_ships_a_py_typed_marker() -> None:
    """Type checkers analyse glitnir_forseti instead of treating it as untyped."""
    assert resources.files("glitnir_forseti").joinpath("py.typed").is_file()
