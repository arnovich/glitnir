"""Preview cards: what the renderer shows for views, evidence and decisions.

Directives (`notebook.directives`) become cards by asking the installed
providers whether they can run them; saved records (`notebook.check_records`)
then fill in each card's last snapshot, failure or outcome and its currency.
Nothing here runs a provider's work, starts a thread or writes a file, and
provider code can never break the preview: its errors become card text.

A provider's outcome is untrusted; `providers.outcomes` is all of it the
preview and the saved records ever see.
"""

from typing import Any

from glitnir.lean.fingerprint import Fingerprint
from glitnir.lean.project_views import view_provider_sha256
from glitnir.notebook.check_records import (
    decision_failure_status,
    decision_status,
    evidence_status,
    saved_decision_failures,
    saved_decisions,
    saved_evidence,
    saved_evidence_failures,
)
from glitnir.providers import (
    DecisionProvider,
    DecisionRequest,
    EvidenceProvider,
    EvidenceRequest,
    Providers,
)
from glitnir.providers.outcomes import checked_decision, checked_evidence


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
                "failure": None,
            }
        )
    return cards


def view_provider_hash(providers: Providers, view: dict[str, Any]) -> str | None:
    """Hash what the listed `view`'s provider adds to draw it, or None."""
    return view_provider_sha256(
        providers.views[view["kind"]], view["kind"], view["name"], tuple(view["using"])
    )


def view_cards(
    views: list[dict[str, Any]], providers: Providers
) -> list[dict[str, Any]]:
    """Report the view kinds no installed provider draws."""
    notes = "".join(
        f" (Provider {name} failed to load: {reason})"
        for name, reason in providers.errors.items()
    )
    return [
        (
            view
            if view["error"] or view["kind"] in providers.views
            else {
                **view,
                "error": f"No installed provider draws `{view['kind']}` views." + notes,
            }
        )
        for view in views
    ]


def environment_digest(fingerprint: Fingerprint, imports: tuple[str, ...] = ()) -> str:
    """Return the fingerprint, or '' (which matches nothing) when it fails."""
    try:
        return fingerprint(imports) if imports else fingerprint()
    except Exception:  # noqa: BLE001 - saved results must not break preview
        return ""  # every saved result then reads as stale


def provider_imports(provider: Any) -> tuple[str, ...]:
    """Return the modules a provider loads besides the notebook's, or ()."""
    try:  # provider code must never break the preview
        imports = provider.imports
        if isinstance(imports, tuple) and all(isinstance(m, str) for m in imports):
            return imports
    except Exception:  # noqa: BLE001
        pass
    return ()


def with_saved_evidence(
    cards: list[dict[str, Any]],
    records: dict[str, Any] | None,
    text: str,
    fingerprint: Fingerprint,
    providers: Providers | None = None,
    compare_environment: bool = True,
) -> list[dict[str, Any]]:
    """Show each card's saved snapshot and failure; nothing is recomputed.

    A run's environment includes its provider's modules, so each card is
    compared with the fingerprint taken with them. Without
    `compare_environment` no fingerprint is taken: cards are shown as recorded.
    A saved failure is shown as `_shown_failure` says.
    """
    saved = saved_evidence(records)
    failures = saved_evidence_failures(records)
    found = [card for card in cards if card["key"] in saved or card["key"] in failures]
    if not found:
        return cards  # nothing saved, no fingerprint
    for card in found:
        environment = ""
        if compare_environment:
            provider = providers.evidence.get(card["verb"]) if providers else None
            imports = provider_imports(provider) if provider is not None else ()
            environment = environment_digest(fingerprint, imports)
        entry = saved.get(card["key"])
        if entry is not None:
            card["status"] = evidence_status(
                entry,
                text,
                card["line"],
                environment,
                compare_environment=compare_environment,
            )
            card["snapshot"] = checked_evidence(entry["snapshot"])
        failure = failures.get(card["key"])
        if failure is not None:
            status = evidence_status(
                failure,
                text,
                card["line"],
                environment,
                compare_environment=compare_environment,
            )
            card["failure"] = _shown_failure(
                card,
                failure,
                status,
                compare_environment,
                shown=card["snapshot"] is not None,
                current=card["status"] == "current",
            )
    return cards


