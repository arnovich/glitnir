"""Test-only stand-in for `gimle.asgard.rational_interchange`.

It has the surface the evidence provider may use and returns fixed dummy
values, so the provider's Asgard paths run in CI without Asgard. A circuit
tree that is not a list is refused with `RationalError`, as a malformed one
would be.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Any


class RationalError(ValueError):
    """Raised for a value the stand-in refuses."""


def decode_rational(value: Any) -> Fraction:
    if type(value) is not list:
        raise RationalError("fake: expected rational pair")
    return Fraction(1, 2)


def numerical_value(value: Fraction) -> float:
    return 0.5


@dataclass(frozen=True)
class ExactCircuit:
    """A decoded node; `decode` always returns ``x · p`` from two ports to one."""

    tag: str
    inputs: int
    outputs: int
    coefficient: Fraction = Fraction(0)
    left: "ExactCircuit | None" = None
    right: "ExactCircuit | None" = None

    @classmethod
    def decode(cls, value: Any) -> "ExactCircuit":
        if type(value) is not list:
            raise RationalError("fake: unreadable circuit")
        return cls("multiplication", 2, 1)
