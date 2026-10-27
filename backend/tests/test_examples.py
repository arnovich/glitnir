"""Checking `example` commands as named theorems: the reader's account, the rewrite."""

import pytest
from conftest import EXAMPLES, claim_of, edits_of, example_entries


def test_example_tag_never_occurs_in_the_source():
    """Generated theorem names cannot be written, declared or used by the source."""
    from glitnir.lean.examples import EXAMPLE_TAG, example_tag

    assert example_tag("example : True := trivial") == EXAMPLE_TAG
    text = f"def {EXAMPLE_TAG}1 : Nat := 1 -- _{EXAMPLE_TAG}\n"
    tag = example_tag(text)
    assert tag not in text and tag.endswith(EXAMPLE_TAG)


def test_examples_are_rewritten_only_at_their_keywords():
    """The compiled text differs only where `example` became the named theorem."""
    from glitnir.lean.examples import check_rewrite, rewrite_examples

    data = EXAMPLES.encode()
    entries = example_entries(EXAMPLES)
    edits = edits_of(EXAMPLES, entries)
    second, third = entries[1]["keyword"][0], entries[2]["keyword"][0]
    assert edits == [
        (second, f"private theorem _glitnir_example_{second}".encode()),
        (third, f"private theorem _glitnir_example_{third}".encode()),
    ]
    compiled = rewrite_examples(data, edits)
    assert compiled.decode() == (
        "theorem a : True := True.intro\n"
        f"private theorem _glitnir_example_{second} : True := trivial\n"
        "/-- doc -/\n"
        f"private theorem _glitnir_example_{third} (p : Prop) (h : p) : p := h\n"
    )
    check_rewrite(data, compiled, edits)
    pairs = zip(EXAMPLES.split("\n"), compiled.decode().split("\n"), strict=True)
    for saved, built in pairs:
        if saved.startswith("example"):
            assert built.startswith("private theorem _glitnir_example_")
            assert built.endswith(saved.removeprefix("example"))
        else:
            assert built == saved
    assert rewrite_examples(data, []) == data
    check_rewrite(data, data, [])


@pytest.mark.parametrize(
    "compiled",
    [
        lambda c: c.replace(b"True.intro", b"True.Intro"),
        lambda c: c.replace(b"trivial", b"trivial\n"),
        lambda c: c.replace(b"private theorem", b"private  theorem", 1),
        lambda c: c + b"theorem b : True := trivial\n",
        lambda c: c[:-1],
    ],
)
def test_any_other_difference_in_the_compiled_text_fails(compiled):
    """The checker refuses compiled text that differs anywhere but the keywords."""
    from glitnir.lean.examples import check_rewrite, rewrite_examples
    from glitnir.lean.project_snapshot import SnapshotError

    data = EXAMPLES.encode()
    edits = edits_of(EXAMPLES, example_entries(EXAMPLES))
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, compiled(rewrite_examples(data, edits)), edits)
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, rewrite_examples(data, edits), [(0, b"theorem")])
    with pytest.raises(SnapshotError, match="differs from the saved source"):
        check_rewrite(data, data.replace(b"example", b"exampl\n", 1), [(0, b"x\n")])


def _malformed(entries):
    """Ways a reader report could misdescribe the examples of `EXAMPLES`."""
    second = entries[1]["keyword"][0]
    yield {**entries[1], "keyword": [second, second + 6]}
    yield {**entries[1], "keyword": [second + 1, second + 8]}
    yield {**entries[1], "keyword": [0, 7]}
    yield {**entries[1], "keyword": (second, second + 7)}
    yield {**entries[1], "keyword": [second, True]}
    yield {**entries[1], "end": second + 3}
    yield {**entries[1], "start": second + 1}
    yield {**entries[1], "start": 0}  # `theorem a …` is not a comment
    yield {**entries[1], "name": "_private.M.0._glitnir_example_0"}
    yield {**entries[1], "name": f"_glitnir_example_{second}.extra"}


@pytest.mark.parametrize("index", range(10))
def test_malformed_example_reports_are_refused(index):
    """Keyword spans must hold `example` inside their entry, under their name."""
    from glitnir.lean.project_snapshot import SnapshotError

    entries = example_entries(EXAMPLES)
    entries[1] = list(_malformed(entries))[index]
    with pytest.raises(SnapshotError, match="malformed|do not match"):
        edits_of(EXAMPLES, entries)


