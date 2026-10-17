"""Lean elaboration limits hold without any pinned Lean release."""

import pytest

from glitnir.lean.limits import (
    ELABORATION_TIMEOUT_VARIABLE,
    SessionLimits,
    elaboration_timeout_ms,
)


def test_the_elaboration_ceiling_defaults_and_can_be_raised(monkeypatch) -> None:
    """Thirty seconds unless the environment says otherwise, and never garbage."""
    monkeypatch.delenv(ELABORATION_TIMEOUT_VARIABLE, raising=False)
    assert elaboration_timeout_ms() == 30_000
    monkeypatch.setenv(ELABORATION_TIMEOUT_VARIABLE, "180000")
    assert elaboration_timeout_ms() == 180_000
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv(ELABORATION_TIMEOUT_VARIABLE, bad)
        with pytest.raises(ValueError, match=ELABORATION_TIMEOUT_VARIABLE):
            elaboration_timeout_ms()


def test_session_limits_reject_non_integer_and_over_cap_values() -> None:
    """Limits are exact positive integers below the shared ceilings."""
    assert SessionLimits().timeout_ms == 30_000
    with pytest.raises(TypeError):
        SessionLimits(timeout_ms=True)
    with pytest.raises(ValueError, match="positive"):
        SessionLimits(max_heartbeats=0)
    with pytest.raises(ValueError, match="limit"):
        SessionLimits(timeout_ms=1_800_001)


def test_the_memory_limit_defaults_to_eight_gigabytes(monkeypatch) -> None:
    """Without a setting Lean gets 8192 MB."""
    from glitnir.lean.limits import MEMORY_LIMIT_VARIABLE, memory_limit_mb

    monkeypatch.delenv(MEMORY_LIMIT_VARIABLE, raising=False)
    assert memory_limit_mb() == 8192
    monkeypatch.setenv(MEMORY_LIMIT_VARIABLE, "")
    assert memory_limit_mb() == 8192


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("1024", 1024),
        ("16384", 16384),
        ("32768", 32768),
        ("512", 1024),
        ("99999", 32768),
    ],
)
def test_the_memory_limit_is_configurable_within_bounds(
    monkeypatch, configured, expected
) -> None:
    """A configured limit is used, clamped to 1024..32768 MB."""
    from glitnir.lean.limits import MEMORY_LIMIT_VARIABLE, memory_limit_mb

    monkeypatch.setenv(MEMORY_LIMIT_VARIABLE, configured)
    assert memory_limit_mb() == expected


@pytest.mark.parametrize(
    "configured",
    [
        "abc",
        "0",
        "-5",
        "8.5",
        " 8192",
        "²",
        "1000000",
        pytest.param("9" * 5000, id="huge"),
    ],
)
def test_an_invalid_memory_limit_falls_back_to_the_default(
    monkeypatch, configured
) -> None:
    """Anything but a positive decimal integer means the default."""
    from glitnir.lean.limits import MEMORY_LIMIT_VARIABLE, memory_limit_mb

    monkeypatch.setenv(MEMORY_LIMIT_VARIABLE, configured)
    assert memory_limit_mb() == 8192
