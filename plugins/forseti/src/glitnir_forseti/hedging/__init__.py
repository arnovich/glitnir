"""The hedging lane: model-free super-hedging of a convex payoff under a move band.

A hedging claim fixes a convex option payoff (a call, a put or a straddle) and a
band within which the spot may move at each of a number of steps, and asks what
a hedger who knows nothing more about the market can guarantee. The prices,
hedge ratios and worst-case shortfalls are plain rational numbers; the
functions that compute them live in `numbers` and use `fractions.Fraction`
only, so that every figure reproduces the theorem it quotes. `payoffs` holds the
payoffs a goal may name and the Lean names a block cites for each.
"""
