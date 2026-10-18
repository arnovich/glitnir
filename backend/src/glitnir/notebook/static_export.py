"""Static export: notebooks shown from their saved results, for galleries.

A static page shows what a notebook's own checks last saved. `SavedNotebook`
is the public entry point for building one outside the editor: its
`~SavedNotebook.snapshot` is the snapshot the shared renderer (``app/``) reads
through ``GET /api/notebook``, built from the saved source and its saved
``<Notebook>.glitnir.json`` *as recorded*. Each saved result is compared only
with the source, never with the reader's Lean environment, so the snapshot
says so (``preview.recorded``) and the renderer words it "Saved results, shown
as recorded", never "current".

Reading a snapshot never runs Lean, git or a provider computation, and never
writes a file. Drawing a view (`~SavedNotebook.draw_view`) runs Lean in the
sandbox to draw, as the editor does; it checks nothing and writes nothing
beside the notebook. `export_styles` is the CSS the renderer's Export HTML
embeds (``GET /api/export/styles``). Export reports recorded observations: it
neither runs Lean for checks nor updates records.
"""

import base64
import re
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from glitnir.lean.fingerprint import direct_imports, header_imports
from glitnir.notebook.source_preview import SourcePreviewService, _provider_imports

_APP = Path(__file__).parent / "app"

#: The words a snapshot from saved results carries as its `preview.notice`.
RECORDED_NOTICE = (
    "Saved results, shown as recorded. Each result is shown as its check last "
    "saved it, compared only with this saved source and not with the reader's "
    "Lean environment. Nothing was checked or run to show it."
)
#: How often `SavedNotebook.draw_view` asks whether a drawing is done.
POLL_SECONDS = 0.2


def _as_recorded(*_imports: str) -> None:
    """Compare every saved result with the environment it was recorded in."""
    return None


class SavedNotebook:
    """A saved notebook and its saved results, read for a static page.

    Only reads are offered: nothing here checks the notebook, runs evidence
    or decisions, or writes its records. Close it, or use it as a context
    manager, so a drawing still in progress is stopped.

    Args:
        path: The notebook's ``.lean`` file. Its results are read from the
            ``<Notebook>.glitnir.json`` beside it.
    """

    def __init__(self, path: Path) -> None:
        """Keep the notebook's location; nothing is read or started yet."""
        self.path = path
        self._service = SourcePreviewService(path)

    def __enter__(self) -> "SavedNotebook":
        """Return this notebook; `close` runs when the block ends."""
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        """Close the notebook."""
        self.close()

    def close(self) -> None:
        """Stop any drawing in progress and wait briefly for it."""
        self._service.close()

    def snapshot(self, *, run_prompts: bool = False) -> dict[str, Any]:
        """Return the renderer's snapshot of the saved source and saved results.

        Each saved claim, evidence run and decision keeps its saved status and
        is compared only with the saved source: one whose source changed
        since it was saved is out of date, and every other one is shown as
        recorded. ``preview.recorded`` is True and ``preview.notice`` is
        `RECORDED_NOTICE`. A claim's ``current`` and an evidence card's
        ``"current"`` status mean "recorded for this source"; the renderer
        words them so.

        Args:
            run_prompts: Keep what running a provider would need (an
                evidence card's or decision's ``unavailable``, such as an
                extra to install, and ``directive_unavailable``). A static page
                runs nothing, so by default cards show their saved results
                without such prompts. A directive with no installed provider
                still says so.

        Returns:
            The snapshot, in the shape ``GET /api/notebook`` serves. Its
            ``sha256`` is the saved source's SHA-256, with CRLF read as LF.
        """
        service = self._service
        snapshot = service._snapshot(service._text(), _as_recorded)
        snapshot["preview"]["recorded"] = True
        snapshot["preview"]["notice"] = RECORDED_NOTICE
        if not run_prompts:
            for card in [*snapshot["evidence"], *snapshot["decisions"]]:
                card["unavailable"] = None
            snapshot["directive_unavailable"] = {"evidence": {}, "decide": {}}
        return snapshot

    def asset(self, reference: str, sha256: str) -> tuple[bytes, str]:
        """Return an image the saved prose shows, at the hash its snapshot lists.

        Returns:
            The image's bytes and media type.

        Raises:
            glitnir.notebook.assets.AssetError: The image is not listed, has
                another hash or cannot be read.
        """
        return self._service.asset(reference, sha256)

    def view_imports(self) -> list[str]:
        """List the modules Lean loads to draw this notebook's views, sorted.

        They are the notebook's own imports and the imports of each listed
        view's provider, and must be built in the notebook's Lake project
        before `draw_view`. Toolchain modules (``Init``, ``Std``, ``Lean``,
        ``Lake``) are listed as named. A notebook without drawable views
        needs none. No saved result is read.
        """
        service = self._service
        text = service._text()
        snapshot = service._snapshot(text, _as_recorded)
        views = [view for view in snapshot["views"] if not view["error"]]
        if not views:
            return []
        header = header_imports(text)
        modules = set(direct_imports(text) if header is None else header)
        providers = service._providers()
        for view in views:
            modules.update(_provider_imports(providers.views[view["kind"]]))
        return sorted(modules)

    def draw_view(self, cell: int, line: int) -> dict[str, Any]:
        """Draw the view listed at `cell` and `line` of the snapshot; wait for it.

        The view is drawn as the editor draws it, by its provider with Lean in
        the sandbox, from the notebook project's own build, bounded by the
        check time limit. Nothing is checked, and a drawing never changes a
        result.

        Returns:
            The drawing the renderer reads from ``GET /api/view`` (``svg`` and
            any structured geometry), or ``{"error": message}``.

        Raises:
            KeyError: No view directive is listed at `cell` and `line`.
        """
        service = self._service
        while True:
            answer = service.view(cell, line)
            state = answer.get("status")
            if state not in ("drawing", "queued"):
                return answer
            while (polled := service.view_job(answer["job"])).get("status") == (
                "drawing"
            ):
                time.sleep(POLL_SECONDS)
            # Behind another view, or forgotten: ask for this one again.
            if state == "drawing" and not polled.get("transient"):
                return polled


def export_styles() -> str:
    """Return offline CSS, retaining the vendored KaTeX and font licences."""
    katex = _APP / "katex"
    css = (katex / "katex.min.css").read_text()

    def embed(match: re.Match[str]) -> str:
        font = (katex / "fonts" / match[1]).read_bytes()
        encoded = base64.b64encode(font).decode("ascii")
        return f'src:url(data:font/woff2;base64,{encoded}) format("woff2")'

    # Only woff2 files are vendored. Drop the upstream woff/ttf fallbacks too.
    css = re.sub(r"src:url\(fonts/([\w-]+\.woff2)\)[^;}]*", embed, css)
    licence = (katex / "LICENSE").read_text().replace("*/", "* /")
    font_licence = (katex / "OFL.txt").read_text().replace("*/", "* /")
    return "\n".join(
        (
            f"/* KaTeX (MIT licence)\n{licence}\n*/",
            f"/* KaTeX fonts (SIL OFL 1.1)\n{font_licence}\n*/",
            (_APP / "app.css").read_text(),
            (_APP / "cards.css").read_text(),
            (_APP / "circuits.css").read_text(),
            css,
            (_APP / "export.css").read_text(),
        )
    )
