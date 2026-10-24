"""List `-- @decide <name> : <goal>` directives as preview cards.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it, and the comment lines right after it that start with `--` and at least two
spaces (not `@`) continue its goal. The same text inside a string or a block
comment is not one; where the scan is unsure, lines are read as in
`evidence_cards`. A decision only proposes a Lean block that the project
checker then checks, so reading too much costs at most a card that fails.

A provider's outcome is untrusted; `providers.outcomes.checked_decision` is
all of it the preview and the saved records ever see.
"""

from typing import Any

from glitnir.notebook.evidence_cards import (
    provider_failure,
    provider_unavailable,
)
from glitnir.providers import DecisionProvider, DecisionRequest, Providers


def decision_request(
    provider: DecisionProvider, directive: dict[str, Any]
) -> tuple[DecisionRequest | None, str]:
    """Return the provider's request for a directive, or why it rejects it."""
    try:
        request = provider.parse(
            directive["name"], directive["kind"], directive["goal"]
        )
    except ValueError as error:
        return None, str(error) or "The provider rejected this directive."
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if not isinstance(request, DecisionRequest):
        return None, f"Provider {provider.name} returned an unreadable request."
    return request, ""


def decision_cards(
    directives: list[dict[str, Any]], providers: Providers
) -> list[dict[str, Any]]:
    """Give each directive its provider and errors; nothing runs or decides."""
    notes = "".join(
        f" (Provider {name} failed to load: {reason})"
        for name, reason in providers.errors.items()
    )
    cards: list[dict[str, Any]] = []
    for directive in directives:
        provider = providers.decisions.get(directive["kind"])
        error, unavailable = directive["error"], None
        if not error and provider is None:
            error = f"No installed provider decides `{directive['kind']}` goals."
            error += notes
        if not error and provider is not None:
            unavailable, error = provider_unavailable(provider)
            error = error or decision_request(provider, directive)[1]
        cards.append(
            {
                **directive,
                "provider": provider.name if provider is not None else None,
                "error": error,
                "unavailable": unavailable,
                # Imports are checked against the prepared setup when a
                # decision runs, not read from the text here.
                "missing_imports": [],
                "status": "never",
                "run_error": None,
                "outcome": None,
                "verification": None,
            }
        )
    return cards
