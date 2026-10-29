"""Check a proposed block as the editor does, for the live lane tests.

The live tests in ``test_stepped_lean.py`` and ``test_hedging_lean.py`` write
a scratch notebook into the Forseti Lean checkout and have Glitnir check the
saved file followed by the block (``check_candidate``, the one proof
authority for a decision). The ``check_block`` fixture in ``conftest.py`` uses
`check_in_scratch` here, and holds each check to `read_check_budget_ms`.
"""

import hashlib
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

#: The axioms Lean itself provides; a block may rest on nothing else.
STANDARD_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})
#: The longest a block may take to check, in milliseconds, unless the
#: environment says otherwise (`read_check_budget_ms`).
CHECK_BUDGET_MS = 300_000
#: A block that needs nothing, to have the checker replay a notebook's imports.
WARM_BLOCK = "theorem warm_up : True := trivial"


def read_check_budget_ms() -> int:
    """Return the budget a live check is held to.

    An unset or empty ``FORSETI_CHECK_BUDGET_MS`` means `CHECK_BUDGET_MS`.

    Raises:
        pytest.fail.Exception: If the variable is not a positive whole number.
    """
    value = os.environ.get("FORSETI_CHECK_BUDGET_MS", "").strip()
    if not value:
        return CHECK_BUDGET_MS
    try:
        budget = int(value)
    except ValueError:
        budget = 0
    if budget <= 0:
        pytest.fail(
            "FORSETI_CHECK_BUDGET_MS must be a positive whole number of"
            f" milliseconds, got {value!r}",
            pytrace=False,
        )
    return budget


@dataclass(frozen=True)
class Checked:
    """What the project's Lean said about a proposed block.

    Attributes:
        ok: Whether Glitnir reports the block's theorem as checked.
        message: Glitnir's message, which names the failing line otherwise.
        axioms: The axioms the theorem depends on.
        check_ms: How long the check took, in milliseconds.
        stage_ms: How long each of the check's stages took, in milliseconds.
    """

    ok: bool
    message: str
    axioms: tuple[str, ...]
    check_ms: int
    stage_ms: dict[str, int]


def check_in_scratch(
    write: Callable[[str, str], Path], imports: str, block: str, theorem: str
) -> Checked:
    """Check a block as the editor does, in a scratch notebook of the project.

    The notebook holds the imports, then a directive line; the block is checked
    right after it. ``write`` is the ``scratch_notebook`` fixture's, so no file
    of the checkout is overwritten and only the scratch file is removed.
    """
    from glitnir.lean.project_checking import check_candidate

    header = "".join(f"{line}\n" for line in imports.split("\n"))
    text = f"{header}\n-- @decide {theorem} : (scratch)\n"
    path = write("GlitnirBlockLive", text)
    stage_ms: dict[str, int] = {}
    started = time.monotonic()
    verdict = check_candidate(
        path,
        expected_sha256=hashlib.sha256(text.encode()).hexdigest(),
        insert_after_line=text.count("\n"),
        block=block,
        theorem=theorem,
        cancel=threading.Event(),
        stage_ms=stage_ms,
    )
    elapsed = round((time.monotonic() - started) * 1000)
    return Checked(
        verdict["status"] == "checked",
        verdict["message"],
        tuple(verdict["axioms"]),
        elapsed,
        stage_ms,
    )
