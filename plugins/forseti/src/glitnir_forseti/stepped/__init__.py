"""The stepped lane: discrete-time stochastic decisions with exact arithmetic.

A stepped model advances by a fixed step under a bounded-variance shock. Its
horizon, fixed-date and expected-energy bounds are plain rational numbers; the
functions that compute them live in `numbers` and use `fractions.Fraction`
only, so that every figure reproduces the theorem it quotes.
"""
