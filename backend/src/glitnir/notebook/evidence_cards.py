"""List `-- @evidence <verb> <target> <options>` directives as preview cards.

A directive is a comment line where Lean reads code, as `lexing.scan` finds
it; the same text inside a string or a block comment is not one. Where the
scan is unsure, the line is read as a directive unless the source before it
ends inside a literal or comment: evidence only proposes work and never
grants proof, so reading too much costs at most a card that fails.
"""

from typing import Any

from glitnir.providers import (
    DecisionProvider,
    EvidenceProvider,
    EvidenceRequest,
    Providers,
)


def provider_failure(
    provider: EvidenceProvider | DecisionProvider, error: BaseException
) -> str:
    """Word an exception raised by provider code; KeyboardInterrupt propagates."""
    if isinstance(error, KeyboardInterrupt):
        raise error
    return f"Provider {provider.name} failed: {type(error).__name__}: {error}"


def provider_unavailable(
    provider: EvidenceProvider | DecisionProvider,
) -> tuple[str | None, str]:
    """Return what the provider lacks, or the error asking it raised."""
    try:
        missing = provider.unavailable()
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if missing is not None and not isinstance(missing, str):
        return None, f"Provider {provider.name} reported an unreadable availability."
    return missing, ""


def evidence_request(
    provider: EvidenceProvider, directive: dict[str, Any]
) -> tuple[EvidenceRequest | None, str]:
    """Return the provider's request for a directive, or why it rejects it."""
    try:
        request = provider.parse(
            directive["verb"], directive["target"], directive["options"]
        )
    except ValueError as error:
        return None, str(error) or "The provider rejected this directive."
    except BaseException as error:  # provider code must never break the preview
        return None, provider_failure(provider, error)
    if not isinstance(request, EvidenceRequest):
        return None, f"Provider {provider.name} returned an unreadable request."
    return request, ""


def _parse_error(provider: EvidenceProvider, directive: dict[str, Any]) -> str:
    """Return why the provider rejects the directive, or "" if it accepts it."""
    return evidence_request(provider, directive)[1]


def evidence_cards(
    directives: list[dict[str, Any]], providers: Providers
) -> list[dict[str, Any]]:
    """Give each directive its provider and errors; nothing runs or computes."""
    notes = "".join(
        f" (Provider {name} failed to load: {reason})"
        for name, reason in providers.errors.items()
    )
    cards: list[dict[str, Any]] = []
    for directive in directives:
        provider = providers.evidence.get(directive["verb"])
        error, unavailable = directive["error"], None
        if not error and provider is None:
            error = f"No installed provider runs `@evidence {directive['verb']}`."
            error += notes
        if not error and provider is not None:
            unavailable, error = provider_unavailable(provider)
            error = error or _parse_error(provider, directive)
        cards.append(
            {
                "cell": directive["cell"],
                "line": directive["line"],
                "verb": directive["verb"],
                "target": directive["target"],
                "key": directive["key"],
                "provider": provider.name if provider is not None else None,
                "error": error,
                "unavailable": unavailable,
                "status": "never",
                "run_error": None,
                "snapshot": None,
                "failure": None,
            }
        )
    return cards
