"""Tests for the structured ``lean --json`` message and goal-state channel."""

import json

import pytest

from glitnir_forseti.lean.goals import (
    MAX_GOALS,
    Goal,
    GoalState,
    Hypothesis,
    parse_goal_state,
)
from glitnir_forseti.lean.messages import (
    MAX_MESSAGE_TEXT_BYTES,
    MAX_MESSAGES,
    LeanMessage,
    MessageSeverity,
    MessageStream,
    SourcePosition,
    parse_messages,
)

# Captured from leanprover/lean4:v4.32.1 running `lean --json`; v4.35.0 emits
# the same documents.
UNSOLVED = {
    "caption": "",
    "data": "unsolved goals\nx : Nat\n⊢ x + 0 = x",
    "endPos": {"column": 6, "line": 2},
    "fileName": "probe.lean",
    "isSilent": False,
    "keepFullRange": False,
    "kind": "Tactic.unsolvedGoals",
    "pos": {"column": 33, "line": 1},
    "severity": "error",
}
SORRY = {
    "data": "declaration uses `sorry`",
    "endPos": {"column": 10, "line": 1},
    "kind": "hasSorry",
    "pos": {"column": 8, "line": 1},
    "severity": "warning",
}
AXIOMS = {
    "data": "'t1' depends on axioms: [sorryAx]",
    "endPos": {"column": 0, "line": 7},
    "kind": "[anonymous]",
    "pos": {"column": 0, "line": 7},
    "severity": "information",
}
TWO_GOALS = (
    "unsolved goals\n"
    "case left\nx : Nat\nh : x > 2\n⊢ x > 1\n\n"
    "case right\nx : Nat\nh : x > 2\n⊢ x > 0"
)


def stream(*documents: object) -> bytes:
    return "\n".join(json.dumps(document) for document in documents).encode("utf-8")


def test_parses_the_observed_message_vocabulary() -> None:
    result = parse_messages(stream(UNSOLVED, SORRY, AXIOMS))
    assert result.unreadable_lines == 0
    assert not result.truncated
    kinds = tuple(message.kind for message in result.messages)
    assert kinds == ("Tactic.unsolvedGoals", "hasSorry", "[anonymous]")
    severities = tuple(message.severity for message in result.messages)
    assert severities == (
        MessageSeverity.ERROR,
        MessageSeverity.WARNING,
        MessageSeverity.INFORMATION,
    )
    first = result.messages[0]
    assert first.start == SourcePosition(1, 33)
    assert first.end == SourcePosition(2, 6)
    assert first.text.startswith("unsolved goals")


def test_sorry_and_errors_are_reported_separately() -> None:
    result = parse_messages(stream(UNSOLVED, SORRY))
    assert result.has_sorry()
    assert tuple(message.kind for message in result.errors()) == (
        "Tactic.unsolvedGoals",
    )
    assert len(result.unsolved_goals()) == 1
    assert not parse_messages(stream(UNSOLVED)).has_sorry()


def test_unreadable_lines_are_counted_not_dropped_or_raised() -> None:
    raw = b'{"severity": "error"}\nnot json at all\n123\n\n'
    result = parse_messages(raw)
    assert len(result.messages) == 1
    assert result.unreadable_lines == 2


def test_unmodelled_severity_and_missing_fields_stay_total() -> None:
    result = parse_messages(stream({"severity": "catastrophe"}, {}))
    assert tuple(message.severity for message in result.messages) == (
        MessageSeverity.UNKNOWN,
        MessageSeverity.UNKNOWN,
    )
    assert all(message.kind == "" for message in result.messages)
    assert all(message.text == "" for message in result.messages)
    assert all(message.start == SourcePosition(0, 0) for message in result.messages)


def test_message_limit_truncates_explicitly() -> None:
    result = parse_messages(stream(*([UNSOLVED] * (MAX_MESSAGES + 5))))
    assert result.truncated
    assert len(result.messages) == MAX_MESSAGES


def test_parse_messages_rejects_non_bytes() -> None:
    with pytest.raises(TypeError):
        parse_messages("{}")  # type: ignore[arg-type]


def test_message_values_reject_hostile_construction() -> None:
    origin = SourcePosition(1, 0)
    with pytest.raises(TypeError):
        LeanMessage("error", "", origin, origin, "")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourcePosition(-1, 0)
    with pytest.raises(TypeError):
        SourcePosition(True, 0)
    with pytest.raises(TypeError):
        MessageStream((), "none", False)  # type: ignore[arg-type]


def test_single_goal_carries_context_and_target() -> None:
    state = parse_goal_state(UNSOLVED["data"])
    assert not state.truncated
    assert not state.is_closed()
    (goal,) = state.goals
    assert goal.case is None
    assert goal.hypotheses == (Hypothesis(("x",), "Nat"),)
    assert goal.target == "x + 0 = x"


def test_case_labelled_goals_are_split() -> None:
    state = parse_goal_state(TWO_GOALS)
    assert tuple(goal.case for goal in state.goals) == ("left", "right")
    assert tuple(goal.target for goal in state.goals) == ("x > 1", "x > 0")
    assert state.goals[0].hypotheses == (
        Hypothesis(("x",), "Nat"),
        Hypothesis(("h",), "x > 2"),
    )


def test_shared_binder_names_stay_grouped() -> None:
    (goal,) = parse_goal_state("unsolved goals\nx y : Nat\n⊢ x = y").goals
    assert goal.hypotheses == (Hypothesis(("x", "y"), "Nat"),)


def test_goal_without_context_or_header_is_accepted() -> None:
    (goal,) = parse_goal_state("⊢ True").goals
    assert goal.hypotheses == ()
    assert goal.target == "True"


def test_empty_payload_is_a_closed_state() -> None:
    assert parse_goal_state("unsolved goals").is_closed()
    assert parse_goal_state("   ").is_closed()


def test_goal_limit_truncates_explicitly() -> None:
    payload = "\n\n".join(["⊢ True"] * (MAX_GOALS + 3))
    state = parse_goal_state(payload)
    assert state.truncated
    assert len(state.goals) == MAX_GOALS


def test_goal_values_reject_hostile_construction() -> None:
    with pytest.raises(TypeError):
        Hypothesis(["x"], "Nat")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Goal(None, (), 3)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        GoalState((), "no")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_goal_state(b"bytes")  # type: ignore[arg-type]


def test_oversized_message_text_is_flagged_not_silently_cut() -> None:
    oversized = dict(UNSOLVED, data="x" * (MAX_MESSAGE_TEXT_BYTES + 32))
    result = parse_messages(stream(UNSOLVED, oversized))
    assert result.any_text_truncated()
    assert result.messages[0].text_truncated is False
    assert result.messages[1].text_truncated is True
    assert len(result.messages[1].text.encode("utf-8")) == MAX_MESSAGE_TEXT_BYTES


def test_text_truncation_flag_is_validated() -> None:
    origin = SourcePosition(1, 0)
    flag = "yes"
    error = MessageSeverity.ERROR
    with pytest.raises(TypeError):
        LeanMessage(error, "", origin, origin, "", flag)  # type: ignore[arg-type]
