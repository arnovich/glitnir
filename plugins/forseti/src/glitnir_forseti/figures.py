"""Show an exact figure in a reason: exactly when short, else by its decimal.

The stepped and hedging deciders compute with `fractions.Fraction` only, and
write their figures into the reasons they give by one rule (`figure`). The
decimal beside or instead of a fraction is itself computed exactly, so no
reason shows a number that floating point rounded.
"""

from __future__ import annotations

import math
from fractions import Fraction

_PLACES = 4
#: Longer exact figures are shown by their decimal alone.
_SHORT = 40
#: A numerator or denominator of more bits than this has more than `_SHORT`
#: digits, so its figure is long without writing it out.
_SHORT_BITS = 4 * _SHORT
#: A decimal of four places is used for values from ``0.00005`` up to this.
_FIXED_BELOW = 10**9


def figure(value: Fraction) -> str:
    """Show a figure as every reason does: exact when short, else a decimal.

    A figure of at most `_SHORT` characters is shown exactly, followed by its
    `_decimal` in parentheses unless four places already write it exactly:
    ``1/4``, ``16384/82433 (0.1988)``, ``1/10000000000 (1.0e-10)``. A longer
    one is shown by its decimal alone, ``≈ 0.0084`` or ``≈ 1.3e-111``.
    """
    if not _short(value):
        return f"≈ {_decimal(value)}"
    if (value * 10**_PLACES).denominator == 1:
        return str(value)
    return f"{value} ({_decimal(value)})"


def equals(value: Fraction) -> str:
    """`figure` after a formula: ``= 1/4``, or ``≈ 0.0084`` if long."""
    shown = figure(value)
    return shown if shown.startswith("≈") else f"= {shown}"


def _short(value: Fraction) -> bool:
    """Whether ``value`` is written in at most `_SHORT` characters.

    A part of more than `_SHORT_BITS` bits is too long to write out at all,
    and may be past the digits `str` writes for an integer.
    """
    bits = max(value.numerator.bit_length(), value.denominator.bit_length())
    return bits <= _SHORT_BITS and len(str(value)) <= _SHORT


def _decimal(value: Fraction) -> str:
    """Approximate a rational in decimal, exactly computed, rounding half up.

    Four places, ``0.1988``, for a magnitude from ``0.00005`` up to
    `_FIXED_BELOW`; any other nonzero value takes two significant digits,
    ``1.3e-111``, so that no figure reads as zero or as a bound on itself.
    """
    if value < 0:
        return f"-{_decimal(-value)}"
    scale = 10**_PLACES
    scaled = math.floor(value * scale + Fraction(1, 2))
    if value == 0 or 0 < scaled < _FIXED_BELOW * scale:
        return f"{scaled // scale}.{scaled % scale:0{_PLACES}d}"
    return _scientific(value)


def _scientific(value: Fraction) -> str:
    """Write a positive rational with two significant digits, ``1.3e-111``."""
    # ``2^(b − 1) ≤ value < 2^(b + 1)`` for the bit lengths' difference ``b``,
    # and ``0.3 < log₁₀ 2 < 0.31``: start at a power of ten at or below the
    # value's, then climb to it.
    low = value.numerator.bit_length() - value.denominator.bit_length() - 1
    exponent = low * 3 // 10 if low >= 0 else low * 31 // 100
    while value >= Fraction(10) ** (exponent + 1):
        exponent += 1
    digits = math.floor(value / Fraction(10) ** exponent * 10 + Fraction(1, 2))
    if digits == 100:
        digits, exponent = 10, exponent + 1
    return f"{digits // 10}.{digits % 10}e{exponent}"
