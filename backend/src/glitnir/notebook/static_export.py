"""Static export: notebooks shown from their saved results, for galleries.

A static page shows what a notebook's own checks last saved. `SavedNotebook`
is the public entry point for building one outside the editor: its
`~SavedNotebook.snapshot` is the snapshot the shared renderer (``app/``) reads
through ``GET /api/notebook``, built from the saved source and its saved
``<Notebook>.glitnir.json`` *as recorded*. Each saved result is compared only
with the source, never with the reader's Lean environment, so the snapshot
says so (``preview.recorded``) and the renderer words it "Saved results, shown
as recorded", never "current". Only this module asks for that comparison; the
editor's preview always compares with the environment.

Reading a snapshot never runs Lean, git or a provider run, and never writes a
file. Drawing a view (`~SavedNotebook.draw_view`) answers with the drawing saved
in ``<Notebook>.glitnir.json`` (its ``views`` section) when its saved source
and provider hash match, as recorded: without running Lean and without
comparing the environment. Otherwise it runs Lean in the sandbox to draw, as
the editor does, unless the project's Lean version is not the one Glitnir
checks (`glitnir.lean.project_inspection.lean_support`): then there is no
drawing and the refusal is returned. It checks nothing and writes nothing in
the notebook's project, so a drawing made here is not saved. `export_styles` is
the CSS the renderer's Export HTML embeds (``GET /api/export/styles``). Export reports
recorded observations: it neither runs Lean for checks nor updates records.
"""

import base64
import os
import re
import time
from pathlib import Path
from types import TracebackType
from typing import Any

from glitnir.lean.fingerprint import direct_imports, header_imports
from glitnir.notebook.directives import view_keys
from glitnir.notebook.source_preview import SourcePreviewService
from glitnir.preview import assets
from glitnir.preview.cards import provider_imports, view_provider_hash
from glitnir.preview.editor import _APP
from glitnir.preview.snapshot import (
    PreviewState,
    build_snapshot,
    cells,
    prose,
    read_source,
)

#: The words a snapshot from saved results carries as its `preview.notice`.
RECORDED_NOTICE = (
    "Saved results, shown as recorded. Each result is shown as its check last "
    "saved it, compared only with this saved source and not with the reader's "
    "Lean environment. Nothing was checked or run to show it."
)
#: How often `SavedNotebook.draw_view` asks whether a drawing is done.
POLL_SECONDS = 0.2


def _unfingerprinted(*_imports: str) -> str:
    """Stand in for the environment, which a static export never fingerprints.

    It is never called when results are compared as recorded; if it were,
    '' matches no saved result, so nothing would read as current.
    """
    return ""


def _neutral_project(project: dict[str, Any], notebook: Path) -> dict[str, Any]:
    """Describe the project without local paths: its root relative to `notebook`."""
    root = project.get("root")
    if root is not None:
        root = Path(os.path.relpath(root, notebook.absolute().parent)).as_posix()
    return {"kind": project["kind"], "root": root, "toolchain": project["toolchain"]}


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
        self._service = SourcePreviewService(path, save_drawings=False)
        self._providers = self._service._providers

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

    def _recorded(self, text: str) -> dict[str, Any]:
        return build_snapshot(
            self.path,
            text,
            _unfingerprinted,
            PreviewState(),
            self._providers,
            compare_environment=False,
        )

    def snapshot(self, *, run_prompts: bool = False) -> dict[str, Any]:
        """Return the renderer's snapshot of the saved source and saved results.

        Each saved claim, evidence run and decision keeps its saved status and
        is compared only with the saved source: one whose source changed
        since it was saved is out of date, one saved under no known
        environment is out of date too, and every other one is shown as
        recorded. ``preview.recorded`` is True and ``preview.notice`` is
        `RECORDED_NOTICE`. A claim's ``current`` and an evidence card's
        ``"current"`` status mean "recorded for this source"; the renderer
        words them so. ``preview.project`` names the project's kind and
        declared toolchain, and its root relative to the notebook's directory,
        so the snapshot holds no local absolute path of it.

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
        snapshot = self._recorded(read_source(self.path))
        preview = snapshot["preview"]
        preview["recorded"] = True
        preview["notice"] = RECORDED_NOTICE
        preview["project"] = _neutral_project(preview["project"], self.path)
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
            glitnir.preview.assets.AssetError: The image is not listed, has
                another hash or cannot be read.
        """
        text = read_source(self.path)
        found = assets.references(prose(cells(text)))
        return assets.read(self.path, found, reference, sha256)

    def view_imports(self) -> list[str]:
        """List the modules Lean loads to draw this notebook's views, sorted.

        They are the notebook's own imports and the imports of each listed
        view's provider, and must be built in the notebook's Lake project
        before `draw_view`. Toolchain modules (``Init``, ``Std``, ``Lean``,
        ``Lake``) are listed as named. A notebook without drawable views
        needs none. The views are found as in `snapshot`, which reads the
        saved results to place claims; nothing is compared or run.
        """
        text = read_source(self.path)
        views = [view for view in self._recorded(text)["views"] if not view["error"]]
        if not views:
            return []
        header = header_imports(text)
        modules = set(direct_imports(text) if header is None else header)
        providers = self._providers()
        for view in views:
            modules.update(provider_imports(providers.views[view["kind"]]))
        return sorted(modules)

    def draw_view(self, cell: int, line: int) -> dict[str, Any]:
        """Draw the view listed at `cell` and `line` of the snapshot; wait for it.

        A drawing saved for the view in the notebook's records is used as
        recorded while the source through its directive and its provider's
        hash (`glitnir.lean.project_views.view_provider_sha256`) match: no
        Lean runs and the environment is not compared, as for every saved
        result in `snapshot`, also for a project on a Lean version Glitnir
        does not check. Otherwise the view is drawn as the editor draws
        it, by its provider with Lean in the sandbox, from the notebook
        project's own build, bounded by the check time limit; that drawing is
        not saved. For a project on a Lean version Glitnir does not check, a
        view without a matching saved drawing is refused (``{"error":
        message}``, the editor's refusal) and nothing is drawn. Nothing is
        checked, and a drawing never changes a result.
        Reading the notebook or its environment for the drawing may fail;
        that failure is returned like a failed drawing.

        Returns:
            The drawing the renderer reads from ``GET /api/view`` (``svg`` and
            any structured geometry), or ``{"error": message}``.

        Raises:
            KeyError: No view directive is listed at `cell` and `line`.
        """
        try:
            return self._drawn(cell, line)
        except KeyError:
            raise
        except Exception as error:  # noqa: BLE001 - reported as the drawing
            return {"error": f"View drawing failed: {type(error).__name__}: {error}"}

    def _drawn(self, cell: int, line: int) -> dict[str, Any]:
        service = self._service
        saved = self._saved_drawing(cell, line)
        if saved is not None:
            return saved
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

    def _saved_drawing(self, cell: int, line: int) -> dict[str, Any] | None:
        """Return the view's saved drawing as recorded, or None to draw it."""
        service = self._service
        text = service._text()
        views = self._recorded(text)["views"]
        view = next((v for v in views if v["cell"] == cell and v["line"] == line), None)
        if view is None or view["error"]:
            return None  # `view` reports it
        provider_sha256 = view_provider_hash(service._providers(), view)
        if provider_sha256 is None:
            return None
        return service._saved_drawing(
            text,
            view_keys(text, views)[line],
            line,
            "",
            provider_sha256,
            compare_environment=False,
        )


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