def _shown_failure(
    card: dict[str, Any],
    failure: dict[str, Any],
    status: str,
    compare_environment: bool,
    *,
    shown: bool,
    current: bool,
) -> dict[str, Any] | None:
    """Return a card's saved failure as shown, or None when it is hidden.

    `status` is the failure's own currency; `shown` says the card shows a
    saved result (a snapshot or a decision), and `current` that it is not out
    of date. A failure that is out of date is hidden behind a current result,
    and, as recorded, always: a static page shows only what was recorded for
    its source. Without a saved result the card reads failed, or out of date
    for a failure that is.
    """
    if status == "stale" and (not compare_environment or current):
        return None
    if not shown:
        card["status"] = "failed" if status == "current" else "stale"
    return {
        "kind": failure["kind"],
        "message": failure["message"],
        "created": failure["created"],
        "status": status,
    }


def with_saved_decisions(
    cards: list[dict[str, Any]],
    records: dict[str, Any] | None,
    text: str,
    fingerprint: Fingerprint,
    compare_environment: bool = True,
) -> list[dict[str, Any]]:
    """Show each card's saved outcome, failure and status; nothing is decided.

    Without `compare_environment` no fingerprint is taken, as for evidence. A
    saved failure is shown as `_shown_failure` says, behind any decision that
    is not out of date, and never on an accepted decision.
    """
    saved = saved_decisions(records)
    failures = saved_decision_failures(records)
    found = [card for card in cards if card["key"] in saved or card["key"] in failures]
    if not found:
        return cards  # nothing saved, no fingerprint
    environment = environment_digest(fingerprint) if compare_environment else ""
    for card in found:
        entry = saved.get(card["key"])
        if entry is not None:
            card["status"] = decision_status(
                entry,
                text,
                card["end_line"],
                environment,
                compare_environment=compare_environment,
            )
            card["outcome"] = checked_decision(entry["outcome"])
            verification = entry["verification"]
            card["verification"] = (
                None
                if verification is None
                else {**verification, "axioms": list(verification["axioms"])}
            )
        failure = failures.get(card["key"])
        # An accepted block is part of the notebook, judged by the project
        # check: no later run's failure is news about it.
        if failure is not None and card["status"] != "accepted":
            status = decision_failure_status(
                failure,
                text,
                card["end_line"],
                environment,
                compare_environment=compare_environment,
            )
            card["failure"] = _shown_failure(
                card,
                failure,
                status,
                compare_environment,
                shown=entry is not None,
                current=entry is not None and card["status"] != "stale",
            )
    return cards


def unavailable_words(providers: Providers | None, section: str) -> dict[str, str]:
    """Map each evidence verb or decision kind that cannot run to the reason."""
    if providers is None:
        return {}
    table: dict[str, EvidenceProvider] | dict[str, DecisionProvider] = (
        providers.evidence if section == "evidence" else providers.decisions
    )
    reasons: dict[str, str] = {}
    for word in sorted(table):
        missing, error = provider_unavailable(table[word])
        if missing or error:
            reasons[word] = missing or error
    return reasons


def with_runs(
    cards: list[dict[str, Any]],
    running: str | None,
    run_errors: dict[str, str],
    saved: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Mark the card being run and show each card's last run error.

    A run error that this session also saved as the card's failure (its key
    in ``saved``) takes the failure's place, so no failure is shown twice.
    Any other run error says nothing about the directive (a busy slot, a
    setup failure), so a saved failure stays shown, marked ``earlier``.
    """
    for card in cards:
        if card["key"] == running:
            card["status"] = "running"
        card["run_error"] = run_errors.get(card["key"])
        failure = card.get("failure")
        if card["run_error"] is not None and failure is not None:
            card["failure"] = (
                None if card["key"] in saved else {**failure, "earlier": True}
            )
    return cards
