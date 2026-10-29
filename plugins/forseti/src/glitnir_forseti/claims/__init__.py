"""Closed goals as text, and one front door to the five deciders.

`text` reads and writes entailment, trajectory, stream, stepped and hedging
goals as text; `decide` passes a goal to its kind's decider and reports that
decider's answer unchanged. Untrusted orchestration: authority stays with each
decider's replay.
"""
