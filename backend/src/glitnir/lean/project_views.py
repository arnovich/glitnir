"""Draw project-mode views through providers; a view never grants proof status."""

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

from glitnir.lean.provider_lean import run_provider_lean
from glitnir.notebook.drawings import checked_drawing
from glitnir.providers import ViewProvider


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


def render_project_view(
    source: Path,
    *,
    expected_sha256: str,
    line: int,
    provider: ViewProvider,
    kind: str,
    name: str,
    using: tuple[str, ...],
    cancel: threading.Event,
    progress: Callable[[str], None] | None = None,
    environment_sha256: str | None = None,
) -> dict[str, Any]:
    """Elaborate the notebook prefix and a provider command in the sandbox.

    The provider's imports come from the project's own build, so the notebook
    need not import them. A busy Lean slot is waited for, up to the check
    time limit; `progress` is told what the drawing is doing. With the
    `environment_sha256` of the notebook and the provider's imports, the
    project's Lake setup is reused while that digest is unchanged. A drawing made
    with the notebook's proofs skipped carries `"proofs_skipped": True`.
    """
    try:  # provider code must never break the preview
        imports = tuple(provider.imports)
        preamble = provider.preamble
        marker = provider.marker
        command = provider.command(kind, name, using)
    except Exception as error:
        return {
            "error": f"Provider {provider.name} failed: "
            f"{type(error).__name__}: {error}"
        }
    result = run_provider_lean(
        source,
        expected_sha256=expected_sha256,
        line=line,
        provider_name=provider.name,
        imports=imports,
        preamble=preamble,
        command=command,
        marker=marker,
        needs="view",
        cancel=cancel,
        progress=progress,
        wait=True,
        environment_sha256=environment_sha256,
    )
    if "payload" not in result:
        return result
    drawing = checked_drawing(result["payload"])
    if result.get("proofs_skipped") and "error" not in drawing:
        return {**drawing, "proofs_skipped": True}
    return drawing