@pytest.mark.parametrize(
    "text",
    [
        "-- example : False := x\nexample : True := trivial\n",
        "/- example : False := x -/\nexample : True := trivial\n",
        "/-- example : False -/\nexample : True := trivial\n",
        'def s : String := "example : False"\nexample : True := trivial\n',
        "def «example» : Nat := 1\nexample : True := trivial\n",
    ],
)
def test_an_example_span_outside_code_is_refused(text):
    """A span on `example` text in a comment, string or name is not a keyword."""
    from glitnir.lean.project_snapshot import SnapshotError

    real = text.encode().rindex(b"example")
    fake = text.encode().index(b"example")
    assert edits_of(text, [claim_of(text, real)]) == [
        (real, f"private theorem _glitnir_example_{real}".encode())
    ]
    with pytest.raises(SnapshotError, match="do not match|malformed"):
        edits_of(text, [claim_of(text, fake), claim_of(text, real)])
    with pytest.raises(SnapshotError, match="do not match|malformed"):
        edits_of(text, [claim_of(text, fake)])


def test_the_reader_must_account_for_every_example():
    """The reader can neither drop an example nor claim one twice.

    An unaccounted keyword withholds from the end of the last reported command
    before it, where the source can still be checked alone.
    """
    from glitnir.lean.failures import CommandFailure
    from glitnir.lean.project_snapshot import SnapshotError

    text = "example : True := trivial\nexample : Nat := 3\nexample := 3\n"
    first, second, third = (
        i for i in range(len(text)) if text.startswith("example", i)
    )
    plain = [
        {"start": second, "end": third - 1, "keyword": [second, second + 7]},
        {"start": third, "end": len(text) - 1, "keyword": [third, third + 7]},
    ]
    assert edits_of(text, [claim_of(text, first)], plain) == [
        (0, b"private theorem _glitnir_example_0")
    ]
    for dropped in ([], plain[:1], plain[1:]):
        missing = (plain[:1] if not dropped or dropped == plain[1:] else plain[1:])[0]
        line = text.count("\n", 0, missing["start"]) + 1
        with pytest.raises(CommandFailure, match=f"Line {line} has") as raised:
            edits_of(text, [claim_of(text, first)], dropped)
        # Withheld from after the last reported command before it.
        ends = [first] + [p["start"] for p in dropped if p["start"] < missing["start"]]
        assert raised.value.offset == text.index("\n", max(ends)) + 1
    with pytest.raises(SnapshotError, match="do not match"):
        edits_of(text, [claim_of(text, first)], plain + plain[:1])
    with pytest.raises(CommandFailure) as raised:
        edits_of(text, [], plain)
    assert raised.value.offset == 0  # nothing before it: unavailable
    with pytest.raises(SnapshotError, match="malformed"):
        edits_of(text, [claim_of(text, first)], [{"keyword": [second, second + 7]}])


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("example : True := trivial\n", True),
        ("example:True := trivial\n", True),
        ("example (p : Prop) (h : p) : p := h\n", True),
        ("example {p : Prop} (h : p) : p := h\n", True),
        ("example [Inhabited Nat] : True := trivial\n", True),
        ("example ⦃p : Prop⦄ (h : p) : p := h\n", True),
        ("example\n  : True := trivial\n", True),
        ("-- note\n/- a -/ /-- doc -/\nexample : True := trivial\n", True),
        ("/-- one -/ /-- two -/\nexample : True := trivial\n", False),
        ("/-! module -/\nexample : True := trivial\n", False),
    ],
)
def test_a_claimed_keyword_starts_its_command(text, ok):
    """Only whitespace, comments and one docstring precede a claimed keyword."""
    from glitnir.lean.project_snapshot import SnapshotError

    keyword = text.encode().index(b"example")
    claim = claim_of(text, keyword, start=0)
    if ok:
        assert edits_of(text, [claim])[0][0] == keyword
    else:
        with pytest.raises(SnapshotError, match="malformed"):
            edits_of(text, [claim])


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("@[simp] example : True := trivial\n", True),
        ("@[instance]\nexample : Inhabited Nat := ⟨0⟩\n", True),
        ("@[simp, aesop safe [constructors]] example : True := trivial\n", True),
        ("/-- doc -/ @[simp] example : True := trivial\n", True),
        ("private example : True := trivial\n", True),
        ("protected example : True := trivial\n", True),
        ("public example : True := trivial\n", True),
        ("meta example : True := trivial\n", True),
        ("noncomputable example : True := trivial\n", True),
        ("unsafe example : True := trivial\n", True),
        ("partial example : True := trivial\n", True),
        ("nonrec example : True := trivial\n", True),
        ("/-- doc -/\nexample : True := trivial\n", False),
        ("variable [Inhabited Nat]\nexample : True := trivial\n", False),
        ("def l : List Nat := [1, 2]\nexample : True := trivial\n", False),
        ("def a : Array Nat := #[1]\nexample : True := trivial\n", False),
        ("noncomputable section\nexample : True := trivial\n", False),
        ("def x := Foo.private\nexample : True := trivial\n", False),
    ],
)
def test_attributes_and_modifiers_are_refused_by_the_lexer(text, refused):
    """A reader that hides an attribute or modifier cannot get the example checked.

    The fake report starts the claim at its keyword, so only the trusted
    lexer sees what precedes it.
    """
    from glitnir.lean.failures import CommandFailure

    keyword = text.encode().index(b"example")
    claim = claim_of(text, keyword)
    if refused:
        with pytest.raises(CommandFailure, match="attributes or modifiers") as raised:
            edits_of(text, [claim])
        assert raised.value.offset == keyword
    else:
        assert edits_of(text, [claim])[0][0] == keyword


