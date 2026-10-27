"""What a project check returns, and what a decision check returns.

These are the wire forms exactly: the keys and value types `check_project` and
`check_candidate` produce, as the service saves and shows them. The prose that
explains each field is in `project_checking.check_project`'s docstring and in
`notebook/check_records.py`.
"""

from typing import Any, Literal, NotRequired, TypedDict


class Axiom(TypedDict):
    """One axiom a theorem depends on, as both helper programs report it."""

    name: str
    module: str | None


class Use(TypedDict):
    """One claim a theorem uses: a theorem of this notebook or of another."""

    module: str
    name: str
    in_notebook: bool


class Theorem(TypedDict):
    """One checked declaration: its status, what decided it, and where it is."""

    name: str
    type: str
    statement: str | None
    uses: list[Use]
    uses_truncated: NotRequired[Literal[True]]
    axioms: list[Axiom]
    status: Literal["checked", "admitted", "assumptions"]
    start_line: int
    end_line: int
    example: bool
    proposition_sha256: str


class NonClaim(TypedDict):
    """An example that claims nothing: no type, or one that is not a proposition."""

    start_line: int
    end_line: int


class Withheld(TypedDict):
    """Where a check stopped, and why nothing from `from_line` on was checked."""

    from_line: int
    message: str
    through_line: NotRequired[int]


#: How long each stage of a check took, in milliseconds, by stage name; the
#: names are dynamic (`report_reference`, `report_candidate`, ...) and display only.
StageMs = dict[str, int]


class Checked(TypedDict):
    """What `_check_text` establishes about one text under a project snapshot."""

    status: Literal["checked_snapshot"]
    source_sha256: str
    environment_sha256: str
    toolchain: str
    module: str
    theorems: list[Theorem]
    non_claims: list[NonClaim]
    snapshot_bytes: int
    resolution: dict[str, Any] | None
    replay: str
    message: str


class CheckResult(Checked):
    """A finished check of a saved file: `Checked`, where it stopped, its stages.

    A check run remotely adds `execution`, the compute layer's description of
    where it ran; the records keep it under `environment`.
    """

    withheld: Withheld | None
    stage_ms: StageMs
    execution: NotRequired[dict[str, Any]]


class UnavailableResult(TypedDict):
    """A check that could not run: no statuses, and how far it got if any stage ran.

    A check run remotely adds `execution`, as `CheckResult` does.
    """

    status: Literal["unavailable"]
    message: str
    theorems: list[Theorem]
    stage_ms: NotRequired[StageMs]
    execution: NotRequired[dict[str, Any]]


#: What `check_project` returns.
CheckOutcome = CheckResult | UnavailableResult


class Verdict(TypedDict):
    """What `check_candidate` returns about a decision's block."""

    status: Literal["checked", "rejected"]
    message: str
    axioms: list[str]
    type: str | None


def with_message(result: CheckOutcome, message: str) -> CheckOutcome:
    """Return ``result`` with its message replaced and every other key kept.

    The two branches say the same thing; mypy types a spread only over one
    kind of result at a time.
    """
    if result["status"] == "checked_snapshot":
        return {**result, "message": message}
    return {**result, "message": message}
