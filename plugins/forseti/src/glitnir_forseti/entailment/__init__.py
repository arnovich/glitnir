"""Search and replay Lean-checked predicate entailments.

A request is one closed goal ``P ⊨ Q`` over an ordered context, with explicit
limits. Untrusted producers look for evidence: Z3 for a rational or
real-algebraic counterexample, and Z3 again for the weights of the
fixed-dictionary square certificate. Evidence becomes a result only through
`replay_artifact`, which regenerates the Lean checker application from the
independently supplied goal and checks it in a fresh sandboxed process against
the pinned library. The
outcome is exactly ``PROVEN``, ``REFUTED`` or ``UNKNOWN`` with a reason.

The search service lives in `glitnir_forseti.entailment.service`. The replay and
the modules this package imports never import Z3 or the producers, so an
artifact replays offline without them.
"""

from .limits import EntailmentLimits, LimitError
from .replay import (
    InvalidEvidence,
    ReplayOutcome,
    ReplayUnavailable,
    ValidatedEntailment,
    replay_artifact,
)
from .syntax import (
    Add,
    And,
    Atom,
    Const,
    Context,
    Fls,
    Formula,
    Goal,
    GoalError,
    Mul,
    Neg,
    Not,
    Or,
    Poly,
    Relation,
    Tru,
    Var,
    decode_goal,
    encode_goal,
)

__all__ = [
    "Add",
    "And",
    "Atom",
    "Const",
    "Context",
    "EntailmentLimits",
    "Fls",
    "Formula",
    "Goal",
    "GoalError",
    "InvalidEvidence",
    "LimitError",
    "Mul",
    "Neg",
    "Not",
    "Or",
    "Poly",
    "Relation",
    "ReplayOutcome",
    "ReplayUnavailable",
    "Tru",
    "ValidatedEntailment",
    "Var",
    "decode_goal",
    "encode_goal",
    "replay_artifact",
]
