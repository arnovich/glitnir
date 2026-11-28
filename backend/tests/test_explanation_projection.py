"""Saved-source explanation projection for the Lean extension pilot."""

from pathlib import Path

from glitnir.preview.explanations import project_explanations
from glitnir.preview.snapshot import PreviewState, build_snapshot, indexed_cells


def test_projects_annotated_tactics_without_elaboration():
    """Two adjacent annotations keep their prose and source locations."""
    source = (
        "import Glitnir\n"
        "theorem t (P Q R : Prop) (h : P → Q) (k : Q → R) (p : P) : R := by\n"
        '  explain "derive-q" "First obtain Q.\\nThen use it." =>\n'
        "    have q : Q := h p\n"
        '  explain "derive-r" "Use Q to obtain R." =>\n'
        "    exact k q\n"
    )
    cells = indexed_cells(source)
    assert cells[1]["authored_claim"] == "t"
    assert project_explanations(source, cells) == [
        {
            "authored_id": "derive-q",
            "markdown": "First obtain Q.\nThen use it.",
            "line": 3,
            "cell": 1,
        },
        {
            "authored_id": "derive-r",
            "markdown": "Use Q to obtain R.",
            "line": 5,
            "cell": 1,
        },
    ]


def test_does_not_project_comments_strings_or_malformed_annotations():
    """Lexical projection fails closed rather than inventing explanation cards."""
    source = (
        "theorem t : True := by\n"
        '  -- explain "comment" "hidden" =>\n'
        '  let s := "explain \\"literal\\" \\"hidden\\" =>"\n'
        '  explain "missing-arrow" "hidden"\n'
        "  trivial\n"
    )
    assert project_explanations(source, indexed_cells(source)) == []


def test_duplicate_ids_are_ambiguous():
    """An authored ID must identify one annotation in the saved file."""
    source = (
        "theorem t : True := by\n"
        '  explain "same" "First" => trivial\n'
        '  explain "same" "Second" => trivial\n'
    )
    assert project_explanations(source, indexed_cells(source)) == []


def test_ids_are_scoped_by_claim_and_quotations_are_not_projected():
    """Other declarations may reuse an ID; quoted syntax stays raw Lean."""
    source = (
        'theorem first : True := by\n  explain "step" "First" => trivial\n'
        'theorem second : True := by\n  explain "step" "Second" => trivial\n'
    )
    assert [
        item["markdown"] for item in project_explanations(source, indexed_cells(source))
    ] == [
        "First",
        "Second",
    ]
    quoted = (
        "import Lean\n"
        "def quoted : Lean.Syntax := `(by\n"
        '  explain "quoted" "Not a proof step." =>\n'
        "    trivial\n)\n"
    )
    assert project_explanations(quoted, indexed_cells(quoted)) == []


def test_invalid_unicode_escape_keeps_preview_readable():
    """A lone surrogate in an authored string does not break the snapshot."""
    source = 'theorem t : True := by\n  explain "id" "\\ud800" =>\n    trivial\n'
    assert project_explanations(source, indexed_cells(source)) == []


def test_counter_example_appears_in_actual_saved_snapshot():
    """The adapted existing proof shows two explanations before any Lean run."""
    path = Path(__file__).resolve().parents[2] / "lean-extension/Examples/Counter.lean"
    snapshot = build_snapshot(
        path, path.read_text(), lambda: None, PreviewState(), lambda: None
    )
    assert [
        (item["authored_id"], item["markdown"], item["line"])
        for item in snapshot["explanations"]
    ] == [
        ("recurrence", "The recursive definition supplies the successor equation.", 30),
        (
            "induction-step",
            "Rewrite with the recurrence, apply the induction hypothesis, "
            "then rearrange the sum.",
            32,
        ),
    ]
    assert snapshot["project_claims"] == []
