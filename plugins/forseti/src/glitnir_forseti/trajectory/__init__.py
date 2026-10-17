"""Circuit-rooted trajectory proofs over Lean-compiled continuous models (112).

A root goal binds a compiled circuit, its interpretation, the time domain, the
initial state by port ID, an observed output and a postcondition over it. The
service assembles a candidate from the model's named Lean contract and task
111's exact certificates, and only `replay_root` — a fresh check of the whole
root theorem against the caller's goal and the pinned environment — decides.
"""

from .goal import TrajectoryGoal, decode_goal, encode_goal
from .replay import InitialWitness, ValidatedRoot, replay_root

__all__ = [
    "InitialWitness",
    "TrajectoryGoal",
    "ValidatedRoot",
    "decode_goal",
    "encode_goal",
    "replay_root",
]
