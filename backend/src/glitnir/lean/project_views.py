"""Draw project-mode views through providers; a view never grants proof status."""

import threading
from pathlib import Path
from typing import Any, Callable

from glitnir.lean.provider_lean import run_provider_lean
from glitnir.notebook.drawings import checked_drawing
from glitnir.providers import ViewProvider


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
) -> dict[str, Any]:
    """Elaborate the notebook prefix and a provider command in the sandbox.

    The provider's imports come from the project's own build, so the notebook
    need not import them. A busy Lean slot is waited for, up to the check
    time limit; `progress` is told what the drawing is doing.
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
    )
    if "payload" not in result:
        return result
    return checked_drawing(result["payload"])
