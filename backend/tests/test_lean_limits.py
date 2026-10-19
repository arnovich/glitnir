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


def test_default_memory_limit_is_16384_on_a_large_machine(monkeypatch) -> None:
    """Without a setting a large machine gives Lean 16384 MB."""
    from glitnir.lean import limits

    monkeypatch.delenv(limits.MEMORY_LIMIT_VARIABLE, raising=False)
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 65536)
    assert limits.memory_limit_mb() == 16384
    monkeypatch.setenv(limits.MEMORY_LIMIT_VARIABLE, "")
    assert limits.memory_limit_mb() == 16384


def test_default_memory_limit_leaves_2048_mb_free(monkeypatch) -> None:
    """A smaller machine keeps 2048 MB for everything else."""
    from glitnir.lean import limits

    monkeypatch.delenv(limits.MEMORY_LIMIT_VARIABLE, raising=False)
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 12288)
    assert limits.memory_limit_mb() == 10240


def test_default_memory_limit_never_below_minimum(monkeypatch) -> None:
    """A tiny machine still gets the 1024 MB floor."""
    from glitnir.lean import limits

    monkeypatch.delenv(limits.MEMORY_LIMIT_VARIABLE, raising=False)
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 2048)
    assert limits.memory_limit_mb() == 1024


def test_default_memory_limit_without_known_memory(monkeypatch) -> None:
    """When the machine's memory is unknown the default applies unreduced."""
    from glitnir.lean import limits

    monkeypatch.delenv(limits.MEMORY_LIMIT_VARIABLE, raising=False)
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: None)
    assert limits.memory_limit_mb() == 16384


def test_explicit_memory_limit_is_not_capped_by_memory(monkeypatch) -> None:
    """A configured limit is honoured whatever the machine has."""
    from glitnir.lean import limits

    monkeypatch.setenv(limits.MEMORY_LIMIT_VARIABLE, "20000")
    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 8192)
    assert limits.memory_limit_mb() == 20000


def test_physical_memory_is_positive_or_unknown() -> None:
    """The probe reports megabytes, or None where the system cannot say."""
    from glitnir.lean.limits import physical_memory_mb

    value = physical_memory_mb()
    assert value is None or value > 0


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
    from glitnir.lean import limits

    monkeypatch.setattr(limits, "physical_memory_mb", lambda: 65536)
    monkeypatch.setenv(limits.MEMORY_LIMIT_VARIABLE, configured)
    assert limits.memory_limit_mb() == 16384
