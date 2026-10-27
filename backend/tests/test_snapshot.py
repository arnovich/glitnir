"""The preview snapshot's cells."""

from glitnir.lean.prepared_packages import ResolutionPackage
from glitnir.preview.snapshot import ResolutionSummary, indexed_cells


def test_indexed_cells_number_in_order():
    """Cells are numbered in file order, each with its id and index."""
    entries = indexed_cells("/-! a -/\ntheorem t : True := trivial\n")
    assert [(e["id"], e["index"]) for e in entries] == [
        (f"cell-{i}", i) for i in range(len(entries))
    ]


def test_resolution_summary_shows_the_resolution_packages():
    """The summary's packages are the checker's resolution packages, unchanged."""
    assert ResolutionSummary.__annotations__["packages"] == list[ResolutionPackage]
