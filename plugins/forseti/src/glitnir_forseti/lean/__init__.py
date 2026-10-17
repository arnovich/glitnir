"""Untrusted tooling for observing the pinned Lean elaborator.

This package is the search-side channel to Lean: it elaborates a source unit,
reports the diagnostics Lean produced and exposes the goals a tactic block
left open. It observes an untrusted process and grants no proof authority.
A script establishes something only when a unit containing it elaborates
cleanly, with no ``sorry`` and no axiom outside the allowlist, and a
declaration admitted with ``sorry`` is a failed attempt rather than a result.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from glitnir.lean.limits import SessionLimits

from .goals import Goal, GoalState, Hypothesis, parse_goal_state
from .messages import (
    HAS_SORRY_KIND,
    UNSOLVED_GOALS_KIND,
    LeanMessage,
    MessageSeverity,
    MessageStream,
    SourcePosition,
    parse_messages,
)
from .unit import THEOREM_NAME, render_unit

if TYPE_CHECKING:
    from .session import (
        NO_EXIT_CODE,
        Elaboration,
        LeanSession,
        RecordedLeanSession,
        SessionStatus,
        SubprocessLeanSession,
    )

#: Session names load on first use: the session runs the pinned Lean release,
#: and importing project-mode modules from this package must not load it.
_SESSION_NAMES = frozenset(
    {
        "NO_EXIT_CODE",
        "Elaboration",
        "LeanSession",
        "RecordedLeanSession",
        "SessionStatus",
        "SubprocessLeanSession",
    }
)


def __getattr__(name: str) -> Any:
    """Load a session name from its module the first time it is used.

    Args:
        name: The attribute requested from this package.

    Returns:
        The named object from ``glitnir_forseti.lean.session``.

    Raises:
        AttributeError: If the name is not a lazily loaded session name.
    """
    if name not in _SESSION_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(".session", __name__), name)
    globals()[name] = value
    return value


__all__ = [
    "Elaboration",
    "Goal",
    "GoalState",
    "HAS_SORRY_KIND",
    "Hypothesis",
    "LeanMessage",
    "LeanSession",
    "MessageSeverity",
    "MessageStream",
    "NO_EXIT_CODE",
    "RecordedLeanSession",
    "SessionLimits",
    "SessionStatus",
    "SourcePosition",
    "SubprocessLeanSession",
    "THEOREM_NAME",
    "UNSOLVED_GOALS_KIND",
    "parse_goal_state",
    "parse_messages",
    "render_unit",
]
