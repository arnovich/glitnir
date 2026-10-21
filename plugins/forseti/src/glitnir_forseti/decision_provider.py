"""Decide `-- @decide <name> : <goal>` directives for Glitnir.

A goal is written in the syntax of `glitnir_forseti.claims.text` and starts
with its kind: ``entailment``, ``trajectory``, ``stream`` or ``stepped``.
`decide` runs that kind's decider without the pinned Lean replay: the first
evidence a lane finds, or for a stepped goal the template its exact arithmetic
chooses, becomes a proposed Lean block
(`glitnir_forseti.notebook.decisions.block`).
Nothing here grants anything. Glitnir's core checks the block with the
project's own Lean after the notebook's text up to the directive, and only
that check makes it a proof.

A proof is the theorem ``<name>``; a refutation is ``<name>_refuted``, stating
``¬ (claim)``. Each block is self-contained: it defines what it uses in its own
``Decided_<name>`` namespace. Which modules it needs depends on the goal (the
entailment lane, or the model), so `IMPORTS` is empty and the outcome lists
the block's imports as a stage; a notebook without them fails the check.

Deciding needs Z3, from the ``z3`` extra (the stepped decider itself uses
none). A cancelled decision returns ``unknown`` with the reason ``cancelled``.
"""

import importlib.util
import threading
from typing import Any

from glitnir.providers import DecisionProvider, DecisionRequest

KINDS = frozenset({"entailment", "trajectory", "stream", "stepped"})
#: The modules every block needs, whatever its goal; see the module docstring.
IMPORTS: tuple[str, ...] = ()
NEEDS_Z3 = "Install glitnir-forseti[z3] to decide goals."
#: Every name a block's templates define starts with this, so a decision
#: named with it could collide with them in the block's namespace.
RESERVED_PREFIX = "forseti"
#: Bounds of Glitnir's decision outcome (`glitnir.notebook.decision_cards`).
_MAX_REASON = 200
_MAX_STAGES = 32
_MAX_STAGE_TEXT = 500


def z3_installed() -> bool:
    """Whether Z3 can be imported, without importing it."""
    try:
        return importlib.util.find_spec("z3") is not None
    except (ModuleNotFoundError, ValueError):
        return False


def unavailable() -> str | None:
    """Name the missing Z3 extra, or return None when deciding can run."""
    return None if z3_installed() else NEEDS_Z3


def _goal(kind: str, text: str) -> Any:
    """Read and name-check a goal of `kind`; raise `ValueError` if it is bad."""
    from glitnir_forseti.claims.decide import check_names, goal_kind, read_goal

    if kind not in KINDS:
        raise ValueError(f"Forseti decides only {', '.join(sorted(KINDS))} goals.")
    try:
        goal = read_goal(text)
        check_names(goal)
    except (ValueError, RecursionError) as error:  # GoalTextError among them
        raise ValueError(f"Unreadable goal: {error}") from None
    found = goal_kind(goal)
    if found != kind:
        article = "an" if found[0] in "aeiou" else "a"
        raise ValueError(f"The goal reads as {article} {found} goal, not {kind}.")
    return goal


def parse(name: str, kind: str, goal: str) -> DecisionRequest:
    """Read a directive's goal; raise `ValueError` if it cannot be decided."""
    if name.startswith(RESERVED_PREFIX):
        raise ValueError(
            f"The decision name `{name}` is reserved: names starting with "
            f"`{RESERVED_PREFIX}` are the definitions a decided block makes."
        )
    _goal(kind, goal)
    return DecisionRequest(name, kind, goal, {})


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stage(record: dict[str, Any]) -> list[str]:
    """Show one lane or stage record as `[stage, outcome, detail]`.

    Nothing is replayed here, so no record carries a Lean check. The only
    check a record can still name is a stream's ``invalid``: evidence that does
    not fit its claim, which its detail explains.
    """
    name = str(record.get("lane") or record.get("stage") or "")
    outcome = str(record.get("outcome") or "")
    if record.get("check") == "invalid":
        outcome = "does not fit the claim"
    return [
        _clip(name, _MAX_STAGE_TEXT),
        _clip(outcome, _MAX_STAGE_TEXT),
        _clip(str(record.get("detail") or "").strip(), _MAX_STAGE_TEXT),
    ]


def decide(request: DecisionRequest, cancel: threading.Event) -> dict[str, Any]:
    """Decide one request without replay; return a Glitnir decision outcome.

    `cancel` stops the decider's solver worker; the outcome is then
    ``unknown`` with the reason ``cancelled``.
    """
    from glitnir_forseti.claims.decide import decide as decide_goal
    from glitnir_forseti.notebook.decisions import block

    goal = _goal(request.kind, request.goal)
    decision = decide_goal(goal, cancel=cancel, replay=False)
    stages = [_stage(record) for record in decision.stages]
    if decision.status == "UNKNOWN":
        reason = decision.reason or "undecided"
        return {
            "status": "unknown",
            "reason": _clip(reason, _MAX_REASON),
            "stages": stages[:_MAX_STAGES],
        }
    proposed = block(request.name, decision)
    needs = "\n".join(f"import {module}" for module in proposed.imports)
    stages.append(["imports", "needed", _clip(needs or "none", _MAX_STAGE_TEXT)])
    found = stages[-2][0] if len(stages) > 1 else request.kind
    return {
        "status": "proven" if decision.status == "PROVEN" else "refuted",
        "reason": _clip(f"Found by the {found} stage.", _MAX_REASON),
        "stages": stages[-_MAX_STAGES:],
        "block": proposed.source,
        "theorem": proposed.theorem,
    }


def decision_provider() -> DecisionProvider:
    """Return the Forseti provider for all four kinds of goal."""
    return DecisionProvider(
        name="forseti",
        kinds=KINDS,
        imports=IMPORTS,
        parse=parse,
        decide=decide,
        unavailable=unavailable,
    )