@pytest.mark.parametrize("stray_first", [False, True])
def test_the_earliest_of_several_example_failures_wins(stray_first):
    """Stopping at the earliest reason leaves no other reason in the prefix."""
    from glitnir.lean.failures import CommandFailure

    attribute = "@[simp] example : True := trivial\n"
    stray = "def n : Lean.Name := `example\n"
    middle = stray + attribute if stray_first else attribute + stray
    text = "theorem a : True := trivial\n" + middle
    data = text.encode()
    keyword = data.index(b"example : True")
    theorem = {"name": "a", "start": 0, "end": text.index("\n")}
    claim = claim_of(text, keyword)  # a lying reader starts it at the keyword
    with pytest.raises(CommandFailure) as raised:
        edits_of(text, [theorem, claim])
    if stray_first:
        assert raised.value.offset == data.index(b"def n")
        assert "outside an example command" in str(raised.value)
    else:
        # Mid-line; `check_project` moves it back to the line's start.
        assert raised.value.offset == keyword
        assert "attributes or modifiers" in str(raised.value)


def test_an_example_keyword_outside_a_command_withholds_from_before_it():
    """A quoted `example` the reader cannot account for keeps earlier claims."""
    from glitnir.lean.failures import CommandFailure

    text = (
        "theorem a : True := trivial\n"
        "def n : Lean.Name := `example\n"
        "example : True := trivial\n"
    )
    keyword = text.encode().rindex(b"example")
    theorem = {"name": "a", "start": 0, "end": text.index("\n")}
    with pytest.raises(CommandFailure, match="Line 2 has the `example`") as raised:
        edits_of(text, [theorem, claim_of(text, keyword)])
    assert raised.value.offset == text.index("def n")


def test_an_unreadable_file_withholds_from_its_first_claimed_example():
    """Where the lexer cannot follow Lean, examples are refused, not guessed."""
    from glitnir.lean.examples import example_edits
    from glitnir.lean.failures import CommandFailure

    text = 'def s : String := s!"{1}"\nexample : True := trivial\n'
    keyword = text.encode().index(b"example")
    with pytest.raises(CommandFailure, match="cannot read this file") as raised:
        edits_of(text, [claim_of(text, keyword)])
    assert raised.value.offset == keyword
    plain = {"start": keyword, "end": len(text) - 1, "keyword": [keyword, keyword + 7]}
    # Nothing is rewritten, and its non-claims are not recorded either: they
    # were never reconciled with the lexer.
    assert example_edits(text, [], [plain], "_glitnir_example_") == ([], [])


def test_examples_using_decl_name_are_refused():
    """`decl_name%` reads the theorem's name, so such examples are not checked."""
    from glitnir.lean.failures import CommandFailure

    text = (
        "theorem a : True := trivial\nexample : decl_name% ≠ `_example := by decide\n"
    )
    keyword = text.encode().index(b"example")
    with pytest.raises(CommandFailure, match="decl_name%") as raised:
        edits_of(text, [claim_of(text, keyword)])
    assert raised.value.offset == keyword


def test_compiled_positions_map_back_to_the_saved_source():
    """Offsets after a rewritten keyword shift back; inside it, to the keyword."""
    from glitnir.lean.examples import original_offset, rewrite_examples

    data = EXAMPLES.encode()
    edits = edits_of(EXAMPLES, example_entries(EXAMPLES))
    compiled = rewrite_examples(data, edits)
    for marker in (b"theorem a", b"True := trivial", b"/-- doc", b"(p : Prop)", b"h\n"):
        assert original_offset(compiled.index(marker), edits) == data.index(marker)
    second = edits[0][0]
    for inside in range(second, second + len(edits[0][1])):
        assert original_offset(inside, edits) == second
    assert original_offset(len(compiled), edits) == len(data)
