"""Notebooks: a Lean file of prose, definitions and claims.

A notebook is one Lean module, and Lean is its only source of mathematical
truth. This package splits the file into cells for the preview, checks the
saved file in its Lean project and keeps a record of that check. It is
untrusted orchestration over Lean: it re-derives nothing Lean checks and
grants no proof authority.
"""

from .document import Cell, CellKind, Claim, Notebook, NotebookError, parse_notebook

__all__ = [
    "Cell",
    "CellKind",
    "Claim",
    "Notebook",
    "NotebookError",
    "parse_notebook",
]
