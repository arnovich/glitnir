"""The public comparison example stays pinned, incomplete and candid before its run."""

import hashlib
from pathlib import Path


ROOT = Path(__file__).parents[2] / "examples/two-round-lean-evaluation"


def test_prepared_two_round_example_has_no_hidden_completed_proof() -> None:
    source = (ROOT / "TwoRoundTrial.lean").read_text()
    report = (ROOT / "REPORT.md").read_text()
    assert "theorem target (a b c : ℝ) (h : a + b + c = 0)" in source
    assert "/- BEGIN PROOF target -/\n  sorry\n/- END PROOF target -/" in source
    assert "execution not admitted" in report
    assert "Allowed axioms: none" in report


def test_report_pins_the_current_template_and_environment() -> None:
    source = (ROOT / "TwoRoundTrial.lean").read_bytes()
    report = (ROOT / "REPORT.md").read_text()
    assert hashlib.sha256(source).hexdigest() in report
    assert "leanprover/lean4:v4.35.0" in report
    assert "2c6238eff6de60d63e45e10c93cdc7239de7ad69" in report
    assert "c33461ada7554b5d422bfb549ea40e0c83355f115d1de52bd75f2e1227908c7e" in report
