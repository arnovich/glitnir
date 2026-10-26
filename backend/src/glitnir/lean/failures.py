"""Where a Lean stage failed, and how to say so."""

import re

from glitnir.lean.project_snapshot import SnapshotError


def line_range(text: str, start: int, end: int) -> tuple[int, int]:
    """Return 1-based inclusive lines for UTF-8 byte offsets into LF text."""
    data = text.encode()
    first = data.count(b"\n", 0, start) + 1
    last = data.count(b"\n", 0, max(start, end - 1)) + 1
    return first, last


class CommandFailure(SnapshotError):
    """A Lean command failed; everything before ``offset`` may still be checked."""

    def __init__(self, message: str, *, offset: int, note: str = "") -> None:
        """Record the failure message, the command's byte offset and a note."""
        super().__init__(message)
        self.offset = offset
        self.note = note


_POSITION = re.compile(r"^.*?:(\d+):(\d+): error", re.MULTILINE)


def first_error_offset(stderr: str, text: str) -> int | None:
    """Return the byte offset of the first positioned Lean error, if any."""
    match = _POSITION.search(stderr)
    if match is None:
        return None
    line, column = int(match[1]), int(match[2])
    lines = text.split("\n")
    if not 1 <= line <= len(lines):
        return None
    before = "\n".join(lines[: line - 1])
    prefix = before + ("\n" if line > 1 else "") + lines[line - 1][:column]
    return len(prefix.encode())


def failure_message(error: str) -> str:
    """Return the headline of a Lean failure for display, at most 500 characters."""
    lines = [
        x.strip()
        for x in error.splitlines()
        if x.strip() and not x.startswith("lean did not complete successfully")
    ]
    if not lines:
        return ""
    positioned = next((x for x in lines if ": error" in x), None)
    if positioned is not None:
        return positioned.split(": error", 1)[-1].lstrip(": ").strip()[:500]
    return lines[0][:500]
