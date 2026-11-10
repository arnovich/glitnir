"""Optional reviewed notebook handoffs, separate from proof-producing providers."""

from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable


@dataclass(frozen=True)
class HandoffProvider:
    """Prepare a reviewable request and explicitly submit its saved envelope.

    Credentials are transient arguments, never part of returned dictionaries.
    Preparation may read remote capabilities but must not mutate remote state.
    Submission must support exact-envelope recovery without launching agents.
    The host owns confirmation and durable storage before submission.
    Optional status reads an existing request without launching or changing work.
    """

    name: str
    label: str
    prepare: Callable[[dict[str, Any], str], dict[str, Any]]
    submit: Callable[[dict[str, Any], str], dict[str, Any]]
    status: Callable[[dict[str, Any], str], dict[str, Any]] | None = None


def load_handoffs() -> dict[str, HandoffProvider]:
    """Discover installed handoffs, refusing duplicate names and invalid callbacks."""
    found: dict[str, HandoffProvider] = {}
    for entry in metadata.entry_points(group="glitnir.providers"):
        for provider in entry.load()():
            if not isinstance(provider, HandoffProvider):
                continue
            if (
                not provider.name
                or not provider.label
                or not callable(provider.prepare)
                or not callable(provider.submit)
                or (provider.status is not None and not callable(provider.status))
                or provider.name in found
            ):
                raise ValueError("Invalid or duplicate notebook handoff provider")
            found[provider.name] = provider
    return found
