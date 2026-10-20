"""Closed goals as text, and one front door to the four deciders.

`text` reads and writes entailment, trajectory, stream and stepped goals as text;
`decide` passes a goal to its kind's decider and reports that decider's answer
unchanged. Untrusted orchestration: authority stays with each decider's replay.
"""
