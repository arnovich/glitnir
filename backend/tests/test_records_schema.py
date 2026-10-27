"""The records validators enforce exactly the declared shapes."""

from glitnir.lean.results import Use, Verdict, Withheld
from glitnir.notebook import check_records
from glitnir.notebook import records_schema as schema


def _keys(shape):
    return set(shape.__required_keys__) | set(shape.__optional_keys__)


def test_claim_record_keys_match_the_validator():
    """`_CLAIM_KEYS` is exactly the required part of a claim, with its value types."""
    assert set(schema._CLAIM_KEYS) == set(schema.ClaimRecord.__required_keys__)
    for key, kind in schema._CLAIM_KEYS.items():
        annotation = schema.ClaimRecord.__annotations__[key]
        assert annotation is kind or getattr(annotation, "__origin__", None) is kind


def test_claim_record_optional_keys_match_the_validator():
    """The keys `well_formed` checks by name when present are the optional keys."""
    assert set(schema.ClaimRecord.__optional_keys__) == {
        "example",
        "statement",
        "statement_shortened",
        "kernel_statement",
        "kernel_statement_shortened",
        "uses",
        "uses_truncated",
        "fingerprint",
    }


def test_non_claim_record_keys_match_the_validator():
    assert set(schema._NON_CLAIM_KEYS) == _keys(schema.NonClaimRecord)
    for key, kind in schema._NON_CLAIM_KEYS.items():
        assert schema.NonClaimRecord.__annotations__[key] is kind


def test_evidence_entry_keys_match_the_validator():
    assert {*schema._EVIDENCE_KEYS, "snapshot"} == _keys(schema.EvidenceEntry)
    assert all(
        schema.EvidenceEntry.__annotations__[key] is str
        for key in schema._EVIDENCE_KEYS
    )


def test_view_entry_keys_match_the_validator():
    assert {*schema._VIEW_KEYS, "drawing"} == _keys(schema.ViewEntry)
    assert all(
        schema.ViewEntry.__annotations__[key] is str for key in schema._VIEW_KEYS
    )


def test_decision_entry_keys_match_the_validator():
    assert {*schema._EVIDENCE_KEYS, "outcome", "verification"} == _keys(
        schema.DecisionEntry
    )
    assert all(
        schema.DecisionEntry.__annotations__[key] is str
        for key in schema._EVIDENCE_KEYS
    )


def test_failure_entry_keys_match_the_validator():
    assert set(schema._FAILURE_KEYS) == _keys(schema.FailureEntry)
    assert all(
        schema.FailureEntry.__annotations__[key] is str for key in schema._FAILURE_KEYS
    )


def test_verification_keys_match_the_verdict():
    assert schema._VERIFICATION_KEYS == _keys(Verdict)


def test_use_keys_match_the_validator():
    assert schema._USE_KEYS == _keys(Use)


def test_withheld_keys_match_the_validator():
    assert schema._WITHHELD_KEYS == set(Withheld.__required_keys__)
    assert set(Withheld.__optional_keys__) == {"through_line"}


def test_records_sections_are_the_optional_keys():
    """Every result section the writer keeps is an optional key of the records."""
    assert {"non_claims", *check_records._RESULT_SECTIONS} == set(
        schema.Records.__optional_keys__
    )
    assert set(schema.Records.__required_keys__) == {
        "format",
        "source_sha256",
        "environment",
        "withheld",
        "claims",
    }
