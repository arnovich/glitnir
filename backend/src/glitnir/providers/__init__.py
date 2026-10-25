"""Optional packages that extend notebooks, found through entry points."""

import threading
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Callable, Iterable, NamedTuple

GROUP = "glitnir.providers"


@dataclass(frozen=True)
class ViewProvider:
    """Draws `-- @view <kind> <name>` directives with a Lean adapter.

    Lean elaborates the notebook before the directive, then `preamble`, then
    `command(kind, name, using)`, which prints `marker` followed by the
    drawing. The preamble starts in a fresh top-level scope, without the
    notebook's namespaces, `open`s, options (it has the project's) or
    variables. Environment extensions the notebook activated, such as its
    local and scoped notation, instances and attributes, still apply to it.
    The command runs in the notebook's scope, with the `open`s the preamble
    left at its end added; a name both resolve is ambiguous, as Lean reports.
    """

    name: str
    kinds: frozenset[str]
    imports: tuple[str, ...]
    preamble: str
    marker: str
    command: Callable[[str, str, tuple[str, ...]], str]


@dataclass(frozen=True)
class EvidenceRequest:
    """One `-- @evidence <verb> <target> <options>` directive, as a provider read it.

    `target` is a Lean name; `options` must be JSON-serialisable.
    """

    verb: str
    target: str
    options: dict[str, Any]


@dataclass(frozen=True)
class EvidenceProvider:
    """Runs `-- @evidence <verb> <target>` directives: Lean extracts, Python computes.

    `parse(verb, target, options_text)` raises `ValueError` on bad input.
    `command(request)` is Lean that prints `marker` followed by JSON, which
    `compute(request, payload)` turns into an evidence snapshot; a `ValueError`
    it raises is shown on the card as its message alone. That message is also
    saved as the run's failure in the notebook's committed
    `<Notebook>.glitnir.json`, so it must name no local path or secret (one
    naming a local directory Glitnir knows is not saved, but nothing else is
    filtered). Any other exception from `command` or `compute` is shown with
    its text but saved as its type alone, and an `ImportError` (a missing or
    broken dependency) is shown but never saved. `preamble` and the
    command are elaborated in the scopes `ViewProvider` describes.
    `unavailable()` names a missing optional dependency, or returns None.
    `parse` and `unavailable` run on every preview refresh, so they must be
    cheap and free of side effects.
    """

    name: str
    verbs: frozenset[str]
    imports: tuple[str, ...]
    preamble: str
    marker: str
    parse: Callable[[str, str, str], EvidenceRequest]
    command: Callable[[EvidenceRequest], str]
    compute: Callable[[EvidenceRequest, Any], dict[str, Any]]
    unavailable: Callable[[], str | None]


@dataclass(frozen=True)
class DecisionRequest:
    """One `-- @decide <name> : <goal>` directive, as a provider read it.

    `name` is a Lean identifier, `kind` the goal's first word and `goal` the
    goal text with its continuation lines joined and every run of whitespace
    in it collapsed to a single space; `options` must be JSON-serialisable.
    """

    name: str
    kind: str
    goal: str
    options: dict[str, Any]


@dataclass(frozen=True)
class DecisionProvider:
    """Decides `-- @decide <name> : <goal>` directives whose goal starts with a kind.

    `parse(name, kind, goal)` raises `ValueError` on bad input. `decide(request,
    cancel)` returns an outcome, which the core validates before using it; it
    should stop soon after `cancel` is set. A `ValueError` it raises is shown on
    the card as its message alone, and saved as the run's failure in the
    notebook's committed `<Notebook>.glitnir.json`, so, as for
    `EvidenceProvider.compute`, it must name no local path or secret. Any other
    exception is shown with its text but saved as its type alone, and an
    `ImportError` (a missing or broken dependency) is shown but never saved.
    `imports` are the modules the Lean block it proposes needs.
    `unavailable()` names a missing optional dependency, or returns None.
    `parse` and `unavailable` run on every preview refresh, so they must be
    cheap and free of side effects.
    """

    name: str
    kinds: frozenset[str]
    imports: tuple[str, ...]
    parse: Callable[[str, str, str], DecisionRequest]
    decide: Callable[[DecisionRequest, threading.Event], dict[str, Any]]
    unavailable: Callable[[], str | None]


