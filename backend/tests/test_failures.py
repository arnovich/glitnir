"""Where a Lean stage failed, and how to say so."""

from glitnir.lean.failures import first_error_offset


def test_first_error_offset_stays_within_the_text():
    """A column past the line's end is the line's end; a line past the text is none."""
    text = "theorem a : True := trivial\nbad\n"
    assert first_error_offset("A.lean:2:40: error: x", text) == len(
        "theorem a : True := trivial\nbad"
    )
    assert first_error_offset("A.lean:9:0: error: x", text) is None
