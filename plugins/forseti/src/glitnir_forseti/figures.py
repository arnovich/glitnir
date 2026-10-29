"""Show an exact figure in a reason or a sentence, and never a float's rounding.

The stepped and hedging deciders compute with `fractions.Fraction` only, and
write their figures into the reasons they give by one rule (`figure`). The
hedging lane's sentences, in the docstrings of the blocks it proposes, write a
number decimal first with the fraction beside (`stated`), a simple return as a
signed percentage (`percent`) and a premium rounded up (`rounded_up`). Every
decimal beside or instead of a fraction is itself computed exactly, so no
reason or sentence shows a number that floating point rounded, and a rounded
one is marked ``≈``.
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
#: A sentence writes a number as its decimal when that ends within this many
#: places: ``3.555``, ``0.0125``.
_ENDING_PLACES = 8
#: A sentence writes a number exactly only up to this many bits in its
#: numerator or denominator, about 150 digits; past it, by its decimal alone.
_WRITTEN_BITS = 512


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


def exact(value: Fraction, *, plus: bool = False) -> str:
    """Write a rational exactly, its sign ``−``: ``1/50``, ``−1/50``, ``100``.

    With ``plus`` a positive value is written ``+1/50``.
    """
    magnitude = abs(value)
    text = (
        str(magnitude.numerator)
        if magnitude.denominator == 1
        else f"{magnitude.numerator}/{magnitude.denominator}"
    )
    if value < 0:
        return f"−{text}"
    return f"+{text}" if plus and value > 0 else text


def stated(value: Fraction) -> str:
    """Write a number in a sentence: its decimal when it ends, the fraction beside.

    An integer is itself, ``100``; a value whose decimal ends within
    `_ENDING_PLACES` places is that decimal with the fraction beside,
    ``3.555 (711/200)`` or ``−0.5 (−1/2)``; any other is exact with its rounded
    decimal beside, ``1/3 (≈ 0.3333)``. A value too long to write in a
    sentence, of more than `_WRITTEN_BITS` bits, is its decimal alone,
    ``≈ 3.5549``. The sign is ``−``.
    """
    if _bits(value) > _WRITTEN_BITS:
        return f"≈ {_minus(_decimal(value))}"
    if value.denominator == 1:
        return exact(value)
    places = _places(value)
    if places is not None:
        return f"{_fixed(value, places)} ({exact(value)})"
    return f"{exact(value)} (≈ {_minus(_decimal(value))})"


def percent(value: Fraction) -> str:
    """Write a simple return as a signed percentage: ``+2 %``, ``−1.25 %``.

    A percentage whose decimal does not end within `_ENDING_PLACES` places is
    rounded to four and marked: ``≈ +0.3333 %``.
    """
    sign = "−" if value < 0 else "+" if value > 0 else ""
    hundred = abs(value) * 100
    places = _places(hundred)
    if places is not None and _bits(hundred) <= _WRITTEN_BITS:
        return f"{sign}{_fixed(hundred, places)} %"
    return f"≈ {sign}{_decimal(hundred)} %"


def move(value: Fraction) -> str:
    """Write a simple return as a percentage, its exact value beside when short.

    ``+2 % (+1/50)``, ``−2 % (−1/50)``; a return too long for `figure` to
    write exactly is its percentage alone.
    """
    shown = percent(value)
    return f"{shown} ({exact(value, plus=True)})" if _short(value) else shown


def rounded_up(value: Fraction) -> str | None:
    """Write a value rounded up to four places, ``3.5549``, or None if too large.

    It is at least the value, as a premium that covers must be. A value not
    below `_FIXED_BELOW` has no four-place decimal to read, and gives None.
    """
    if not value < _FIXED_BELOW:
        return None
    return _fixed(Fraction(math.ceil(value * 10**_PLACES), 10**_PLACES), _PLACES)


def _bits(value: Fraction) -> int:
    """Return the bits of the longer of a rational's numerator and denominator."""
    return max(value.numerator.bit_length(), value.denominator.bit_length())


def _places(value: Fraction) -> int | None:
    """Return the places that write ``value`` exactly, or None past `_ENDING_PLACES`."""
    for places in range(_ENDING_PLACES + 1):
        if (value * 10**places).denominator == 1:
            return places
    return None


def _fixed(value: Fraction, places: int) -> str:
    """Write ``value``, exact at ``places`` places, as a decimal: ``−3.555``."""
    scaled = abs(value) * 10**places
    assert scaled.denominator == 1
    digits = str(scaled.numerator).rjust(places + 1, "0")
    whole, fraction = digits[: len(digits) - places], digits[len(digits) - places :]
    text = f"{whole}.{fraction}" if places else whole
    return f"−{text}" if value < 0 else text


def _minus(text: str) -> str:
    """Write a decimal's sign as ``−``, as a sentence does."""
    return f"−{text[1:]}" if text.startswith("-") else text
