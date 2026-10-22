"""Root proofs about stream models.

A root goal names a model — the heat reconstruction, or the viscous Burgers
circuit at a declared viscosity — a reading of it (formal stream, evaluated
field or analytic tail), the basis, the declared profile and one claim: the
whole output streams, finitely many output coefficients, the evaluated field
on a box, or the certified truncation of an exponential-sum profile's series.
Candidates cite the Lean contracts and adapters of Forseti Lean
(``Gimle.Forseti.Examples.FormalHeatContract``,
``Gimle.Forseti.StreamObservation``, ``Gimle.Forseti.FieldObservation``) and
asgard-lean's majorant theorems (``Gimle.Asgard.Streams.AnalyticHeat``); only
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
