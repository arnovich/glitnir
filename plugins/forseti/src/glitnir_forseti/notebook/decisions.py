"""Turn a decided goal into the Lean block the decision provider proposes.

The decision provider (`glitnir_forseti.decision_provider`) reads a
``-- @decide <name> : <goal>`` directive's goal in the syntax of
`glitnir_forseti.claims.text` and runs its decider. The decider's answer
becomes a *block*: the Lean definitions of the decided statement in their own
namespace, and one theorem. The claim is written as the property it is about
(`readable`) and proved by the decider's script followed by a checked bridge
to that property; a refutation is instead the theorem ``<name>_refuted``
stating ``¬ (claim)``, derived the same way. The block carries every
definition it uses. Whether it holds is decided only by Lean checking it as
part of the file, never by the decider or this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from glitnir_forseti.claims.decide import Decision
from glitnir_forseti.claims.text import render_goal
from glitnir_forseti.entailment.syntax import Goal
from glitnir_forseti.notebook.readable import Readable, readable
from glitnir_forseti.streams.goal import StreamGoal
from glitnir_forseti.trajectory.goal import TrajectoryGoal

_IMPORT = re.compile(r"^import\s+(\S+)\s*$")
#: The two statement shapes of a refutation, and how each proves ``¬ claim``.
_ROOT_REFUTATION = ": forsetiIdentities ∧ ¬ forsetiContract"
_ENTAILMENT = (
    ": Gimle.Forseti.Syntax.FormulaEntailment forsetiAntecedent forsetiConsequent"
)
#: The lane statements each readable bridge is written against. A stream
#: refutation is the root's identities with the contract's negation, or, for
#: a lane whose root has no identities (a field band), the root's negation.
_LANE_STATEMENTS: dict[type, dict[str, tuple[str, ...]]] = {
    Goal: {
        "PROVEN": (_ENTAILMENT,),
        "REFUTED": (": ¬ " + _ENTAILMENT.removeprefix(": "),),
    },
    TrajectoryGoal: {"PROVEN": (": forsetiRoot",), "REFUTED": (_ROOT_REFUTATION,)},
    StreamGoal: {
        "PROVEN": (": forsetiRoot",),
        "REFUTED": (_ROOT_REFUTATION, ": ¬ forsetiRoot"),
    },
}


class DecisionError(ValueError):
    """A decision cannot become a notebook block."""


@dataclass(frozen=True)
class Block:
    """A decided goal as the Lean that states and proves it.

    Attributes:
        source: The Lean to insert right after the directive, self-contained:
            a namespace holding the lane's definitions and the theorem, and
            the end of the namespace. Every line is trimmed, the first is
            code and there is no trailing newline.
        imports: The modules the definitions need, which the notebook's
            header must import.
        theorem: The theorem ``source`` declares: the claim's name for a
            proof, the name with ``_refuted`` for a refutation.
    """

    source: str
    imports: tuple[str, ...]
    theorem: str


def namespace(name: str) -> str:
    """Return the namespace a decided claim's definitions live in."""
    return f"Decided_{name}"


