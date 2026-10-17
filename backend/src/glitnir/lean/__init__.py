"""Untrusted tooling for checking a saved Lean file in its own project.

This package inspects the project's toolchain and packages, checks the saved
file in a sandbox, replays it in the kernel and reports the axioms each
declaration depends on. It observes untrusted processes and grants no proof
authority by itself.
"""
