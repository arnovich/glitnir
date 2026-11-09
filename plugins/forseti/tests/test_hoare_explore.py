"""Source-backed Hoare proof contributions for the Explore tab."""

from glitnir_forseti.hoare_explore import hoare_proofs


def test_sequential_proof_links_premises_in_rule_order() -> None:
    """The composition rule links declarations, with the middle predicate shared."""
    proofs = hoare_proofs(
        {
            "Demo.first": "theorem first : ExactHoare P double M := by\n  trivial\n",
            "Demo.second": "theorem second : ExactHoare M triple Q := by\n  trivial\n",
            "Demo.whole": (
                "theorem whole : ExactHoare P pipeline Q := by\n"
                "  exact exactHoareSequential first second\n"
            ),
        }
    )
    whole = proofs["Demo.whole"]
    assert (whole["pre"], whole["command"], whole["post"]) == ("P", "pipeline", "Q")
    assert whole["rule"] == "Sequential composition"
    assert whole["premises"] == ["Demo.first", "Demo.second"]
    assert proofs["Demo.first"]["rule"] is None


def test_unknown_or_inconsistent_rule_keeps_the_triple() -> None:
    """Missing premises or a different middle predicate leave the rule open."""
    proofs = hoare_proofs(
        {
            "Demo.first": "theorem first : ExactHoare P double N := by\n  trivial\n",
            "Demo.second": "theorem second : ExactHoare M triple Q := by\n  trivial\n",
            "Demo.whole": (
                "theorem whole : ExactHoare P pipeline Q := by\n"
                "  exact exactHoareSequential first second\n"
            ),
            "Demo.other": "theorem other : True := by\n  trivial\n",
        }
    )
    assert "Demo.other" not in proofs
    assert proofs["Demo.whole"]["rule"] is None
    assert proofs["Demo.whole"]["premises"] == []