def block(name: str, decision: Decision) -> Block:
    """Turn a decided goal into the block that states and proves it.

    The claim is stated readably when the goal has a readable form
    (`readable.readable`): its proof first proves the lane's statement with
    the lane's script, as ``root``, then derives the claim from it. A
    refutation is the theorem ``<name>_refuted : ¬ (claim)``, which proves the
    lane's negated statement, as ``refuted``, then ``¬ claim`` from it. Other
    goals keep the lane's statement as the claim. The block carries every
    definition it uses, so it relies on nothing else in the notebook.

    Raises:
        DecisionError: The decision is ``UNKNOWN`` or its statement has a shape
            this module does not know how to state as a claim.
    """
    if decision.lean is None or decision.status not in ("PROVEN", "REFUTED"):
        raise DecisionError("only a decided goal becomes a claim")
    imports, definitions = _split_prelude(decision.lean.prelude)
    goal = render_goal(decision.goal)
    if "-/" in goal:
        raise DecisionError("the goal text would close the claim's docstring")
    try:
        stated = readable(decision.goal)
    except (KeyError, ValueError) as error:
        raise DecisionError(f"no readable statement: {error}") from error
    if stated is None:
        statement, proof = _as_lane(decision)
        note = _note(decision, "Stated as the decider states it.")
    else:
        statement, proof = _as_readable(decision, stated)
        note = _note(decision, stated.description)
    theorem = name
    if decision.status == "REFUTED":
        statement, theorem = _negated(statement), f"{name}_refuted"
    body = "\n".join("  " + line for line in proof)
    parts = [
        f"namespace {namespace(name)}",
        definitions,
        f"/-- {note} Goal ({decision.kind}): {goal} -/\n"
        f"theorem {theorem} {statement} := by\n{body}",
        f"end {namespace(name)}",
    ]
    source = "\n\n".join(part for part in parts if part)
    # Editors trim trailing blanks on save; the inserted block must not change.
    trimmed = "\n".join(line.rstrip() for line in source.split("\n"))
    return Block(trimmed, imports, theorem)


def _negated(statement: str) -> str:
    """State the negation of a claim's statement, written ``: <claim>``."""
    if not statement.startswith(":"):
        raise DecisionError(f"no claim shape for the statement {statement!r}")
    return f": ¬ ({statement.removeprefix(':').strip()})"


def _note(decision: Decision, what: str) -> str:
    # Provenance only: whether the theorem holds is its check's to say.
    verb = "proved" if decision.status == "PROVEN" else "refuted"
    return f"{what} The {decision.kind} decider {verb} it."


def _as_lane(decision: Decision) -> tuple[str, tuple[str, ...]]:
    """State the claim as the lane does, with its proof or its refutation."""
    assert decision.lean is not None
    statement, script = decision.lean.statement, decision.lean.script
    if decision.status == "REFUTED":
        return _refuting(statement, script)
    return statement, script


def _as_readable(decision: Decision, stated: Readable) -> tuple[str, tuple[str, ...]]:
    """State the claim readably, bridged to the lane's statement.

    Returns the claim's statement and, for a proof, its proof or, for a
    refutation, the proof of its negation.
    """
    assert decision.lean is not None
    lane, script = decision.lean.statement, decision.lean.script
    if lane not in _LANE_STATEMENTS[type(decision.goal)][decision.status]:
        raise DecisionError(f"no claim shape for the statement {lane!r}")
    nested = tuple("  " + line for line in script)
    if decision.status == "PROVEN":
        return stated.statement, (f"have root {lane} := by", *nested, *stated.from_root)
    return stated.statement, (
        f"have refuted {lane} := by",
        *nested,
        "intro claim",
        *stated.to_root,
    )


def _split_prelude(prelude: str) -> tuple[tuple[str, ...], str]:
    imports = []
    rest = []
    for line in prelude.split("\n"):
        match = _IMPORT.match(line)
        if match:
            imports.append(match.group(1))
        else:
            rest.append(line)
    return tuple(imports), "\n".join(rest).strip("\n")


def _refuting(statement: str, script: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """State the claim a refutation is about, and prove its negation.

    The decider proved a statement that implies ``¬ claim``, so the script
    proves that statement and then the negation from it.
    """
    nested = tuple("  " + line for line in script)
    if statement == _ROOT_REFUTATION:
        return ": forsetiRoot", (
            f"have refuted {_ROOT_REFUTATION} := by",
            *nested,
            "exact fun root => refuted.2"
            " (show forsetiIdentities ∧ forsetiContract from root).2",
        )
    if statement.startswith(": ¬ "):
        claim = statement.removeprefix(": ¬ ")
        return f": {claim}", (
            f"have refuted : ¬ {claim} := by",
            *nested,
            "exact refuted",
        )
    raise DecisionError(f"no claim shape for the refutation {statement!r}")