AnyProvider = ViewProvider | EvidenceProvider | DecisionProvider


class Providers(NamedTuple):
    """Installed providers by view kind, evidence verb and decision kind.

    A view kind, an evidence verb and a decision kind may share a word; each is
    looked up apart. `errors` holds load errors by entry point name.
    """

    views: dict[str, ViewProvider]
    evidence: dict[str, EvidenceProvider]
    decisions: dict[str, DecisionProvider]
    errors: dict[str, str]


def _entry_points() -> Iterable[Any]:
    return metadata.entry_points(group=GROUP)


def _clash(word: str, first: AnyProvider) -> str:
    if isinstance(first, ViewProvider):
        return f"Kind `{word}` is already drawn by provider {first.name}."
    if isinstance(first, DecisionProvider):
        return f"Kind `{word}` is already decided by provider {first.name}."
    return f"Verb `{word}` is already run by provider {first.name}."


def _words(provider: AnyProvider) -> frozenset[str]:
    """Return the view kinds, evidence verbs or decision kinds `provider` serves."""
    if isinstance(provider, EvidenceProvider):
        return provider.verbs
    return provider.kinds


def _checked(provider: Any) -> AnyProvider:
    """Return `provider` if its fields have the declared types; raise otherwise."""
    if not isinstance(provider, (ViewProvider, EvidenceProvider, DecisionProvider)):
        raise TypeError("returned a non-provider object")
    functions: dict[str, object]
    if isinstance(provider, ViewProvider):
        if isinstance(provider.kinds, str):
            raise TypeError("declared kinds as a string")
        functions = {"command": provider.command}
    elif isinstance(provider, EvidenceProvider):
        if isinstance(provider.verbs, str):
            raise TypeError("declared verbs as a string")
        functions = {
            "parse": provider.parse,
            "command": provider.command,
            "compute": provider.compute,
            "unavailable": provider.unavailable,
        }
    else:
        if isinstance(provider.kinds, str):
            raise TypeError("declared kinds as a string")
        functions = {
            "parse": provider.parse,
            "decide": provider.decide,
            "unavailable": provider.unavailable,
        }
    if not isinstance(provider, DecisionProvider):
        if not provider.marker:
            raise ValueError("declared an empty marker")
        if not isinstance(provider.preamble, str):
            raise TypeError("declared a preamble that is not a str")
    if not isinstance(provider.imports, tuple) or not all(
        isinstance(module, str) for module in provider.imports
    ):
        raise TypeError("declared imports that are not a tuple of str")
    for field, function in functions.items():
        if not callable(function):
            raise TypeError(f"declared a {field} that is not callable")
    return provider


def _table(providers: Providers, provider: AnyProvider) -> dict[str, Any]:
    """Return the table of `providers` that serves `provider`'s words."""
    if isinstance(provider, ViewProvider):
        return providers.views
    if isinstance(provider, EvidenceProvider):
        return providers.evidence
    return providers.decisions


def load_providers() -> Providers:
    """Map view kinds, evidence verbs and decision kinds to providers.

    Load errors are collected by entry point name; the first provider of a
    word keeps it.
    """
    loaded = Providers({}, {}, {}, {})
    for entry in _entry_points():
        try:
            found = [_checked(provider) for provider in entry.load()()]
            if not found:
                raise ValueError("returned no providers")
            own = Providers({}, {}, {}, {})
            clashes: list[str] = []
            for provider in found:
                table = _table(own, provider)
                for word in sorted(_words(provider)):
                    if word in table:
                        clashes.append(_clash(word, table[word]))
                    else:
                        table[word] = provider
        except BaseException as error:  # a provider must never break the preview
            if isinstance(error, KeyboardInterrupt):
                raise
            loaded.errors[entry.name] = f"{type(error).__name__}: {error}"
            continue
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (own.views, loaded.views),
            (own.evidence, loaded.evidence),
            (own.decisions, loaded.decisions),
        ]
        for words, table in pairs:
            for word, provider in words.items():
                if word in table:
                    clashes.append(_clash(word, table[word]))
                else:
                    table[word] = provider
        if clashes:  # the first provider of a word keeps it
            loaded.errors[entry.name] = " ".join(clashes)
    return loaded
