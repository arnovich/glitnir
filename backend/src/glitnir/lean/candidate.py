"""A decision candidate's text and verdict: the saved prefix plus a provider block."""

from glitnir.lean.environment import LEAN_AXIOM_ALLOWLIST
from glitnir.lean.failures import (
    CommandFailure,
    failure_message,
    first_error_offset,
    line_range,
)
from glitnir.lean.project_snapshot import SnapshotError
from glitnir.lean.results import Theorem, Verdict


def candidate_text(saved: str, insert_after_line: int, block: str) -> str:
    """Return the saved lines through `insert_after_line`, then `block` and LF."""
    lines = saved.split("\n")
    if not 1 <= insert_after_line <= len(lines):
        raise SnapshotError(
            f"Decision directive line {insert_after_line} is outside the notebook."
        )
    return "".join(line + "\n" for line in lines[:insert_after_line]) + block + "\n"


def failure_location(failure: CommandFailure, text: str, insert_after_line: int) -> str:
    """Word a failure's line as a block line, or as a line of the saved notebook."""
    # A compile error carries its own position; a reader failure only the command's.
    position = first_error_offset(str(failure), text)
    if position is None:
        position = failure.offset
    line, _ = line_range(text, position, position + 1)
    message = failure_message(str(failure)) or "The checker stopped at this command."
    if line > insert_after_line:
        return f"Block line {line - insert_after_line}: {message}"
    return f"Line {line}: {message}"


def candidate_verdict(
    theorems: list[Theorem], theorem: str, first: int, last: int
) -> Verdict:
    """Accept only one `checked` declaration of `theorem` within lines first..last."""
    found = [
        item
        for item in theorems
        if (item["name"] == theorem or item["name"].endswith("." + theorem))
        and first <= item["start_line"]
        and item["end_line"] <= last
    ]
    if len(found) != 1:
        message = (
            f"The block does not declare `{theorem}`."
            if not found
            else f"The block declares `{theorem}` more than once."
        )
        return {"status": "rejected", "message": message, "axioms": [], "type": None}
    name, status, axioms = found[0]["name"], found[0]["status"], found[0]["axioms"]
    # Names only (the saved decision and the card hold strings); the
    # modules stay in the rejection message below.
    names = list(dict.fromkeys(a["name"] for a in axioms))
    kind = found[0]["type"]
    if status == "checked":
        message = f"The checker reports `{name}` as checked."
        return {"status": "checked", "message": message, "axioms": names, "type": kind}
    if status == "admitted":
        message = f"`{name}` is admitted: its proof depends on `sorry`."
    else:
        extra = [a["name"] for a in axioms if a["name"] not in LEAN_AXIOM_ALLOWLIST]
        if extra:
            listed = ", ".join(f"`{a}`" for a in extra)
            message = (
                f"`{name}` depends on axioms outside Lean's standard set: {listed}."
            )
        else:
            # Standard names, but not all provided by the toolchain's own modules.
            listed = ", ".join(
                f"`{a['name']}` (from `{a['module'] or 'an unknown module'}`)"
                for a in axioms
            )
            message = (
                f"`{name}` depends on axioms the checker could not trace to "
                f"Lean's own modules: {listed}."
            )
    return {"status": "rejected", "message": message, "axioms": names, "type": kind}
