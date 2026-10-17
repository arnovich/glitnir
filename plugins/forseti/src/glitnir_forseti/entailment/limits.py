"""Explicit resource limits for one entailment request.

Every limit has a default here, and the effective values are retained with each
result and artifact, so a run is reproducible from what it recorded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

#: Hard ceilings: a caller may lower a limit, never raise it past these.
CEILINGS = {
    "wall_time_ms": 1_800_000,
    "max_variables": 32,
    "max_nodes": 16_384,
    "max_depth": 256,
    "max_degree": 32,
    "max_monomials": 65_536,
    "max_dictionary": 64,
    "max_weights": 4_096,
    "max_coefficient_bits": 4_096,
    "max_artifact_bytes": 16_777_216,
    "max_lean_output_bytes": 1_048_576,
    "max_heartbeats": 10_000_000,
}


class LimitError(ValueError):
    """A limit value was not a positive integer within its ceiling."""


@dataclass(frozen=True, slots=True)
class EntailmentLimits:
    """Ceilings for one request; each is a positive integer.

    Attributes:
        wall_time_ms: The whole request, solver and Lean together.
        max_variables: Coordinates in the goal's context.
        max_nodes: Syntax nodes across both formulas.
        max_depth: Nesting depth of any formula.
        max_degree: Degree of any polynomial the certificate lane expands.
        max_monomials: Monomials any polynomial expansion may retain, including
            Euler mode tables, bounded while the expansion runs.
        max_dictionary: Polynomials in the square dictionary.
        max_weights: Unknown weights handed to the solver.
        max_coefficient_bits: Bits of any numerator or denominator, in goals,
            evidence and solver answers.
        max_artifact_bytes: The encoded artifact.
        max_lean_output_bytes: Output each Lean process may print.
        max_heartbeats: Lean's elaboration budget per check.
    """

    wall_time_ms: int = 300_000
    max_variables: int = 8
    max_nodes: int = 512
    max_depth: int = 64
    max_degree: int = 8
    max_monomials: int = 4_096
    max_dictionary: int = 16
    max_weights: int = 512
    max_coefficient_bits: int = 256
    max_artifact_bytes: int = 1_048_576
    max_lean_output_bytes: int = 1_048_576
    max_heartbeats: int = 2_000_000

    def __post_init__(self) -> None:
        """Reject a non-integer, non-positive or over-ceiling limit."""
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= CEILINGS[field.name]:
                raise LimitError(
                    f"{field.name} must be an integer in 1..{CEILINGS[field.name]}"
                )

    def as_dict(self) -> dict[str, int]:
        """Return the effective limits, for retention."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> EntailmentLimits:
        """Read retained limits, refusing unknown or missing keys."""
        names = {field.name for field in fields(cls)}
        if type(data) is not dict or set(data) != names:
            raise LimitError("limits must name exactly the known limits")
        return cls(**data)
