"""Remaining tactic goals parsed from a Lean ``unsolved goals`` message.

Lean pretty-prints the open goals of a failed tactic block as text.  This
module turns that text into exact immutable values so a caller never re-parses
the raw message payload.  The result describes what a search has left to do; it
is search state, not evidence, and it grants no proof authority.

The printed form is not a versioned API.  Parsing is therefore total and
conservative: anything that does not match the expected shape is retained as
text rather than discarded, and the limits below bound a hostile payload.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_GOALS = 256
MAX_HYPOTHESES = 512

_HEADER = "unsolved goals"
_CASE_PREFIX = "case "
_TURNSTILE = "⊢"
_SEPARATOR = " : "


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One hypothesis line of a goal context."""

    names: tuple[str, ...]
    type_text: str

    def __post_init__(self) -> None:
        """Reject malformed name containers and non-string types."""
        if type(self.names) is not tuple or any(
            type(name) is not str for name in self.names
        ):
            raise TypeError("names must be a tuple of strings")
        if type(self.type_text) is not str:
            raise TypeError("type_text must be a string")


@dataclass(frozen=True, slots=True)
class Goal:
    """One remaining goal: its optional case label, context and target."""

    case: str | None
    hypotheses: tuple[Hypothesis, ...]
    target: str

    def __post_init__(self) -> None:
        """Reject malformed labels, contexts and targets."""
        if self.case is not None and type(self.case) is not str:
            raise TypeError("case must be a string or None")
        if type(self.hypotheses) is not tuple or any(
            type(value) is not Hypothesis for value in self.hypotheses
        ):
            raise TypeError("hypotheses must be a tuple of Hypothesis values")
        if type(self.target) is not str:
            raise TypeError("target must be a string")


@dataclass(frozen=True, slots=True)
class GoalState:
    """Every goal left open by one tactic block."""

    goals: tuple[Goal, ...]
    truncated: bool

    def __post_init__(self) -> None:
        """Reject malformed goal containers and flags."""
        if type(self.goals) is not tuple or any(
            type(goal) is not Goal for goal in self.goals
        ):
            raise TypeError("goals must be a tuple of Goal values")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")

    def is_closed(self) -> bool:
        """Report whether no goal remains open.

        Returns:
            True when the state carries no goal.
        """
        return not self.goals


def _hypotheses(lines: list[str]) -> tuple[tuple[Hypothesis, ...], bool]:
    collected: list[tuple[list[str], list[str]]] = []
    truncated = False
    for line in lines:
        continuation = line.startswith((" ", "\t")) or _SEPARATOR not in line
        if continuation and collected:
            collected[-1][1].append(line.strip())
            continue
        if len(collected) >= MAX_HYPOTHESES:
            truncated = True
            break
        if _SEPARATOR in line:
            names, _, type_text = line.partition(_SEPARATOR)
            collected.append((names.split(), [type_text.strip()]))
        else:
            collected.append(([], [line.strip()]))
    return (
        tuple(
            Hypothesis(tuple(names), " ".join(part for part in parts if part))
            for names, parts in collected
        ),
        truncated,
    )


def _goal(block: str) -> tuple[Goal, bool]:
    lines = block.splitlines()
    case: str | None = None
    if lines and lines[0].startswith(_CASE_PREFIX):
        case = lines[0][len(_CASE_PREFIX) :].strip()
        lines = lines[1:]
    target_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.lstrip().startswith(_TURNSTILE)
        ),
        None,
    )
    hypotheses: tuple[Hypothesis, ...]
    if target_index is None:
        hypotheses, truncated = (), False
        target = "\n".join(lines).strip()
    else:
        hypotheses, truncated = _hypotheses(lines[:target_index])
        remainder = list(lines[target_index:])
        remainder[0] = remainder[0].lstrip()[len(_TURNSTILE) :]
        target = "\n".join(remainder).strip()
    return Goal(case, hypotheses, target), truncated


def parse_goal_state(data: str) -> GoalState:
    """Parse the payload of an ``unsolved goals`` message.

    Args:
        data: The ``data`` field of a ``Tactic.unsolvedGoals`` message.

    Returns:
        The ordered remaining goals, flagged when a limit stopped parsing.
    """
    if type(data) is not str:
        raise TypeError("goal payload must be a string")
    body = data.strip()
    if body.startswith(_HEADER):
        body = body[len(_HEADER) :].lstrip("\n")
    if not body.strip():
        return GoalState((), False)
    goals: list[Goal] = []
    truncated = False
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if len(goals) >= MAX_GOALS:
            truncated = True
            break
        goal, limited = _goal(block)
        truncated = truncated or limited
        goals.append(goal)
    return GoalState(tuple(goals), truncated)
