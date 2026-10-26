"""Where a Lean stage failed, and how to say so."""

import pytest

from glitnir.lean.failures import first_error_offset


def test_first_error_offset_stays_within_the_text():
    """A column past the line's end is the line's end; a line past the text is none."""
    text = "theorem a : True := trivial\nbad\n"
    assert first_error_offset("A.lean:2:40: error: x", text) == len(
        "theorem a : True := trivial\nbad"
    )
    assert first_error_offset("A.lean:9:0: error: x", text) is None


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_line_range_counts_utf8_bytes(newline):
    """Reader byte offsets map to the same 1-based lines for LF and CRLF files."""
    from glitnir.lean.failures import line_range

    raw = newline.join(["-- λ header", "theorem a : True := by", "  trivial", ""])
    text = raw.replace("\r\n", "\n")
    start = text.index("theorem a")
    end = text.index("trivial") + len("trivial")
    start_byte = len(text[:start].encode())
    end_byte = len(text[:end].encode())
    assert line_range(text, start_byte, end_byte) == (2, 3)


def test_first_error_offset_reads_lean_positions():
    """A positioned Lean error maps to a byte offset; other output does not."""
    from glitnir.lean.failures import first_error_offset

    text = "theorem a : True := True.intro\ntheorem \u03bbb : 1 = 2 := rfl\n"
    stderr = (
        "/tmp/x/Source.lean:2:22: error: type mismatch\n"
        "/tmp/x/Source.lean:2:0: warning: unused\n"
    )
    offset = first_error_offset(stderr, text)
    assert offset == len(
        "theorem a : True := True.intro\ntheorem \u03bbb : 1 = 2 := ".encode()
    )
    assert first_error_offset("lean: out of memory\n", text) is None


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            "lean did not complete successfully (exit 4).\n"
            "unsupported example proof syntax",
            "unsupported example proof syntax",
        ),
        (
            "lean did not complete successfully (exit 3).\ntype mismatch\n  False\n"
            "has type\n  Prop : Type\nbut is expected to have type\n  Nat : Type",
            "type mismatch",
        ),
        (
            "lean did not complete successfully (exit 1).\n"
            "/tmp/x/Source.lean:2:23: error: The rfl tactic failed",
            "The rfl tactic failed",
        ),
        ("lean did not complete successfully (exit 1).\n", ""),
    ],
)
def test_failure_message_skips_the_exit_header(error, expected):
    """The displayed message is the Lean diagnostic, not the exit-status header."""
    from glitnir.lean.failures import failure_message

    assert failure_message(error) == expected
    assert len(failure_message("x" * 900)) == 500
