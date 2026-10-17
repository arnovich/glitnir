"""Structured Lean diagnostic messages from the ``lean --json`` channel.

The Lean elaborator emits one JSON object per message when run with
``--json``.  This module owns the total, bounded translation of that byte
stream into exact immutable values.  It observes an untrusted process: nothing
here grants proof authority, and a message claiming success is not a proof.

Parsing is deliberately total.  An unreadable line, an unknown message kind or
a malformed position is recorded rather than raised or silently dropped, so a
caller always learns that the channel returned something it did not model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

MAX_MESSAGE_TEXT_BYTES = 262_144
MAX_MESSAGES = 4_096

UNSOLVED_GOALS_KIND = "Tactic.unsolvedGoals"
HAS_SORRY_KIND = "hasSorry"


class MessageSeverity(str, Enum):
    """Severity reported by the Lean elaborator, plus an unmodelled value."""

    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: object) -> "MessageSeverity":
        """Map a reported severity onto the closed set, never raising.

        Args:
            value: The ``severity`` field of one reported message.

        Returns:
            The matching severity, or ``UNKNOWN`` for anything unmodelled.
        """
        if type(value) is not str:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class SourcePosition:
    """A one-based line and zero-based column inside an elaborated unit."""

    line: int
    column: int

    def __post_init__(self) -> None:
        """Reject non-integer, Boolean or negative coordinates."""
        for label, value in (("line", self.line), ("column", self.column)):
            if type(value) is not int:
                raise TypeError(f"{label} must be a non-Boolean integer")
            if value < 0:
                raise ValueError(f"{label} must not be negative")


UNKNOWN_POSITION = SourcePosition(0, 0)


@dataclass(frozen=True, slots=True)
class LeanMessage:
    """One diagnostic observed from an untrusted Lean process."""

    severity: MessageSeverity
    kind: str
    start: SourcePosition
    end: SourcePosition
    text: str
    text_truncated: bool = False

    def __post_init__(self) -> None:
        """Own exact bounded fields from even a hostile runner."""
        if type(self.text_truncated) is not bool:
            raise TypeError("text_truncated must be a bool")
        if type(self.severity) is not MessageSeverity:
            raise TypeError("severity must be a MessageSeverity")
        for label, value in (("kind", self.kind), ("text", self.text)):
            if type(value) is not str:
                raise TypeError(f"{label} must be a string")
        for label, position in (("start", self.start), ("end", self.end)):
            if type(position) is not SourcePosition:
                raise TypeError(f"{label} must be a SourcePosition")
        if len(self.text.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
            raise ValueError("message text exceeds its byte limit")


@dataclass(frozen=True, slots=True)
class MessageStream:
    """Every message read from one elaboration, with what could not be read."""

    messages: tuple[LeanMessage, ...]
    unreadable_lines: int
    truncated: bool

    def __post_init__(self) -> None:
        """Reject malformed containers and counters."""
        if type(self.messages) is not tuple or any(
            type(message) is not LeanMessage for message in self.messages
        ):
            raise TypeError("messages must be a tuple of LeanMessage values")
        if type(self.unreadable_lines) is not int:
            raise TypeError("unreadable_lines must be a non-Boolean integer")
        if self.unreadable_lines < 0:
            raise ValueError("unreadable_lines must not be negative")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")

    def errors(self) -> tuple[LeanMessage, ...]:
        """Return every message reported at error severity.

        Returns:
            The ordered error messages.
        """
        return tuple(
            message
            for message in self.messages
            if message.severity is MessageSeverity.ERROR
        )

    def any_text_truncated(self) -> bool:
        """Report whether any message lost text to the byte ceiling.

        A caller reading structured data out of message text must treat this
        as lost content rather than as the end of the data.

        Returns:
            True when at least one message was truncated.
        """
        return any(message.text_truncated for message in self.messages)

    def has_sorry(self) -> bool:
        """Report whether any declaration was admitted with ``sorry``.

        Returns:
            True when Lean reported the ``hasSorry`` message kind.
        """
        return any(message.kind == HAS_SORRY_KIND for message in self.messages)

    def unsolved_goals(self) -> tuple[LeanMessage, ...]:
        """Return the messages carrying remaining tactic goals.

        Returns:
            The ordered unsolved-goal messages.
        """
        return tuple(
            message for message in self.messages if message.kind == UNSOLVED_GOALS_KIND
        )


def _position(value: object) -> SourcePosition:
    if type(value) is not dict:
        return UNKNOWN_POSITION
    line = value.get("line")
    column = value.get("column")
    if type(line) is not int or type(column) is not int:
        return UNKNOWN_POSITION
    if line < 0 or column < 0:
        return UNKNOWN_POSITION
    return SourcePosition(line, column)


def _text(value: object) -> tuple[str, bool]:
    if type(value) is not str:
        return "", False
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_MESSAGE_TEXT_BYTES:
        return value, False
    return encoded[:MAX_MESSAGE_TEXT_BYTES].decode("utf-8", errors="ignore"), True


def _message(document: object) -> LeanMessage | None:
    if type(document) is not dict:
        return None
    kind = document.get("kind")
    text, truncated = _text(document.get("data"))
    return LeanMessage(
        MessageSeverity.parse(document.get("severity")),
        kind if type(kind) is str else "",
        _position(document.get("pos")),
        _position(document.get("endPos")),
        text,
        truncated,
    )


def parse_messages(raw: bytes) -> MessageStream:
    """Translate a ``lean --json`` byte stream into exact message values.

    Args:
        raw: The captured standard output of one Lean process.

    Returns:
        Every readable message, the count of lines that could not be read, and
        whether the message limit stopped the translation early.
    """
    if type(raw) is not bytes:
        raise TypeError("raw output must be bytes")
    messages: list[LeanMessage] = []
    unreadable = 0
    truncated = False
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(messages) >= MAX_MESSAGES:
            truncated = True
            break
        try:
            document = json.loads(stripped)
        except ValueError:
            unreadable += 1
            continue
        message = _message(document)
        if message is None:
            unreadable += 1
            continue
        messages.append(message)
    return MessageStream(tuple(messages), unreadable, truncated)
