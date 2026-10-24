"""Draw project-mode views through providers; a view never grants proof status."""

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

from glitnir.lean.provider_lean import run_provider_batch
from glitnir.providers import ViewProvider
from glitnir.providers.outcomes import checked_drawing


def view_provider_sha256(
    provider: ViewProvider, kind: str, name: str, using: tuple[str, ...]
) -> str | None:
    """Hash what a provider adds to the notebook to draw one view.

    The hash covers the provider's imports, preamble and marker and the
    command it gives for this view, so a saved drawing is drawn again when any
    of them changes. Returns None when the provider raises: such a drawing is
    never saved or reused.
    """
    try:  # provider code must never break the preview
        material = json.dumps(
            [
                list(provider.imports),
                provider.preamble,
                provider.marker,
                provider.command(kind, name, using),
            ]
        )
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _provider_failed(provider: ViewProvider, error: Exception) -> dict[str, Any]:
    return {
        "error": f"Provider {provider.name} failed: {type(error).__name__}: {error}"
    }


def _drawing(result: dict[str, Any]) -> dict[str, Any]:
    """Check one view's payload as a drawing, saying if proofs were skipped."""
    if "payload" not in result:
        return result
    drawing = checked_drawing(result["payload"])
    if result.get("proofs_skipped") and "error" not in drawing:
        return {**drawing, "proofs_skipped": True}
    return drawing


def render_project_views(
    source: Path,
    *,
    expected_sha256: str,
    provider: ViewProvider,
    views: list[tuple[int, str, str, tuple[str, ...]]],
    cancel: threading.Event,
    progress: Callable[[str], None] | None = None,
    environment_sha256: str | None = None,
) -> dict[str, Any]:
    """Draw several views of one provider in one sandboxed Lean run.

    `views` holds `(directive line, kind, name, using)`, lines ascending. As
    `run_provider_batch`, returns `{"views": [drawing, ...]}` in that order,
    each the checked drawing (`checked_drawing`) or `{"error": message}`, or
    `{"error": message}` for a failure of the whole run, marked `transient`
    when a later request may succeed. A view whose command the provider
    cannot give is an error of its own and is left out of the run. The
    provider's imports come from the project's own build, so the notebook
    need not import them. A busy Lean slot is waited for, up to the check
    time limit; `progress` is told what the run is doing. With the
    `environment_sha256` of the notebook and the provider's imports, the
    project's Lake setup is reused while that digest is unchanged. A drawing
    made with the notebook's proofs skipped carries `"proofs_skipped": True`.
    """
    try:  # provider code must never break the preview
        imports = tuple(provider.imports)
        preamble = provider.preamble
        marker = provider.marker
    except Exception as error:
        return _provider_failed(provider, error)
    results: list[dict[str, Any] | None] = []
    commands: list[tuple[int, str]] = []
    for line, kind, name, using in views:
        try:
            command = provider.command(kind, name, using)
            if not isinstance(command, str):
                raise TypeError("the command must be text")
        except Exception as error:
            results.append(_provider_failed(provider, error))
            continue
        results.append(None)
        commands.append((line, command))
    if commands:
        result = run_provider_batch(
            source,
            expected_sha256=expected_sha256,
            provider_name=provider.name,
            imports=imports,
            preamble=preamble,
            marker=marker,
            needs="view",
            views=commands,
            cancel=cancel,
            progress=progress,
            wait=True,
            environment_sha256=environment_sha256,
        )
        if "views" not in result:
            return result
        drawn = iter(result["views"])
        results = [_drawing(next(drawn)) if r is None else r for r in results]
    return {"views": results}
