"""Root proofs about stream models (112, lanes 2–3; 187).

A root goal names a model — the heat reconstruction, or the viscous Burgers
circuit at a declared viscosity — a reading of it (formal stream, evaluated
field or analytic tail), the basis, the declared profile and one claim: the
whole output streams, finitely many output coefficients, the evaluated field
on a box, or the certified truncation of an exponential-sum profile's series.
Candidates cite the Lean contracts and adapters of forseti-lean 020/021,
task 111's certificates and asgard-lean's majorant theorems; only
`replay_stream`, a fresh check of the whole root theorem, decides.
"""

from .goal import StreamGoal, decode_goal, encode_goal
from .replay import ValidatedStream, replay_stream

__all__ = [
    "StreamGoal",
    "ValidatedStream",
    "decode_goal",
    "encode_goal",
    "replay_stream",
]
